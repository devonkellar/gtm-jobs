#!/usr/bin/env python3
"""
Smartlead email account daily stats snapshot.
Appends today's daily_sent_count for all accounts to account_stats_log.csv.

Usage:
    python smartlead_account_stats.py              # snapshot today
    python smartlead_account_stats.py --summary    # print weekly summary (last 7 days)
    python smartlead_account_stats.py --dry-run    # print stats, no file write
"""

import csv
import sys
import time
import argparse
import requests
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

# -- Config -------------------------------------------------------------------
from secrets_util import get_secret

API_KEY  = get_secret("SMARTLEAD_API_KEY")
BASE_URL = "https://server.smartlead.ai/api/v1"
SOS_ROOT = Path(r"C:\Users\Devon\sos")
DATA_DIR = SOS_ROOT / "shared" / "data" / "smartlead"
CSV_FILE = DATA_DIR / "account_stats_log.csv"

CSV_HEADERS = [
    "date", "account_id", "from_email", "from_name",
    "daily_sent_count", "message_per_day", "is_smtp_ok", "is_imap_ok",
]


# -- API ----------------------------------------------------------------------

def api_get(path, params=None, retries=3):
    url = f"{BASE_URL}/{path}"
    p = {"api_key": API_KEY}
    if params:
        p.update(params)
    for attempt in range(retries):
        try:
            r = requests.get(url, params=p, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return r
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    return None


def fetch_all_accounts():
    accounts = []
    offset = 0
    while True:
        r = api_get("email-accounts", {"limit": 100, "offset": offset})
        if not r or r.status_code != 200:
            print(f"  ERROR fetching accounts at offset {offset}: {r.status_code if r else 'no response'}")
            break
        batch = r.json()
        if not batch:
            break
        accounts.extend(batch)
        offset += len(batch)
        if len(batch) < 100:
            break
    return accounts


# -- Summary ------------------------------------------------------------------

def print_weekly_summary(rows, n_days=7):
    today = date.today()
    cutoff = today - timedelta(days=n_days - 1)

    # Only rows within window
    window = [r for r in rows if r["date"] >= str(cutoff)]
    if not window:
        print("No data in the last 7 days.")
        return

    # Sum per account
    totals = defaultdict(lambda: {"from_email": "", "from_name": "", "total": 0, "days": 0})
    for r in window:
        aid = r["account_id"]
        totals[aid]["from_email"] = r["from_email"]
        totals[aid]["from_name"] = r["from_name"]
        totals[aid]["total"] += int(r["daily_sent_count"])
        totals[aid]["days"] += 1

    # How many unique dates in window
    dates_in_window = sorted(set(r["date"] for r in window))
    print(f"\nWeekly send summary ({dates_in_window[0]} to {dates_in_window[-1]}, {len(dates_in_window)} days logged)")
    print(f"{'Email':<45} {'Name':<25} {'Total Sent':>10} {'Days':>5}")
    print("-" * 90)

    by_total = sorted(totals.values(), key=lambda x: -x["total"])
    grand = 0
    active = 0
    for acct in by_total:
        if acct["total"] > 0:
            active += 1
            grand += acct["total"]
            print(f"{acct['from_email']:<45} {acct['from_name']:<25} {acct['total']:>10,} {acct['days']:>5}")

    print("-" * 90)
    print(f"{'TOTAL':<70} {grand:>10,}")
    print(f"\n{active} active accounts (sent > 0), {len(totals)} total accounts tracked")
    print(f"Avg per active account: {grand // active if active else 0:,} emails over {len(dates_in_window)} days")


# -- Main ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true", help="Print weekly summary from log")
    parser.add_argument("--dry-run", action="store_true", help="Print stats, no file write")
    parser.add_argument("--days", type=int, default=7, help="Days for summary window (default 7)")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # If just summary, read CSV and print
    if args.summary:
        if not CSV_FILE.exists():
            print("No log file found. Run without --summary first.")
            return
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        print_weekly_summary(rows, n_days=args.days)
        return

    today_str = date.today().isoformat()

    # Check if already snapshotted today
    existing_rows = []
    today_already_done = False
    if CSV_FILE.exists():
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            existing_rows = list(csv.DictReader(f))
        if any(r["date"] == today_str for r in existing_rows):
            print(f"Already snapshotted for {today_str}. Use --summary to view.")
            today_already_done = True

    if today_already_done and not args.dry_run:
        # Still print today's counts from existing data
        today_rows = [r for r in existing_rows if r["date"] == today_str]
        total = sum(int(r["daily_sent_count"]) for r in today_rows)
        active = sum(1 for r in today_rows if int(r["daily_sent_count"]) > 0)
        print(f"Today ({today_str}): {total:,} emails sent from {active} accounts")
        return

    print(f"Fetching email account stats for {today_str}...")
    accounts = fetch_all_accounts()
    print(f"  {len(accounts)} accounts fetched")

    today_rows = []
    for a in accounts:
        today_rows.append({
            "date": today_str,
            "account_id": a["id"],
            "from_email": a.get("from_email", ""),
            "from_name": a.get("from_name", ""),
            "daily_sent_count": a.get("daily_sent_count", 0),
            "message_per_day": a.get("message_per_day", 0),
            "is_smtp_ok": a.get("is_smtp_success", False),
            "is_imap_ok": a.get("is_imap_success", False),
        })

    total_sent = sum(int(r["daily_sent_count"]) for r in today_rows)
    active_count = sum(1 for r in today_rows if int(r["daily_sent_count"]) > 0)

    print(f"\n{today_str} snapshot:")
    print(f"  Total sent today:  {total_sent:,}")
    print(f"  Active accounts:   {active_count}")
    print(f"  Total accounts:    {len(today_rows)}")

    # Top senders
    top = sorted(today_rows, key=lambda r: -int(r["daily_sent_count"]))[:10]
    if top and int(top[0]["daily_sent_count"]) > 0:
        print(f"\n  Top senders today:")
        for r in top:
            if int(r["daily_sent_count"]) == 0:
                break
            print(f"    {r['from_email']:<45} {r['daily_sent_count']:>4} sent")

    if args.dry_run:
        print("\n[DRY RUN] No file written.")
        return

    # Append to CSV
    all_rows = existing_rows + today_rows
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nAppended {len(today_rows)} rows to {CSV_FILE}")
    print(f"Total log rows: {len(all_rows)}")


if __name__ == "__main__":
    main()
