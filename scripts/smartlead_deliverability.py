#!/usr/bin/env python3
"""
Smartlead email account deliverability report.

Cross-references reply_to data (replies_log.csv) with send snapshots
(account_stats_log.csv) to flag accounts and domains with zero replies.

Usage:
    python smartlead_deliverability.py               # full report (accounts + domains)
    python smartlead_deliverability.py --domains     # domain-level summary only
    python smartlead_deliverability.py --zero        # only zero-reply active accounts
    python smartlead_deliverability.py --days 30     # only replies in last N days (default: all)
    python smartlead_deliverability.py --csv         # also write CSV output

Output:
    shared/reports/smartlead-deliverability/YYYY-MM-DD.txt (always)
    shared/reports/smartlead-deliverability/YYYY-MM-DD.csv (with --csv)
"""

import csv
import sys
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

SOS_ROOT  = Path(r"C:\Users\Devon\sos")
DATA_DIR  = SOS_ROOT / "shared" / "data" / "smartlead"
REPLIES_CSV   = DATA_DIR / "replies_log.csv"
STATS_CSV     = DATA_DIR / "account_stats_log.csv"
REPORTS_DIR   = SOS_ROOT / "shared" / "reports" / "smartlead-deliverability"

REPORT_HEADERS = [
    "domain", "account_email", "from_name",
    "total_replies", "last_reply_date",
    "sends_today", "message_per_day",
    "days_in_log", "total_sends_logged",
    "is_active_today", "reply_rate_pct",
    "flag",
]


def load_replies(since_date=None):
    """Returns dict: reply_to_email -> {count, last_date, categories}"""
    if not REPLIES_CSV.exists():
        return {}

    by_account = defaultdict(lambda: {"count": 0, "last_date": "", "categories": set()})
    with open(REPLIES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rt = row.get("reply_to", "").strip().lower()
            if not rt:
                continue
            rd = row.get("reply_date", "")
            if since_date and rd and rd[:10] < since_date:
                continue
            by_account[rt]["count"] += 1
            if rd > by_account[rt]["last_date"]:
                by_account[rt]["last_date"] = rd[:10] if rd else ""
            cat = row.get("lead_category", "")
            if cat:
                by_account[rt]["categories"].add(cat)
    return by_account


def load_stats():
    """Returns dict: email -> {sends_today, message_per_day, days_in_log, total_sends}"""
    if not STATS_CSV.exists():
        return {}

    by_account = defaultdict(lambda: {
        "sends_today": 0, "message_per_day": 0,
        "days_in_log": 0, "total_sends": 0,
        "latest_date": "",
    })

    today = date.today().isoformat()
    with open(STATS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            email = row.get("from_email", "").strip().lower()
            if not email:
                continue
            d = row.get("date", "")
            sent = int(row.get("daily_sent_count", 0) or 0)

            by_account[email]["days_in_log"] += 1
            by_account[email]["total_sends"] += sent
            if d > by_account[email]["latest_date"]:
                by_account[email]["latest_date"] = d
                by_account[email]["message_per_day"] = int(row.get("message_per_day", 0) or 0)
            if d == today:
                by_account[email]["sends_today"] = sent

    return by_account


def load_accounts_api():
    """Fallback: fetch current account list from API if stats CSV is sparse."""
    try:
        import time
        import requests
        from secrets_util import get_secret
        API_KEY  = get_secret("SMARTLEAD_API_KEY")
        BASE_URL = "https://server.smartlead.ai/api/v1"
        accounts = []
        offset = 0
        while True:
            r = requests.get(f"{BASE_URL}/email-accounts",
                             params={"api_key": API_KEY, "limit": 100, "offset": offset},
                             timeout=30)
            if not r or r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            accounts.extend(batch)
            offset += len(batch)
            if len(batch) < 100:
                break
        return {
            a["from_email"].lower(): {
                "from_name": a.get("from_name", ""),
                "sends_today": a.get("daily_sent_count", 0),
                "message_per_day": a.get("message_per_day", 0),
                "days_in_log": 1,
                "total_sends": a.get("daily_sent_count", 0),
                "latest_date": date.today().isoformat(),
            }
            for a in accounts if a.get("from_email")
        }
    except Exception as e:
        print(f"  Warning: could not fetch accounts from API: {e}", file=sys.stderr)
        return {}


def domain_of(email):
    return email.split("@")[-1].lower() if "@" in email else email


def build_rows(replies, stats, from_names):
    """Build one row per account, combining reply and stats data."""
    all_emails = set(replies.keys()) | set(stats.keys())

    rows = []
    for email in sorted(all_emails):
        r = replies.get(email, {"count": 0, "last_date": ""})
        s = stats.get(email, {"sends_today": 0, "message_per_day": 0,
                               "days_in_log": 0, "total_sends": 0})
        name = from_names.get(email, "")
        dom  = domain_of(email)

        sends = s["total_sends"]
        reply_ct = r["count"]
        rate = round(reply_ct / sends * 100, 1) if sends > 0 else None

        is_active_today = s["sends_today"] > 0 or s["message_per_day"] > 0

        # Flag: zero replies + active sender = deliverability risk
        if is_active_today and reply_ct == 0:
            flag = "NO_REPLIES"
        elif is_active_today and reply_ct > 0 and rate is not None and rate < 0.5:
            flag = "LOW_REPLY_RATE"
        else:
            flag = ""

        rows.append({
            "domain": dom,
            "account_email": email,
            "from_name": name,
            "total_replies": reply_ct,
            "last_reply_date": r["last_date"],
            "sends_today": s["sends_today"],
            "message_per_day": s["message_per_day"],
            "days_in_log": s["days_in_log"],
            "total_sends_logged": sends,
            "is_active_today": is_active_today,
            "reply_rate_pct": rate,
            "flag": flag,
        })

    return rows


def domain_summary(rows):
    """Aggregate rows by domain."""
    domains = defaultdict(lambda: {
        "accounts": 0,
        "accounts_with_replies": 0,
        "total_replies": 0,
        "send_capacity_per_day": 0,
        "active_today": 0,
        "zero_reply_active": 0,
        "last_reply_date": "",
    })

    for r in rows:
        d = r["domain"]
        domains[d]["accounts"] += 1
        if r["total_replies"] > 0:
            domains[d]["accounts_with_replies"] += 1
        domains[d]["total_replies"] += r["total_replies"]
        domains[d]["send_capacity_per_day"] += r["message_per_day"]
        if r["is_active_today"]:
            domains[d]["active_today"] += 1
        if r["is_active_today"] and r["total_replies"] == 0:
            domains[d]["zero_reply_active"] += 1
        if r["last_reply_date"] > domains[d]["last_reply_date"]:
            domains[d]["last_reply_date"] = r["last_reply_date"]

    return domains


def print_domain_report(domains, out=sys.stdout):
    # Sort: domains with replies desc, then by capacity
    dead     = [(d, v) for d, v in domains.items() if v["total_replies"] == 0 and v["active_today"] > 0]
    low      = [(d, v) for d, v in domains.items() if 0 < v["total_replies"] < 3 and v["active_today"] > 0]
    healthy  = [(d, v) for d, v in domains.items() if v["total_replies"] >= 3 and v["active_today"] > 0]
    inactive = [(d, v) for d, v in domains.items() if v["active_today"] == 0]

    print(f"\n{'='*90}", file=out)
    print("DOMAIN-LEVEL SUMMARY", file=out)
    print(f"{'='*90}", file=out)

    hdr = f"{'Domain':<40} {'Accts':>5} {'Active':>6} {'Replies':>7} {'0-Reply':>7} {'Cap/Day':>7} {'Last Reply':<12}"
    sep = "-" * 90

    if dead:
        print(f"\n-- DEAD DOMAINS ({len(dead)}) -- no replies, actively sending --", file=out)
        print(hdr, file=out)
        print(sep, file=out)
        for d, v in sorted(dead, key=lambda x: -x[1]["send_capacity_per_day"]):
            print(f"{d:<40} {v['accounts']:>5} {v['active_today']:>6} {v['total_replies']:>7} {v['zero_reply_active']:>7} {v['send_capacity_per_day']:>7} {'n/a':<12}", file=out)

    if low:
        print(f"\n-- LOW REPLY DOMAINS ({len(low)}) -- 1-2 replies only --", file=out)
        print(hdr, file=out)
        print(sep, file=out)
        for d, v in sorted(low, key=lambda x: -x[1]["total_replies"]):
            print(f"{d:<40} {v['accounts']:>5} {v['active_today']:>6} {v['total_replies']:>7} {v['zero_reply_active']:>7} {v['send_capacity_per_day']:>7} {v['last_reply_date']:<12}", file=out)

    if healthy:
        print(f"\n-- HEALTHY DOMAINS ({len(healthy)}) -- 3+ replies --", file=out)
        print(hdr, file=out)
        print(sep, file=out)
        for d, v in sorted(healthy, key=lambda x: -x[1]["total_replies"]):
            print(f"{d:<40} {v['accounts']:>5} {v['active_today']:>6} {v['total_replies']:>7} {v['zero_reply_active']:>7} {v['send_capacity_per_day']:>7} {v['last_reply_date']:<12}", file=out)

    if inactive:
        print(f"\n-- INACTIVE DOMAINS ({len(inactive)}) -- not sending today --", file=out)


def print_account_report(rows, only_zero=False, out=sys.stdout):
    if only_zero:
        rows = [r for r in rows if r["flag"] == "NO_REPLIES" and r["is_active_today"]]
        title = f"ZERO-REPLY ACTIVE ACCOUNTS ({len(rows)})"
    else:
        title = f"ALL ACCOUNTS ({len(rows)})"

    print(f"\n{'='*110}", file=out)
    print(title, file=out)
    print(f"{'='*110}", file=out)

    hdr = f"{'Email':<50} {'Name':<20} {'Replies':>7} {'Last Reply':<12} {'Sent/Day':>8} {'Cap/Day':>7} {'Flag':<15}"
    print(hdr, file=out)
    print("-" * 110, file=out)

    # Sort: flagged first, then by replies desc
    sorted_rows = sorted(rows, key=lambda r: (
        0 if r["flag"] == "NO_REPLIES" else 1 if r["flag"] == "LOW_REPLY_RATE" else 2,
        -r["total_replies"]
    ))

    for r in sorted_rows:
        last = r["last_reply_date"] or "—"
        rate_str = f"{r['reply_rate_pct']:.1f}%" if r["reply_rate_pct"] is not None else "—"
        print(
            f"{r['account_email']:<50} {r['from_name']:<20} "
            f"{r['total_replies']:>7} {last:<12} "
            f"{r['sends_today']:>8} {r['message_per_day']:>7} "
            f"{r['flag']:<15}",
            file=out
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--domains", action="store_true", help="Domain summary only")
    parser.add_argument("--zero", action="store_true", help="Only zero-reply active accounts")
    parser.add_argument("--days", type=int, default=0, help="Filter replies to last N days (0 = all time)")
    parser.add_argument("--csv", action="store_true", help="Also write CSV output")
    parser.add_argument("--no-api", action="store_true", help="Don't fetch from API if stats CSV is empty")
    args = parser.parse_args()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    report_path = REPORTS_DIR / f"{today}.txt"
    csv_path    = REPORTS_DIR / f"{today}.csv"

    since_date = None
    if args.days > 0:
        since_date = (date.today() - timedelta(days=args.days)).isoformat()
        print(f"Filtering replies since {since_date} ({args.days} days)")

    print("Loading replies log...")
    replies = load_replies(since_date)
    print(f"  {sum(v['count'] for v in replies.values()):,} replies across {len(replies)} accounts")

    print("Loading account stats log...")
    stats = load_stats()

    # If stats CSV is sparse (only 1 day), supplement with live API data
    if not stats and not args.no_api:
        print("  Stats CSV empty — fetching live from API...")
        stats = load_accounts_api()

    print(f"  {len(stats)} accounts in stats log")

    # Build from_names index from stats CSV
    from_names = {}
    if STATS_CSV.exists():
        with open(STATS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                em = row.get("from_email", "").strip().lower()
                nm = row.get("from_name", "").strip()
                if em and nm:
                    from_names[em] = nm

    print("Building deliverability report...")
    rows = build_rows(replies, stats, from_names)
    domains = domain_summary(rows)

    # Stats summary
    total_accounts = len(rows)
    active_today = sum(1 for r in rows if r["is_active_today"])
    has_replies = sum(1 for r in rows if r["total_replies"] > 0)
    zero_reply_active = sum(1 for r in rows if r["flag"] == "NO_REPLIES")
    total_replies = sum(r["total_replies"] for r in rows)

    dead_domains = sum(1 for v in domains.values() if v["total_replies"] == 0 and v["active_today"] > 0)
    total_domains = len(domains)

    lines = []
    lines.append(f"# Smartlead Deliverability Report -- {today}")
    lines.append("")
    lines.append(f"Replies window: {'all time' if not since_date else f'since {since_date}'}")
    lines.append(f"Total accounts tracked: {total_accounts:,}")
    lines.append(f"Active today:           {active_today:,}")
    lines.append(f"Accounts with replies:  {has_replies:,}")
    lines.append(f"Zero-reply (active):    {zero_reply_active:,}  <-- deliverability risk")
    lines.append(f"Total replies (all):    {total_replies:,}")
    lines.append(f"Total domains:          {total_domains:,}")
    lines.append(f"Dead domains (active):  {dead_domains:,}")
    summary = "\n".join(lines)

    print()
    print(summary)

    import io
    buf = io.StringIO()

    print(summary, file=buf)

    if not args.domains:
        print_account_report(rows, only_zero=args.zero, out=buf)

    print_domain_report(domains, out=buf)

    full_report = buf.getvalue()
    report_path.write_text(full_report, encoding="utf-8")
    print(f"\nReport saved to {report_path}")

    if args.csv:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=REPORT_HEADERS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV saved to {csv_path}")

    # Print domain summary to terminal always
    print_domain_report(domains)


if __name__ == "__main__":
    main()
