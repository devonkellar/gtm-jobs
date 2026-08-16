#!/usr/bin/env python3
"""
Read/write the reply log, against Supabase or the legacy CSV.

WHY
replies_log.csv is written by smartlead_sync.py and read by ~20 scripts across
three repos. A CI runner is stateless, so the CSV cannot be the system of record
once these jobs move to Actions. This module is the seam: callers ask for
replies, and it answers from Supabase (cloud) or the CSV (laptop, today).

BACKEND SELECTION
  REPLIES_BACKEND=supabase   -> Supabase only
  REPLIES_BACKEND=csv        -> CSV only
  unset                      -> supabase if SUPABASE_SERVICE_KEY is present,
                                else csv
Deliberately not a hard switch: during the migration the laptop keeps writing
the CSV while CI writes Supabase, and both must work from the same file.

DEDUP
Upsert key is (campaign_id, lead_email, coalesce(reply_date,'-infinity')) --
see migrations/001 and 002 for why it is neither campaign_lead_map_id nor a
plain column list.

A KNOWN, DELIBERATE NUMBERS CHANGE
Supabase reports slightly FEWER replies than the CSV, and that is the CSV being
wrong. 46 dated keys in the file carry more than one row -- always a real
category paired with a shadow 'Uncategorized' banked at the same second. One
lead's reply at 2026-08-05 09:46, for instance, appears as both 'Not Interested'
and 'Uncategorized'. The CSV counts that reply twice.

Across the file that inflates the raw reply count by 50. Anything reading
through this module will therefore show a handful fewer replies than the old
CSV path did. Verified case by case; it is dedup, not data loss.
"""

import csv
import io
import os
import sys
import time
from pathlib import Path

import requests

# The REPORTING project (Devon's own), not Smartbound's.
#
# Smartbound's project sits at 454 MB of the 500 MB free tier, 220 MB of which is
# apollo_calls -- and that cannot move: ten live client trial portals on
# reporting.smartbound.ai read it, three of them active. This project was at
# 53 MB with 447 MB free, so the reply log and everything the reporting site
# needs lives here instead. Call reporting stays where it is and is not part of
# this system.
#
# Override with REPORTING_SUPABASE_REF if the project ever changes.
PROJECT_REF = os.environ.get("REPORTING_SUPABASE_REF", "ddpxbmsiiwtjjpsguege")
SUPABASE_URL = f"https://{PROJECT_REF}.supabase.co/rest/v1"
TABLE = "campaign_replies"

# Credentials are looked up under the REPORTING_ names first (what CI sets and
# what the freelance .env uses) and fall back to the older bare names.
KEY_NAMES = ("REPORTING_SUPABASE_SERVICE_KEY", "HIRING_SUPABASE_SERVICE_KEY",
             "SUPABASE_SERVICE_KEY")
PW_NAMES = ("REPORTING_SUPABASE_DB_PASSWORD", "HIRING_SUPABASE_DB_PASSWORD",
            "SUPABASE_DB_PASSWORD")

# Same 14 columns as the CSV, in order. CSV name -> Supabase column.
CSV_TO_DB = {
    "reply_date": "reply_date",
    "campaign_id": "campaign_id",
    "campaign_name": "campaign_name",
    "campaign_status": "campaign_status",
    "lead_email": "lead_email",
    "lead_first_name": "lead_first_name",
    "lead_last_name": "lead_last_name",
    "lead_company": "lead_company",
    "lead_category": "lead_category",
    "reply_body": "reply_body",
    "reply_from": "reply_from",
    "reply_to": "reply_to",
    "lead_id": "smartlead_lead_id",
    "campaign_lead_map_id": "campaign_lead_map_id",
}
DB_TO_CSV = {v: k for k, v in CSV_TO_DB.items()}

_INT_COLS = ("campaign_id", "smartlead_lead_id", "campaign_lead_map_id")


def _sos_root() -> Path:
    return Path(os.environ.get("SOS_ROOT", r"C:\Users\Devon\sos"))


def csv_path() -> Path:
    return _sos_root() / "shared" / "data" / "smartlead" / "replies_log.csv"


def _freelance_env() -> dict:
    """
    Parse the freelance repo's .env. That is where the reporting project's
    credentials live on the laptop (HIRING_SUPABASE_*), and it is a different
    file from the SOS .env that secrets_util reads.
    """
    path = Path(os.environ.get(
        "FREELANCE_ENV",
        r"C:\Users\Devon\devon-kellar-freelance\.env"))
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass  # absent in CI, where everything comes from the environment
    return out


def _cred(names, required=True) -> str:
    """First hit across env -> freelance .env -> SOS .env, in name order."""
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    fe = _freelance_env()
    for n in names:
        if fe.get(n):
            return fe[n]
    try:
        from secrets_util import get_secret
        for n in names:
            v = get_secret(n, required=False)
            if v:
                return v
    except Exception:
        pass
    if required:
        raise RuntimeError(f"none of {names} is set")
    return ""


def backend() -> str:
    b = (os.environ.get("REPLIES_BACKEND") or "").strip().lower()
    if b in ("supabase", "csv"):
        return b
    return "supabase" if _cred(KEY_NAMES, required=False) else "csv"


def _headers() -> dict:
    key = _cred(KEY_NAMES)
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _to_db_row(row: dict) -> dict:
    """CSV row -> Supabase row. Blank strings become NULL, not ''."""
    out = {}
    for csv_col, db_col in CSV_TO_DB.items():
        val = (row.get(csv_col) or "").strip()
        if val == "":
            out[db_col] = None
        elif db_col in _INT_COLS:
            try:
                out[db_col] = int(val)
            except ValueError:
                out[db_col] = None
        else:
            out[db_col] = val
    return out


def _fmt_reply_date(val) -> str:
    """
    Postgres hands back '2026-08-14T19:58:00+00:00'; the CSV has
    '2026-08-14 19:58' and every existing reader parses that shape. Same
    instant either way, but a caller doing a string compare or a [:10] date
    slice would silently disagree between backends. Normalise to the CSV form
    so switching backend is invisible to the twenty readers.
    """
    if not val:
        return ""
    s = str(val).replace("T", " ")
    for cut in ("+", "Z"):
        i = s.find(cut)
        if i > 0:
            s = s[:i]
    s = s.strip()
    # One CSV row carries a date with no time ('2026-06-25'), which Postgres
    # stores as midnight and hands back as '2026-06-25 00:00:00'. Render that
    # back as the bare date so it round-trips to the value the CSV holds.
    if s.endswith(" 00:00:00"):
        return s[:10]
    # trim seconds/microseconds: '2026-08-14 19:58:00' -> '2026-08-14 19:58'
    if len(s) >= 16:
        return s[:16]
    return s


def _to_csv_row(row: dict) -> dict:
    """Supabase row -> the exact 14-column CSV shape callers already parse."""
    out = {c: ("" if row.get(d) is None else str(row.get(d)))
           for d, c in DB_TO_CSV.items()}
    out["reply_date"] = _fmt_reply_date(row.get("reply_date"))
    return out


def upsert(rows, chunk: int = 500) -> int:
    """Upsert CSV-shaped rows on (campaign_id, lead_email, reply_date)."""
    if not rows:
        return 0
    payload = [_to_db_row(r) for r in rows]
    # Collapse duplicates inside this batch too -- Postgres rejects an ON
    # CONFLICT that hits the same key twice in one statement. The None here
    # mirrors the coalesce(reply_date,'-infinity') in the DB index: undated
    # status rows are one per campaign+lead, dated replies stay per-timestamp.
    seen, deduped = set(), []
    for r in payload:
        k = (r["campaign_id"], r["lead_email"], r["reply_date"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)

    h = dict(_headers())
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    sent = 0
    for i in range(0, len(deduped), chunk):
        batch = deduped[i:i + chunk]
        for attempt in range(3):
            r = requests.post(
                f"{SUPABASE_URL}/{TABLE}",
                headers=h,
                # PostgREST cannot target an EXPRESSION index (it rejects both
                # the index name and the coalesce() text as a column), so the
                # undated rows would fail here. Callers that need the upsert
                # semantics of migration 002 use upsert_pg() instead; this path
                # is kept for dated-only writes where the column list is valid.
                params={"on_conflict": "campaign_id,lead_email,reply_date"},
                json=batch,
                timeout=120,
            )
            if r.status_code < 300:
                sent += len(batch)
                break
            if attempt == 2:
                raise RuntimeError(
                    f"supabase {TABLE} -> {r.status_code}: {r.text[:400]}")
            time.sleep(2 * (attempt + 1))
    return sent


def upsert_pg(rows, chunk: int = 1000) -> int:
    """
    Upsert via a direct Postgres connection.

    Needed because the natural key from migration 002 is an EXPRESSION index
    (coalesce(reply_date,'-infinity')) and PostgREST cannot name one as a
    conflict target -- it only accepts a column list, which would let the 1,969
    undated rows insert unbounded. Postgres itself takes the expression happily.

    Requires SUPABASE_DB_PASSWORD (see secrets_util) and psycopg2.
    """
    if not rows:
        return 0
    import psycopg2
    from psycopg2.extras import execute_values

    payload = [_to_db_row(r) for r in rows]
    seen, deduped = set(), []
    for r in payload:
        k = (r["campaign_id"], r["lead_email"], r["reply_date"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)

    cols = list(CSV_TO_DB.values())
    updatable = [c for c in cols
                 if c not in ("campaign_id", "lead_email", "reply_date")]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in updatable)
    sql = (
        f"insert into {TABLE} ({', '.join(cols)}) values %s "
        f"on conflict (campaign_id, lead_email, "
        f"(coalesce(reply_date, '-infinity'::timestamptz))) "
        f"do update set {set_clause}, updated_at = now()"
    )

    conn = psycopg2.connect(
        host=f"db.{PROJECT_REF}.supabase.co",
        port=5432, user="postgres", dbname="postgres",
        password=_cred(PW_NAMES), connect_timeout=15, sslmode="require",
    )
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            for i in range(0, len(deduped), chunk):
                batch = deduped[i:i + chunk]
                execute_values(
                    cur, sql,
                    [tuple(r[c] for c in cols) for r in batch],
                    page_size=chunk,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return len(deduped)


def load_all() -> list:
    """Every reply, as CSV-shaped dicts, newest first. Backend-agnostic."""
    if backend() == "csv":
        p = csv_path()
        if not p.exists():
            return []
        with io.open(p, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    h = _headers()
    out, offset, page = [], 0, 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/{TABLE}",
            headers=h,
            params={"select": "*", "order": "reply_date.desc",
                    "offset": offset, "limit": page},
            timeout=120,
        )
        r.raise_for_status()
        got = r.json()
        out.extend(_to_csv_row(x) for x in got)
        if len(got) < page:
            break
        offset += page
    return out


def migrate_csv(dry_run: bool = False) -> dict:
    """One-off: push the whole CSV into Supabase. Idempotent (upsert)."""
    p = csv_path()
    if not p.exists():
        raise SystemExit(f"no CSV at {p}")
    with io.open(p, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    # Mirror the DB key exactly: blank date -> one row per campaign+lead.
    keys = {(r["campaign_id"], r["lead_email"], (r["reply_date"] or "").strip())
            for r in rows}
    dated = sum(1 for r in rows if (r["reply_date"] or "").strip())
    stats = {"csv_rows": len(rows), "distinct_keys": len(keys),
             "collapsed": len(rows) - len(keys),
             "dated": dated, "undated": len(rows) - dated}
    if dry_run:
        stats["written"] = 0
        return stats
    # upsert_pg, not upsert: the natural key is an expression index.
    stats["written"] = upsert_pg(rows)
    return stats


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if "--migrate" in sys.argv:
        s = migrate_csv(dry_run=dry)
        print(f"csv rows        : {s['csv_rows']}")
        print(f"  dated (events): {s['dated']}")
        print(f"  undated (status): {s['undated']}")
        print(f"distinct keys   : {s['distinct_keys']}")
        print(f"collapsed dupes : {s['collapsed']}")
        print(f"written         : {s['written']}{' (dry run)' if dry else ''}")
    else:
        print(f"backend: {backend()}")
        print(f"csv    : {csv_path()}")
        rows = load_all()
        print(f"rows   : {len(rows)}")
