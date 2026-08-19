#!/usr/bin/env python3
"""
Smartlead → SOS reply sync
Pulls all replies across all campaigns and upserts them into the
`campaign_replies` table in Supabase. Writes no files.

Usage:
    python smartlead_sync.py              # sync last 365 days (default)
    python smartlead_sync.py --days 30    # sync last 30 days
    python smartlead_sync.py --dry-run    # print stats, no file write
    python smartlead_sync.py --all        # all time (no date filter)

Output:
    C:/Users/Devon/sos/shared/data/smartlead/replies_log.csv
"""

import os
import re
import sys
import time
import argparse
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html.parser import HTMLParser

# ── Config ────────────────────────────────────────────────────────────────────
import replies_store
from secrets_util import get_secret

API_KEY   = get_secret("SMARTLEAD_API_KEY")
BASE_URL  = "https://server.smartlead.ai/api/v1"
# SOS_ROOT is overridable so this file runs unchanged on the laptop (where it
# defaults to the real SOS tree) and in CI (where the workflow points it at the
# checkout). Same convention as secrets_util.get_secret.
SOS_ROOT  = Path(os.environ.get("SOS_ROOT", r"C:\Users\Devon\sos"))
DATA_DIR  = SOS_ROOT / "shared" / "data" / "smartlead"


# Smartlead default category IDs (from /leads/fetch-categories)
CATEGORY_NAMES = {
    1: "Interested",
    2: "Meeting Request",
    3: "Not Interested",
    4: "Do Not Contact",
    5: "Information Request",
    6: "Out Of Office",
    7: "Wrong Person",
    8: "Uncategorizable",
    9: "Bounce",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []
    def handle_data(self, d):
        self.result.append(d)
    def get_text(self):
        return " ".join(self.result).strip()


def strip_html(html):
    if not html:
        return ""
    s = _HTMLStripper()
    try:
        s.feed(html)
        text = s.get_text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Trim quoted/forwarded content (anything after common dividers)
    for marker in ["On ", "-----Original Message", "________________________________", "From:"]:
        idx = text.find(f"\n{marker}")
        if idx > 50:
            text = text[:idx].strip()
    return text[:2000]


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
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    return None


def api_post(path, json_body=None, retries=3):
    url = f"{BASE_URL}/{path}"
    params = {"api_key": API_KEY}
    for attempt in range(retries):
        try:
            r = requests.post(url, params=params, json=json_body, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return r
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    return None


def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


# ── Core logic ────────────────────────────────────────────────────────────────

def get_all_campaigns():
    r = api_get("campaigns")
    if r and r.status_code == 200:
        return r.json()
    print(f"  ERROR fetching campaigns: {r.status_code if r else 'no response'}")
    return []


def get_campaign_analytics(campaign_id):
    r = api_get(f"campaigns/{campaign_id}/analytics")
    if r and r.status_code == 200:
        return r.json()
    return {}


def get_categorized_leads(campaign_id):
    """Fetch all leads with a category set (these are reviewed/actioned replies)."""
    leads = []
    for cat_id in CATEGORY_NAMES.keys():
        offset = 0
        while True:
            r = api_get(f"campaigns/{campaign_id}/leads",
                        {"offset": offset, "limit": 100, "lead_category_id": cat_id})
            if not r or r.status_code != 200:
                break
            data = r.json()
            batch = data.get("data", [])
            leads.extend(batch)
            total = int(data.get("total_leads", 0))
            offset += len(batch)
            if offset >= total or not batch:
                break
    return leads


def get_message_history(campaign_id, lead_id):
    r = api_get(f"campaigns/{campaign_id}/leads/{lead_id}/message-history")
    if r and r.status_code == 200:
        return r.json().get("history", [])
    return []


def get_uncategorized_replies():
    """Fetch replies that Smartlead AI never classified (unassigned category)."""
    results = []
    offset = 0
    limit = 20
    while True:
        body = {
            "offset": offset,
            "limit": limit,
            "filters": {
                "leadCategories": {"unassigned": True}
            },
        }
        r = api_post("master-inbox/inbox-replies", json_body=body)
        if not r or r.status_code != 200:
            print(f"  WARN: uncategorized fetch failed at offset {offset}: "
                  f"{r.status_code if r else 'no response'}")
            break
        data = r.json()
        batch = data.get("data", [])
        results.extend(batch)
        total = data.get("total_count", 0)
        offset += len(batch)
        if offset >= total or not batch:
            break
        time.sleep(0.1)
    return results


def extract_replies(history, since_dt=None):
    """Pull REPLY type messages from message history, optionally filtered by date."""
    replies = []
    for msg in history:
        if msg.get("type") != "REPLY":
            continue
        dt = parse_date(msg.get("time"))
        if since_dt and dt and dt < since_dt:
            continue
        replies.append({
            "date": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
            "body": strip_html(msg.get("email_body", "")),
            "from": msg.get("from", ""),
            "to": msg.get("to", ""),
        })
    return replies


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365, help="Sync replies from last N days")
    parser.add_argument("--all", action="store_true", help="No date filter — all time")
    parser.add_argument("--dry-run", action="store_true", help="Print stats only, no file write")
    args = parser.parse_args()

    since_dt = None
    if not args.all:
        since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        print(f"Syncing replies since {since_dt.strftime('%Y-%m-%d')} ({args.days} days)")
    else:
        print("Syncing all replies (no date filter)")


    # 1. Get all campaigns
    print("\nFetching campaign list...")
    campaigns = get_all_campaigns()
    print(f"  {len(campaigns)} campaigns found")

    # 2. Filter to campaigns with reply_count > 0 using analytics
    print("\nChecking analytics for reply counts...")
    campaigns_with_replies = []
    for c in campaigns:
        cid = c["id"]
        cname = c.get("name", "")
        cstatus = c.get("status", "")
        # Skip very old archived/drafted campaigns unless --all
        if not args.all and cstatus in ("ARCHIVED", "DRAFTED"):
            continue
        analytics = get_campaign_analytics(cid)
        reply_count = int(analytics.get("reply_count", 0))
        if reply_count > 0:
            campaigns_with_replies.append({
                "id": cid,
                "name": cname,
                "status": cstatus,
                "reply_count": reply_count,
            })
            print(f"  + {cid} | {cname[:50]} | {reply_count} replies")
        time.sleep(0.1)  # Rate limit

    print(f"\n{len(campaigns_with_replies)} campaigns with replies")

    # 3. For each campaign, fetch categorized leads and their reply history
    all_rows = []
    for camp in campaigns_with_replies:
        cid = camp["id"]
        cname = camp["name"]
        cstatus = camp["status"]
        print(f"\n[{cid}] {cname[:60]}")

        leads = get_categorized_leads(cid)
        print(f"  {len(leads)} categorized leads")

        for lead_data in leads:
            lead = lead_data.get("lead", {})
            lead_id = lead.get("id")
            clm_id = lead_data.get("campaign_lead_map_id")
            cat_id = lead_data.get("lead_category_id")
            cat_name = CATEGORY_NAMES.get(cat_id, f"Category {cat_id}")

            history = get_message_history(cid, lead_id)
            replies = extract_replies(history, since_dt)
            time.sleep(0.05)

            if replies:
                for rep in replies:
                    all_rows.append({
                        "reply_date": rep["date"],
                        "campaign_id": cid,
                        "campaign_name": cname,
                        "campaign_status": cstatus,
                        "lead_email": lead.get("email", ""),
                        "lead_first_name": lead.get("first_name", ""),
                        "lead_last_name": lead.get("last_name", ""),
                        "lead_company": lead.get("company_name", ""),
                        "lead_category": cat_name,
                        "reply_body": rep["body"],
                        "reply_from": rep["from"],
                        "reply_to": rep["to"],
                        "lead_id": lead_id,
                        "campaign_lead_map_id": clm_id,
                    })
            else:
                # Categorized but no reply found in history — still record the lead
                all_rows.append({
                    "reply_date": "",
                    "campaign_id": cid,
                    "campaign_name": cname,
                    "campaign_status": cstatus,
                    "lead_email": lead.get("email", ""),
                    "lead_first_name": lead.get("first_name", ""),
                    "lead_last_name": lead.get("last_name", ""),
                    "lead_company": lead.get("company_name", ""),
                    "lead_category": cat_name,
                    "reply_body": "",
                    "reply_from": "",
                    "reply_to": "",
                    "lead_id": lead_id,
                    "campaign_lead_map_id": clm_id,
                })

    # 4. Fetch uncategorized replies (leads that replied but were never classified)
    print("\nFetching uncategorized replies...")
    uncat_items = get_uncategorized_replies()
    print(f"  {len(uncat_items)} uncategorized reply threads")

    for item in uncat_items:
        camp_id = item.get("email_campaign_id")
        lead_id = item.get("email_lead_id")
        clm_id = item.get("email_campaign_lead_map_id", "")
        camp_name = item.get("email_campaign_name", "")

        if not camp_id or not lead_id:
            continue

        history = get_message_history(camp_id, lead_id)
        replies = extract_replies(history, since_dt)
        time.sleep(0.05)

        # Try to get lead details from the first outbound message or the reply itself
        lead_email = item.get("lead_email", "")
        lead_first = item.get("lead_first_name", "")
        lead_last = item.get("lead_last_name", "")
        lead_company = item.get("lead_company_name", "")

        if replies:
            for rep in replies:
                all_rows.append({
                    "reply_date": rep["date"],
                    "campaign_id": camp_id,
                    "campaign_name": camp_name,
                    "campaign_status": "",
                    "lead_email": lead_email,
                    "lead_first_name": lead_first,
                    "lead_last_name": lead_last,
                    "lead_company": lead_company,
                    "lead_category": "Uncategorized",
                    "reply_body": rep["body"],
                    "reply_from": rep["from"],
                    "reply_to": rep["to"],
                    "lead_id": lead_id,
                    "campaign_lead_map_id": clm_id,
                })
        else:
            # Uncategorized but no reply in history — record anyway
            all_rows.append({
                "reply_date": "",
                "campaign_id": camp_id,
                "campaign_name": camp_name,
                "campaign_status": "",
                "lead_email": lead_email,
                "lead_first_name": lead_first,
                "lead_last_name": lead_last,
                "lead_company": lead_company,
                "lead_category": "Uncategorized",
                "reply_body": "",
                "reply_from": "",
                "reply_to": "",
                "lead_id": lead_id,
                "campaign_lead_map_id": clm_id,
            })

    # Build the dedup set from the reply store.
    # Key: (campaign_lead_map_id, reply_from, reply_date)
    #
    # This used to read replies_log.csv. A CI runner has no such file, so every
    # reply looked new on every run: the upsert still stored the right data, but
    # "New rows added" became the total row count and the log stopped meaning
    # anything. Reading the set from the same place we write to is what lets
    # this job run anywhere.
    existing_rows = replies_store.load_all()
    seen = {(str(r.get("campaign_lead_map_id") or ""),
             r.get("reply_from") or "",
             r.get("reply_date") or "")
            for r in existing_rows}

    # Filter to genuinely new rows
    new_rows = []
    for r in all_rows:
        key = (str(r["campaign_lead_map_id"]), r["reply_from"], r["reply_date"])
        if key not in seen:
            new_rows.append(r)
            seen.add(key)

    new_rows.sort(key=lambda r: r["reply_date"] or "", reverse=True)

    print(f"\n{'-'*60}")
    print(f"Fetched: {len(all_rows)} rows")
    print(f"New rows: {len(new_rows)}")
    print(f"Existing rows: {len(existing_rows)}")

    if args.dry_run:
        print("\n[DRY RUN] No file written.")
        return

    # Supabase is the only target now. The CSV write was dropped on 2026-08-19
    # once nothing read the file any more -- every reader was cut over to
    # replies_store first, and the three superseded copies that still open it
    # carry a DO-NOT-RUN banner. Writing it was the last thing that made this
    # job need a filesystem, and therefore the last thing keeping it on the
    # laptop.
    #
    # No try/except: a failed upsert must fail the run. There is no second
    # target to fall back to, and a green run that stored nothing is exactly
    # the failure this migration exists to remove.
    n = replies_store.upsert(new_rows)
    print(f"Supabase: upserted {n} rows into campaign_replies")
    print(f"New rows added: {len(new_rows)}")
    print(f"Total rows: {len(existing_rows) + len(new_rows)}")


if __name__ == "__main__":
    main()
