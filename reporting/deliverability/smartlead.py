"""Smartlead sender accounts, grouped by sending domain.

Two jobs from one call:
  1. mailbox_count per domain -> drives auto-classification (a domain with no
     mailbox cannot send, so grading its SPF is pointless).
  2. per-mailbox warmup + SMTP/IMAP health -> feeds the weekly checker's score.

Step 2 is not consumed yet (that is check.py, build order step 3) but is collected
here because it is the same call and the same pagination.
"""
import datetime as dt
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[3]
SOS_ENV = Path(r"C:\Users\Devon\sos\shared\config\.env")
# Same order as registrars.py -- repo first, then SOS. Both use override=False, so
# first-loaded wins, and two modules disagreeing about precedence is exactly how the
# two-different-PORKBUN_API_KEY trap gets recreated with the next variable.
for _p in (REPO / ".env", SOS_ENV):
    if _p.exists():
        load_dotenv(_p, override=False)

BASE = "https://server.smartlead.ai/api/v1"
TIMEOUT = 90
PAGE = 100          # the API's hard cap per request


def _key():
    key = os.getenv("SMARTLEAD_API_KEY")
    if not key:
        raise SystemExit(
            "PREFLIGHT: SMARTLEAD_API_KEY missing. It lives in the Claude settings "
            "env block, not in .env -- export it before running."
        )
    return key


def fetch_accounts():
    """Every sender account, paginated.

    The MCP tool caps at 100 and silently returns only the first page, which is why
    this hits the REST API directly. ~700 accounts today, so ~7 calls.
    """
    key, offset, out = _key(), 0, []
    while True:
        r = requests.get(
            f"{BASE}/email-accounts",
            params={"api_key": key, "offset": offset, "limit": PAGE},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list):
            raise SystemExit(f"smartlead email-accounts returned {str(batch)[:200]}")
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        offset += PAGE


def by_domain(accounts=None):
    """{sending_domain: {mailbox_count, mailboxes:[...]}}.

    A mailbox is counted whether or not it is healthy: a broken sender still means
    the domain is a SENDING domain and must be checked. Health is scored later, not
    used to decide whether the domain is in scope.
    """
    accounts = accounts if accounts is not None else fetch_accounts()

    out = {}
    for a in accounts:
        email = (a.get("from_email") or "").strip().lower()
        if "@" not in email:
            continue
        domain = email.rsplit("@", 1)[1]
        w = a.get("warmup_details") or {}
        entry = out.setdefault(domain, {"mailbox_count": 0, "mailboxes": []})
        entry["mailbox_count"] += 1
        entry["mailboxes"].append(
            {
                "email": email,
                "id": a.get("id"),
                "type": a.get("type"),
                "smtp_ok": bool(a.get("is_smtp_success")),
                "imap_ok": bool(a.get("is_imap_success")),
                "smtp_error": a.get("smtp_failure_error"),
                "imap_error": a.get("imap_failure_error"),
                "warmup_status": w.get("status"),
                "warmup_reputation": w.get("warmup_reputation"),
                "warmup_created_at": w.get("warmup_created_at"),
                "warmup_sent": w.get("total_sent_count"),
                "warmup_spam": w.get("total_spam_count"),
                "blocked_reason": w.get("blocked_reason"),
                "max_per_day": w.get("max_email_per_day"),
            }
        )
    # Warmup age per domain = the OLDEST mailbox still warming. Oldest, not newest, so
    # adding one fresh mailbox to a long-warmed domain does not drop it back to
    # `warming` and pull it out of the sending fleet.
    now = dt.datetime.now(dt.timezone.utc)
    for entry in out.values():
        ages = []
        for m in entry["mailboxes"]:
            created = m.get("warmup_created_at")
            m["warmup_days_self"] = None
            if not created:
                continue
            try:
                started = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            # Per-mailbox age as well as the domain max. The checker needs THIS one to
            # decide whether an individual mailbox is too new to grade -- the domain
            # figure is the oldest box, so using it would grade a day-old mailbox as
            # if it had the oldest one's history.
            m["warmup_days_self"] = (now - started).days
            ages.append((now - started).days)
        # None means "no mailbox has warmup running", which readiness() reads as `new`.
        entry["warmup_days"] = max(ages) if ages else None
    return out


if __name__ == "__main__":
    accts = fetch_accounts()
    doms = by_domain(accts)
    print(f"{len(accts)} accounts across {len(doms)} sending domains")

    dead = [
        m for v in doms.values() for m in v["mailboxes"]
        if not m["smtp_ok"] and m["smtp_error"]
    ]
    print(f"{len(dead)} mailboxes with an SMTP failure")
    for m in dead[:5]:
        print(f"  {m['email']}: {str(m['smtp_error'])[:90]}")
