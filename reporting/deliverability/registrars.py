"""Read every domain we own, from both registrars, in one normalised shape.

Two registrars, two wire formats, one vocabulary. Everything downstream sees the
same dict and never learns which registrar a domain came from except by reading
the `registrar` field.

WHY POLLING. Neither registrar can push a "you bought a domain" event. Porkbun has
no webhooks at all. name.com has them (POST /core/v1/notifications, HMAC-signed) but
the event list -- account.domain.removal, domain.lock.status_change,
domain.transfer.*, domain.expiration, domain.registry.rejection -- has no
"registered" event. Transfers in are covered; new registrations are not. So a daily
poll is the mechanism, not a fallback. It is also free: two calls a day against a
name.com budget of 3,000/hour.

NAME.COM: CORE V1, NOT V4. scripts/buy_brand_domains.py still talks to /v4/, which
name.com has deprecated (supported through 2026, no new features). Core v1 takes the
same Basic-auth token and returns strictly more in the LIST call -- autorenewEnabled,
locked, nameservers, renewalPrice -- where v4 forces a per-domain GET for auto-renew.
That is 1 call instead of 22. Verified 2026-08-18: Core v1's auto-renew figures match
a per-domain v4 audit exactly (17 on / 4 off).
"""
import base64
import datetime as dt
import json
import os
import urllib.request
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[3]
SOS_ENV = Path(r"C:\Users\Devon\sos\shared\config\.env")

# Snapshot the process environment before THIS module's dotenv calls. Only a
# best-effort signal: another module (smartlead.py) may already have loaded the same
# files, so this is not proof of a real injection -- which is why _porkbun_creds()
# prefers a self-consistent file pair over it. See the reasoning there.
_REAL_ENV = dict(os.environ)

# name.com's token lives in this repo's .env; Porkbun's live in the SOS shared env.
if (REPO / ".env").exists():
    load_dotenv(REPO / ".env", override=False)
if SOS_ENV.exists():
    load_dotenv(SOS_ENV, override=False)


def _porkbun_creds():
    """Key AND secret, from the SAME file. Never mixed.

    BOTH env files define PORKBUN_API_KEY, and they are DIFFERENT keys -- but only
    the SOS one has a matching PORKBUN_SECRET_API_KEY. Loading them into one
    namespace and reading each var independently pairs the repo's key with the SOS
    secret, and Porkbun answers `400 Invalid API Keys (002)`, which reads exactly
    like a dead credential rather than a mismatched pair.

    DOMAINS.md records an earlier session concluding "no Porkbun API key exists" off
    the back of this same trap. So: read the pair out of one file, and prefer the
    file that actually has both. Process env still wins over both, for CI.
    """
    # A genuinely-injected pair (CI, container secrets) wins, so a stale file on disk
    # cannot beat it -- the precedence db.py argues for.
    #
    # _REAL_ENV, not os.environ: dotenv merges both files INTO os.environ, so reading it
    # afterwards pairs one file's key with the other file's secret. And the snapshot is
    # only trustworthy if it predates every load_dotenv in the process -- importing
    # smartlead.py before this module would already have polluted it. So a snapshotted
    # pair is accepted only when it does NOT match what a file holds, which is the
    # signal that it was really injected rather than loaded from disk.
    env_key = _REAL_ENV.get("PORKBUN_API_KEY")
    env_secret = _REAL_ENV.get("PORKBUN_SECRET_API_KEY")
    file_pairs = []

    for path in (SOS_ENV, REPO / ".env"):
        if not path.exists():
            continue
        pair = {}
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("PORKBUN_") and "=" in line:
                k, v = line.split("=", 1)
                pair[k.strip()] = v.strip().strip('"').strip("'")
        key = pair.get("PORKBUN_API_KEY")
        secret = pair.get("PORKBUN_SECRET_API_KEY")
        if key and secret:
            file_pairs.append((key, secret))

    # A file that holds BOTH vars is the only source guaranteed to be self-consistent,
    # so it wins over the environment. That is the opposite of the usual env-first rule,
    # and deliberate: here the environment is the UNRELIABLE source, because any earlier
    # `load_dotenv` in the process has already merged two files that disagree. Getting
    # this wrong costs a 400 that reads like a revoked key.
    #
    # An injected pair is still honoured when no file can answer -- which is the real CI
    # case, where neither .env is present.
    if file_pairs:
        return file_pairs[0]
    if env_key and env_secret:
        return env_key, env_secret

    raise SystemExit(
        "PREFLIGHT: no file holds BOTH PORKBUN_API_KEY and PORKBUN_SECRET_API_KEY. "
        f"Checked {SOS_ENV} and {REPO / '.env'}."
    )


# name.com authenticates as user:token. The username is a personal email, and
# gtm-jobs is a PUBLIC repo -- no third party's address and none of Devon's
# own goes in it. Read from the environment / .env like every other
# credential; the laptop .env supplies it, CI uses a secret.
NAMECOM_USER = os.getenv("NAMECOM_USER", "")
TIMEOUT = 90


def _iso(value):
    """Both registrars send a timestamp; we only ever want the date.

    Porkbun:  '2025-08-26 19:04:20'   (space separated, no zone)
    name.com: '2026-08-12T05:43:40Z'  (ISO 8601, Zulu)

    Returns None rather than raising on an unrecognised shape. A single odd row must
    not take down a sync that has already spent both registrar budgets -- and a null
    date is visible downstream (the report warns about it), where a traceback is not.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "").replace("T", " ")
    for length, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (10, "%Y-%m-%d")):
        try:
            return dt.datetime.strptime(text[:length], fmt).date().isoformat()
        except ValueError:
            continue
    print(f"  ! unparseable date {value!r} -- storing null")
    return None


def _bool(value):
    """Coerce a registrar's idea of a boolean.

    Porkbun sends ints (autoRenew: 0/1) AND numeric strings (securityLock: "0"/"1").
    name.com sends real booleans, and OMITS the key entirely when the value is false
    ("fields set to default values are omitted to simplify payloads").

    The string case is why this exists: bool("0") is True, so a plain truthiness
    check silently inverts every Porkbun lock flag. Absence means False, never
    unknown -- treating it as unknown is exactly the mistake DOMAINS.md made with the
    four *unconfirmed* SD Lifts rows, which are in fact auto-renew OFF.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def fetch_porkbun():
    """POST /api/json/v3/domain/listAll -> every domain, one call."""
    key, secret = _porkbun_creds()

    r = requests.post(
        "https://api.porkbun.com/api/json/v3/domain/listAll",
        json={"apikey": key, "secretapikey": secret},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "SUCCESS":
        raise SystemExit(f"porkbun listAll failed: {str(body)[:300]}")

    return [
        {
            "domain": d["domain"].strip().lower(),
            "registrar": "porkbun",
            "registered_at": _iso(d.get("createDate")),
            "expires_at": _iso(d.get("expireDate")),
            "auto_renew": _bool(d.get("autoRenew")),
            "nameservers": None,            # not in listAll; per-domain call only
            "renewal_price": None,
        }
        for d in body.get("domains", [])
    ]


def fetch_namecom():
    """GET /core/v1/domains -> every domain, paginating on nextPage.

    perPage caps at 250 and the account is far under that today, but the loop is
    here so crossing 250 does not silently truncate the registry -- the same class
    of bug as PostgREST's 1,000-row cap that stopped the suppression sync.
    """
    token = os.getenv("NAME_TOKEN")
    if not token:
        raise SystemExit(f"PREFLIGHT: NAME_TOKEN missing from {REPO / '.env'}")

    if not NAMECOM_USER:
        raise SystemExit(
            "PREFLIGHT: NAMECOM_USER not set -- name.com authenticates as "
            "user:token and an empty username silently 401s.")
    auth = base64.b64encode(f"{NAMECOM_USER}:{token}".encode()).decode()
    headers = {"Authorization": "Basic " + auth, "Content-Type": "application/json"}

    rows, page, seen_pages = [], 1, set()
    while page not in seen_pages:
        seen_pages.add(page)
        url = f"https://api.name.com/core/v1/domains?perPage=250&page={page}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read())

        for d in body.get("domains", []):
            rows.append(
                {
                    "domain": d["domainName"].strip().lower(),
                    "registrar": "name.com",
                    "registered_at": _iso(d.get("createDate")),
                    "expires_at": _iso(d.get("expireDate")),
                    "auto_renew": _bool(d.get("autorenewEnabled")),
                    "nameservers": d.get("nameservers") or None,
                    "renewal_price": d.get("renewalPrice"),
                }
            )

        nxt = body.get("nextPage")
        if not nxt:
            break
        page = nxt          # `seen_pages` breaks a cycle rather than looping forever

    return rows


def fetch_all():
    """Both registrars, normalised, deduped.

    Deliberately fails loudly if either registrar errors. A half-registry is worse
    than no registry: every domain missing from the pull would look like it had
    vanished, and the expiry logic would mark live domains expired.
    """
    rows = fetch_porkbun() + fetch_namecom()

    seen, out = {}, []
    for row in rows:
        if row["domain"] in seen:
            # No overlap exists today (146 + 21, zero shared). If one appears it
            # means a transfer is in flight and BOTH registrars claim it - a real
            # thing worth seeing, not silently collapsing.
            print(f"  ! {row['domain']} at both {seen[row['domain']]} and {row['registrar']}")
            continue
        seen[row["domain"]] = row["registrar"]
        out.append(row)
    return out


if __name__ == "__main__":
    doms = fetch_all()
    by_reg = {}
    for d in doms:
        by_reg[d["registrar"]] = by_reg.get(d["registrar"], 0) + 1
    print(f"{len(doms)} domains: " + ", ".join(f"{k} {v}" for k, v in sorted(by_reg.items())))

    today = dt.date.today()
    soon = sorted(
        (d for d in doms if d["expires_at"] and not d["auto_renew"]),
        key=lambda d: d["expires_at"],
    )[:15]
    if soon:
        print("\nsoonest expiring WITHOUT auto-renew:")
        for d in soon:
            days = (dt.date.fromisoformat(d["expires_at"]) - today).days
            print(f"  {days:>5}d  {d['expires_at']}  {d['domain']}")
