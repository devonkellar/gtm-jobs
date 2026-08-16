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


def _to_csv_row(row: dict) -> dict:
    """Supabase row -> the exact 14-column CSV shape callers already parse."""
    return {c: ("" if row.get(d) is None else str(row.get(d)))
            for d, c in DB_TO_CSV.items()}


def upsert(rows, chunk: int = 500) -> int:
    """Upsert CSV-shaped rows on (campaign_id, lead_email, reply_date)."""
    if not rows:
        return 0
    payload = [_to_db_row(r) for r in rows]
    # Collapse duplicates inside this batch too -- Postgres rejects an ON
    # CONFLICT that hits the same key twice in one statement.
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

    keys = {(r["campaign_id"], r["lead_email"], r["reply_date"]) for r in rows}
    stats = {"csv_rows": len(rows), "distinct_keys": len(keys),
             "collapsed": len(rows) - len(keys)}
    if dry_run:
        stats["written"] = 0
        return stats
    stats["written"] = upsert(rows)
    return stats


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if "--migrate" in sys.argv:
        s = migrate_csv(dry_run=dry)
        print(f"csv rows        : {s['csv_rows']}")
        print(f"distinct keys   : {s['distinct_keys']}")
        print(f"collapsed dupes : {s['collapsed']}")
        print(f"written         : {s['written']}{' (dry run)' if dry else ''}")
    else:
        print(f"backend: {backend()}")
        print(f"csv    : {csv_path()}")
        rows = load_all()
        print(f"rows   : {len(rows)}")
