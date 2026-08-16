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
Upsert key is (campaign_id, lead_email, reply_date) -- see
migrations/001_campaign_replies.sql for why it is not campaign_lead_map_id
(4,596 rows, 2,276 distinct map_ids, 197 blank).
"""

import csv
import io
import os
import sys
import time
from pathlib import Path

import requests

SUPABASE_URL = "https://mgonnoxpaqqcbtrkzmpf.supabase.co/rest/v1"
TABLE = "campaign_replies"

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


def backend() -> str:
    b = (os.environ.get("REPLIES_BACKEND") or "").strip().lower()
    if b in ("supabase", "csv"):
        return b
    return "supabase" if os.environ.get("SUPABASE_SERVICE_KEY") else "csv"


def _headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not key:
        # Fall back to the shared .env so the laptop works without exporting.
        try:
            from secrets_util import get_secret
            key = get_secret("SUPABASE_SERVICE_KEY")
        except Exception:
            raise RuntimeError("SUPABASE_SERVICE_KEY not set")
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

    try:
        from secrets_util import get_secret
        pw = os.environ.get("SUPABASE_DB_PASSWORD") or get_secret("SUPABASE_DB_PASSWORD")
    except Exception:
        pw = os.environ.get("SUPABASE_DB_PASSWORD")
    if not pw:
        raise RuntimeError("SUPABASE_DB_PASSWORD not set")

    conn = psycopg2.connect(
        host=f"db.{os.environ.get('SUPABASE_PROJECT_REF', 'mgonnoxpaqqcbtrkzmpf')}.supabase.co",
        port=5432, user="postgres", dbname="postgres",
        password=pw, connect_timeout=15, sslmode="require",
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
