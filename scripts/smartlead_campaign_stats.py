#!/usr/bin/env python3
"""
Smartlead Campaign Stats -> Google Sheet

Pulls aggregate stats for every ACTIVE campaign and writes:
  - `Today` tab: overwritten each run with current ACTIVE campaigns, sorted by
    leads_left ascending. Use as the morning glance dashboard.
  - `Week YYYY-MM-DD` tab (one per week, named after the Monday): append one row
    per campaign per day with WEEK-TO-DATE deltas. Mon row = Mon-only activity,
    Tue row = Mon+Tue, ..., Sun row = full weekly total.

Week-to-date deltas are computed by subtracting Sunday-23:59's lifetime totals
(captured as the "baseline" the first time we run on a given week) from
today's lifetime totals. Baselines are stored in a hidden `_baselines` tab.

`positive_replies` counts NET-NEW LEADS, not reply rows: a lead is counted once,
in the campaign that got their first-ever positive reply. Same rule as
weekly_report.py and the install-KPIs job, so all three reconcile exactly
(verified 0 mismatches across 68 campaigns). Derived from replies_log.csv
(synced by smartlead_sync.py at 08:00 KL). Run at 08:30 KL so replies are fresh.

The `alert` column replaces the old `needs_top_up`. It gates on recent activity
and on RUNWAY (leads_left + inprogress), so it distinguishes a live campaign
about to starve from one that simply finished: TOP UP / FINISHING / IDLE / DRY.
See the ALERT_* block below.

Usage:
    python smartlead_campaign_stats.py              # full run
    python smartlead_campaign_stats.py --dry-run    # print, don't write to sheet
"""

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import gspread
import requests
from google.oauth2.service_account import Credentials

# Single source of truth for "what do the numbers say" (merge completed 2026-07-28).
# This script owns the SHEET; weekly_report.py owns the FETCH. They used to each
# implement lifetime totals and leads-left separately, which meant the
# drafted_count -> notStarted bugfix had to be found and applied twice.
#
# Re-verified on live data at swap time: 0 mismatches across all 10 active
# campaigns on sent/replies/bounces/unsubscribes, including NSL US at 12,168
# sends. The one-call /analytics form also replaces ~15 windowed calls per
# campaign, so a full run costs 10 calls instead of ~150.
#
# The windowed walker (fetch_totals_through) is deliberately NOT deleted: the
# one-call endpoint only reports "as of now", and baseline reconstruction needs
# totals as of a PAST date. That is the one job it still owns.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from weekly_report import fetch_lifetime  # noqa: E402

# Config
from secrets_util import get_secret

API_KEY = get_secret("SMARTLEAD_API_KEY")
BASE_URL = "https://server.smartlead.ai/api/v1"
SHEET_ID = "12j104wAE1_V-onwotjvVaCu69DYT7WWQCnkBCENYzQI"
SA_PATH = Path.home() / ".claude" / "google-service-account.json"
SOS_ROOT = Path(r"C:\Users\Devon\sos")
REPLIES_CSV = SOS_ROOT / "shared" / "data" / "smartlead" / "replies_log.csv"

POSITIVE_CATEGORIES = {"Interested", "Meeting Request", "Information Request"}
BASELINE_TAB = "_baselines"

# Schema for the Today tab (current-state snapshot, lifetime numbers)
TODAY_HEADERS = [
    "date", "campaign_id", "campaign_name", "status",
    "sent", "replies", "positive_replies", "bounces", "bounce_rate",
    "unsubscribes", "total_leads", "leads_left",
    "reply_rate", "positive_reply_rate",
    "days_active", "sent_7d", "alert",
]

# How the `alert` column is decided. The old `needs_top_up` was bare
# `leads_left < 50`, which fired on 9 of 10 campaigns because it could not tell
# "about to run dry" from "finished sending weeks ago" — so it was ignored, and
# an alert everyone ignores is worse than no alert.
#
# The fix is to gate on RECENT ACTIVITY. Only a campaign that actually sent in
# the last 7 days can be running dry; one that is quiet is either done or
# already dead, and neither needs a top-up alarm.
#
# Runway is leads_left PLUS leads still mid-sequence. A campaign with 0 not-yet-
# started leads but 202 people still moving through the sequence is not starving
# this week; one with 0 and 12 is finishing tomorrow. Ignoring `inprogress` was
# what made the old flag fire on nearly everything.
#
#   TOP UP    sent in the last 7 days AND runway < 50
#             -> live and genuinely about to starve. This is the one to act on.
#   FINISHING sent in the last 7 days, no fresh leads, but still working
#             through the sequence. Queue more before it lands in TOP UP.
#   DRY       no sends in 7 days AND runway < 50
#             -> out of leads and already stopped. Dead, not urgent.
#   IDLE      no sends in 7 days AND runway >= 50
#             -> has leads but is not sending. Paused, or broken senders.
#   ""        sending fine with leads in hand.
ALERT_TOP_UP = "TOP UP"
ALERT_FINISHING = "FINISHING"
ALERT_DRY = "DRY"
ALERT_IDLE = "IDLE"
LOW_LEADS = 50
ACTIVITY_WINDOW_DAYS = 7

# Schema for the weekly tab (week-to-date deltas + current state for leads_left)
WEEK_HEADERS = [
    "date", "campaign_id", "campaign_name", "status",
    "sent_wk", "replies_wk", "positive_replies_wk", "bounces_wk", "bounce_rate_wk",
    "unsubscribes_wk", "leads_left",
    "reply_rate_wk", "positive_reply_rate_wk",
    "alert",
]

# Baseline tab schema (one row per campaign per ISO week-start)
BASELINE_HEADERS = [
    "week_start", "campaign_id", "campaign_name",
    "sent", "replies", "positive_replies", "bounces", "unsubscribes",
    "captured_at",
]


# Smartlead API

def fetch_campaigns():
    r = requests.get(f"{BASE_URL}/campaigns", params={"api_key": API_KEY}, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_analytics(campaign_id, start_date, end_date, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(
                f"{BASE_URL}/campaigns/{campaign_id}/analytics-by-date",
                params={"api_key": API_KEY,
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat()},
                timeout=90,
            )
            return r.json() if r.status_code == 200 else None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    print(f"[!] fetch_analytics gave up: cid={campaign_id} {start_date}..{end_date} err={last_err}")
    return None


def fetch_totals_through(campaign_id, created_at, through_date):
    """Walk 30-day windows from campaign start to `through_date`. Returns
    cumulative sent/reply/bounce/unsub totals AND latest total_count/drafted_count
    seen during the walk."""
    start = (created_at or datetime.utcnow()).date()
    if start > through_date:
        start = through_date

    totals = {"sent_count": 0, "reply_count": 0, "bounce_count": 0, "unsubscribed_count": 0}
    latest = {"total_count": 0, "drafted_count": 0}

    window_start = start
    while window_start <= through_date:
        window_end = min(window_start + timedelta(days=29), through_date)
        d = fetch_analytics(campaign_id, window_start, window_end)
        if d and isinstance(d, dict):
            for k in totals:
                try:
                    totals[k] += int(d.get(k) or 0)
                except (TypeError, ValueError):
                    pass
            for k in latest:
                try:
                    latest[k] = int(d.get(k) or 0)
                except (TypeError, ValueError):
                    pass
        window_start = window_end + timedelta(days=1)
        time.sleep(0.15)

    return {**totals, **latest}


def fetch_lifetime_analytics(campaign_id, created_at):
    """Lifetime totals for TODAY. Delegates to weekly_report.fetch_lifetime.

    Kept as a named function (rather than inlining the import at the call site)
    so the windowed walker below stays reachable for baseline reconstruction,
    which genuinely needs an as-of-a-past-date figure the one-call form cannot
    give. Returns the fetch owner's schema, not the old *_count schema.
    """
    return fetch_lifetime(campaign_id)


def fetch_sent_last_7d(campaign_id, today=None):
    """Emails sent in the trailing 7 days (inclusive of today).

    Straight from analytics-by-date, one call, so it does not depend on the
    sheet's own history being complete — a campaign created today is judged
    correctly on its first run. Returns None if the call fails, and the caller
    treats None as "unknown" rather than as zero: an API hiccup must not
    silently mark a healthy campaign DRY.
    """
    today = today or date.today()
    start = today - timedelta(days=ACTIVITY_WINDOW_DAYS - 1)
    d = fetch_analytics(campaign_id, start, today)
    if not d or not isinstance(d, dict):
        return None
    return safe_int(d.get("sent_count"))


def classify_alert(leads_left, sent_7d, inprogress=0):
    """Decide the alert cell. See the ALERT_* block above for the reasoning."""
    if sent_7d is None:                       # unknown activity -> stay silent
        return ""
    active = sent_7d > 0
    runway = leads_left + safe_int(inprogress)
    if active and runway < LOW_LEADS:
        return ALERT_TOP_UP
    if active and leads_left < LOW_LEADS:
        return ALERT_FINISHING                # working the tail, no fresh leads
    if not active and runway < LOW_LEADS:
        return ALERT_DRY
    if not active:
        return ALERT_IDLE
    return ""


# Replies CSV

def load_positive_replies_by_campaign(through_date=None):
    """Count NET-NEW positive LEADS per campaign — not positive reply rows.

    A lead counts ONCE, credited to the campaign that got their FIRST positive
    reply, ever. Dedup is by email (falling back to lead_id), global across all
    time, walking oldest-first. This is the same rule weekly_report.load_replies
    and the install-KPIs job use, so the three now reconcile.

    BUGFIX 2026-07-28: this used to count every positive ROW. Roughly 40% of
    positive rows are the same people replying again, so campaigns were
    overstated without bound — New Sales Leaders UK reported 32 positive
    replies against 28 total replies, which is impossible on its face. Any
    figure derived from this (positive_reply_rate, the weekly deltas, the
    baselines) inherited the inflation.

    `through_date` limits to replies on or before that date, for reconstructing
    a week-boundary baseline. It filters AFTER the global dedup, so a lead whose
    first positive reply came before the window is never re-counted inside it.
    """
    counts = defaultdict(int)
    if not REPLIES_CSV.exists():
        return counts

    with open(REPLIES_CSV, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    # every positive reply ever, oldest first, so "first" means first
    pos = []
    for r in rows:
        if (r.get("lead_category") or "").strip() not in POSITIVE_CATEGORIES:
            continue
        rd = (r.get("reply_date") or "")[:10]
        if not rd:
            continue
        pos.append((rd, r))
    pos.sort(key=lambda t: t[0])

    seen = set()
    for rd, row in pos:
        key = (row.get("lead_email") or "").strip().lower() \
            or f"id:{(row.get('lead_id') or '').strip()}"
        if not key or key == "id:":
            continue
        if key in seen:
            continue                    # same lead talking again, not a new lead
        seen.add(key)
        if through_date is not None and rd > through_date.isoformat():
            continue
        try:
            counts[int(row["campaign_id"])] += 1
        except (ValueError, KeyError):
            pass
    return counts


# Helpers

def parse_created(c):
    s = c.get("created_at")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def week_start_for(d):
    """Monday of the week containing d (ISO style)."""
    return d - timedelta(days=d.weekday())


def safe_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# Google Sheet ops

def open_sheet():
    creds = Credentials.from_service_account_file(
        str(SA_PATH),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def ensure_tab(sh, title, headers, rows=2000, cols=None):
    cols = cols or len(headers)
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws
    existing = ws.row_values(1)
    if existing != headers:
        ws.update("A1", [headers], value_input_option="USER_ENTERED")
    return ws


def load_baselines(sh, week_start):
    """Return {campaign_id: dict(sent, replies, positive_replies, bounces, unsubscribes)}
    of baselines captured at this week_start. Missing campaigns get auto-baselined
    later in capture_missing_baselines()."""
    ws = ensure_tab(sh, BASELINE_TAB, BASELINE_HEADERS)
    rows = ws.get_all_records()
    target = week_start.isoformat()
    out = {}
    for r in rows:
        if str(r.get("week_start")) == target:
            try:
                cid = int(r["campaign_id"])
            except (ValueError, KeyError):
                continue
            out[cid] = {
                "sent": safe_int(r.get("sent")),
                "replies": safe_int(r.get("replies")),
                "positive_replies": safe_int(r.get("positive_replies")),
                "bounces": safe_int(r.get("bounces")),
                "unsubscribes": safe_int(r.get("unsubscribes")),
            }
    return out


def append_baselines(sh, week_start, new_rows):
    if not new_rows:
        return
    ws = ensure_tab(sh, BASELINE_TAB, BASELINE_HEADERS)
    ws.append_rows(new_rows, value_input_option="USER_ENTERED")


# Build rows

def collect_active_campaign_stats():
    """Return list of dicts with lifetime stats for every ACTIVE campaign."""
    campaigns = fetch_campaigns()
    active = [c for c in campaigns if c.get("status") == "ACTIVE"]
    print(f"[i] {len(active)} active campaigns (of {len(campaigns)} total)")
    positives = load_positive_replies_by_campaign()
    out = []
    for c in active:
        cid = c["id"]
        print(f"[i] {cid} {c.get('name','')}", flush=True)
        # One call for totals AND lead stats. leads_left is notStarted, never
        # drafted_count -- the fetch owner enforces that; see its docstring.
        lt = fetch_lifetime_analytics(cid, parse_created(c))
        if lt is None:
            print(f"[!] {cid} lifetime fetch failed -- skipped, not written as zeros")
            continue
        out.append({
            "id": cid,
            "name": c.get("name", ""),
            "created": parse_created(c),
            "sent": lt["sent"],
            "replies": lt["replies"],
            "bounces": lt["bounces"],
            "unsubscribes": lt["unsubscribes"],
            "total_leads": lt["loaded"],
            "leads_left": lt["left"],
            "positive_replies": positives.get(cid, 0),
            "inprogress": lt["inprogress"],
            "sent_7d": fetch_sent_last_7d(cid),
        })
    return out


def reconstruct_baseline(campaign, through_date, positives_through):
    """Compute lifetime totals for `campaign` as of end-of-day `through_date`.
    Used the first time we see a campaign in a given week — gives a true
    'start of week' baseline so week-to-date deltas are accurate."""
    cid = campaign["id"]
    created = parse_created(campaign)
    if created is None or created.date() > through_date:
        return {"sent": 0, "replies": 0, "positive_replies": 0,
                "bounces": 0, "unsubscribes": 0}
    stats = fetch_totals_through(cid, created, through_date)
    return {
        "sent": stats["sent_count"],
        "replies": stats["reply_count"],
        "positive_replies": positives_through.get(cid, 0),
        "bounces": stats["bounce_count"],
        "unsubscribes": stats["unsubscribed_count"],
    }


def build_today_rows(stats, today_str):
    rows = []
    for s in stats:
        sent = s["sent"]
        bounce_rate = round(s["bounces"] / sent * 100, 2) if sent else 0.0
        reply_rate = round(s["replies"] / sent * 100, 2) if sent else 0.0
        pos_rate = round(s["positive_replies"] / sent * 100, 2) if sent else 0.0
        days_active = (date.today() - s["created"].date()).days if s["created"] else 0
        sent_7d = s.get("sent_7d")
        rows.append([
            today_str, s["id"], s["name"], "ACTIVE",
            sent, s["replies"], s["positive_replies"], s["bounces"], bounce_rate,
            s["unsubscribes"], s["total_leads"], s["leads_left"],
            reply_rate, pos_rate,
            days_active,
            "" if sent_7d is None else sent_7d,
            classify_alert(s["leads_left"], sent_7d, s.get("inprogress")),
        ])
    return rows


def build_week_rows(stats, baselines, today_str):
    """For each campaign:
       - if baseline exists, delta = current - baseline
       - if baseline absent (campaign first appeared mid-week), delta = current totals
         from its first-run-day, which approximates as `current - 0`. Caller is
         expected to capture a fresh baseline for these so subsequent days don't
         double-count earlier-this-week activity."""
    rows = []
    for s in stats:
        b = baselines.get(s["id"], {})
        sent_wk = max(0, s["sent"] - safe_int(b.get("sent")))
        replies_wk = max(0, s["replies"] - safe_int(b.get("replies")))
        pos_wk = max(0, s["positive_replies"] - safe_int(b.get("positive_replies")))
        bounces_wk = max(0, s["bounces"] - safe_int(b.get("bounces")))
        unsub_wk = max(0, s["unsubscribes"] - safe_int(b.get("unsubscribes")))
        bounce_rate_wk = round(bounces_wk / sent_wk * 100, 2) if sent_wk else 0.0
        reply_rate_wk = round(replies_wk / sent_wk * 100, 2) if sent_wk else 0.0
        pos_rate_wk = round(pos_wk / sent_wk * 100, 2) if sent_wk else 0.0
        rows.append([
            today_str, s["id"], s["name"], "ACTIVE",
            sent_wk, replies_wk, pos_wk, bounces_wk, bounce_rate_wk,
            unsub_wk, s["leads_left"],
            reply_rate_wk, pos_rate_wk,
            classify_alert(s["leads_left"], s.get("sent_7d"), s.get("inprogress")),
        ])
    return rows


def write_today(sh, rows):
    ws = ensure_tab(sh, "Today", TODAY_HEADERS)
    ws.clear()
    ws.append_row(TODAY_HEADERS, value_input_option="USER_ENTERED")
    # Alerts first (TOP UP is the one to act on), then leads_left ascending.
    # Indices are looked up from the schema so adding a column cannot silently
    # re-sort the tab by the wrong field.
    i_left = TODAY_HEADERS.index("leads_left")
    i_alert = TODAY_HEADERS.index("alert")
    rank = {ALERT_TOP_UP: 0, ALERT_FINISHING: 1, ALERT_IDLE: 2, ALERT_DRY: 3}
    rows_sorted = sorted(rows, key=lambda r: (rank.get(r[i_alert], 3), r[i_left]))
    if rows_sorted:
        ws.append_rows(rows_sorted, value_input_option="USER_ENTERED")


def append_week(sh, week_start, rows):
    """Append today's week-to-date snapshot, REPLACING any snapshot already
    written for today.

    The tab is one-set-of-rows-per-day. A plain append meant a second run on the
    same day left TWO sets for that date, silently doubling every per-day total
    read off the tab (hit on 2026-08-01: a re-run made the day read 2,360 against
    a true 1,180). Re-running must be idempotent, so today's rows are cleared
    before the fresh set goes in."""
    title = f"Week {week_start.isoformat()}"
    ws = ensure_tab(sh, title, WEEK_HEADERS)
    if not rows:
        return title

    # Normalise BOTH sides to an ISO date string before comparing. gspread's
    # get_all_values() returns the date column as a STRING ('2026-07-27') while
    # rows[] carries real date objects, so a raw == never matches. (The Sheets
    # API's own values().get returns the same cells as SERIALS — hence compare on
    # a normalised form, not on whatever one reader happens to hand back.)
    def to_iso(v):
        if isinstance(v, date):
            return v.isoformat()
        s = str(v).strip()
        try:  # tolerate a serial, in case the reader changes under us
            return (date(1899, 12, 30) + timedelta(days=int(float(s)))).isoformat()
        except (ValueError, TypeError):
            return s[:10]

    today_iso = to_iso(rows[0][0])
    existing = ws.get_all_values()
    body = [r for r in existing[1:] if r and str(r[0]).strip()]
    keep = [r for r in body if to_iso(r[0]) != today_iso]

    # Write history + today as ONE contiguous block starting at A2, then blank
    # only the rows below it. A clear-then-write pair was tried first and lost 40
    # rows of live history: the clear and the follow-up write do not settle
    # atomically, and get_all_values() in between can read the emptied grid. A
    # single update over the full block never leaves the tab in a wiped state.
    combined = keep + rows
    last_col = chr(64 + len(WEEK_HEADERS))
    ws.update(values=combined, range_name="A2", value_input_option="USER_ENTERED")
    stale = len(body) - len(combined)
    if stale > 0:  # tab previously held more rows than we just wrote
        first = 2 + len(combined)
        ws.batch_clear([f"A{first}:{last_col}{first + stale + 10}"])
    write_week_total(ws, rows)
    return title


def write_week_total(ws, latest_rows):
    """Pin a WEEK TOTAL summary into the two spare columns to the right of the data.

    WHY: every row on this tab is WEEK-TO-DATE cumulative, and a fresh set is
    appended each day. So selecting the sent_wk column and reading the sum
    DOUBLE-COUNTS massively — on 2026-08-01 the column summed to 7,820 against a
    true week total of 2,360, because Monday's sends are re-listed in every later
    snapshot. Nothing on the tab signalled that, so the naive sum looked authoritative.

    This writes the correct figure (today's snapshot only) where it can't be missed.
    Only ACTIVE campaigns appear on this tab, so a campaign that COMPLETED mid-week
    drops off and this under-reports; the install-KPIs sheet counts every campaign
    regardless of status and is the number to trust for true weekly volume."""
    i_sent = WEEK_HEADERS.index("sent_wk")
    i_repl = WEEK_HEADERS.index("replies_wk")
    i_pos = WEEK_HEADERS.index("positive_replies_wk")

    def col_sum(idx):
        t = 0
        for r in latest_rows:
            try:
                t += int(float(r[idx] or 0))
            except (ValueError, TypeError, IndexError):
                pass
        return t

    anchor = chr(65 + len(WEEK_HEADERS) + 1)  # one blank column gap after the data
    block = [
        ["WEEK TOTAL (latest snapshot)"],
        ["Emails sent", col_sum(i_sent)],
        ["Replies", col_sum(i_repl)],
        ["Positive", col_sum(i_pos)],
        ["ACTIVE campaigns", len(latest_rows)],
        [""],
        ["Rows below are WEEK-TO-DATE cumulative,"],
        ["one set appended per day. DO NOT sum"],
        ["the sent_wk column - it double-counts."],
        ["ACTIVE-only: campaigns completed mid-week"],
        ["drop off. True total = install-KPIs sheet."],
    ]
    # The tab is created exactly len(WEEK_HEADERS) wide, but this summary block
    # sits one gap to the RIGHT of the data, so every new week's tab was born
    # too narrow and the first write of the week died with "exceeds grid limits"
    # (Max columns: 14) — the whole task had been failing since the 3rd. Widen
    # before writing.
    need = len(WEEK_HEADERS) + 1 + max(len(r) for r in block)
    if ws.col_count < need:
        ws.add_cols(need - ws.col_count)
    ws.update(values=block, range_name=f"{anchor}1", value_input_option="USER_ENTERED")


# Main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = date.today()
    today_str = today.isoformat()
    wk_start = week_start_for(today)
    # "End of last week" = Sunday before this week's Monday
    last_week_end = wk_start - timedelta(days=1)

    campaigns_raw = fetch_campaigns()
    active_raw = [c for c in campaigns_raw if c.get("status") == "ACTIVE"]
    stats = collect_active_campaign_stats()
    print(f"\n[i] built lifetime totals for {len(stats)} campaigns")

    sh = None if args.dry_run else open_sheet()

    baselines = load_baselines(sh, wk_start) if sh else {}

    # For campaigns missing a baseline this week, reconstruct it as their
    # lifetime totals through end-of-last-week. For campaigns that didn't
    # exist yet last week, the reconstruction returns zeros (so they appear
    # from day 1 of their first send within this week).
    by_id = {c["id"]: c for c in active_raw}
    missing = [s for s in stats if s["id"] not in baselines]
    if missing:
        print(f"\n[i] reconstructing {len(missing)} baselines at {last_week_end.isoformat()}")
        positives_through = load_positive_replies_by_campaign(through_date=last_week_end)
    new_baseline_rows = []
    captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for s in missing:
        camp = by_id[s["id"]]
        print(f"[i]   baseline: {s['id']} {s['name']}", flush=True)
        b = reconstruct_baseline(camp, last_week_end, positives_through)
        baselines[s["id"]] = b
        new_baseline_rows.append([
            wk_start.isoformat(), s["id"], s["name"],
            b["sent"], b["replies"], b["positive_replies"],
            b["bounces"], b["unsubscribes"], captured_at,
        ])

    today_rows = build_today_rows(stats, today_str)
    week_rows = build_week_rows(stats, baselines, today_str)

    if args.dry_run:
        print(f"\n--- baselines to capture ({len(new_baseline_rows)}) ---")
        for r in new_baseline_rows[:5]:
            print(" ", r)
        if len(new_baseline_rows) > 5:
            print(f"  ... and {len(new_baseline_rows)-5} more")
        print(f"\n--- Today tab ({len(today_rows)}) ---")
        for r in today_rows:
            print(" ", r)
        print(f"\n--- Week {wk_start.isoformat()} rows ({len(week_rows)}) ---")
        for r in week_rows:
            print(" ", r)
        return

    if new_baseline_rows:
        append_baselines(sh, wk_start, new_baseline_rows)
        print(f"[OK] captured {len(new_baseline_rows)} new baselines for week {wk_start.isoformat()}")

    write_today(sh, today_rows)
    week_tab = append_week(sh, wk_start, week_rows)
    print(f"[OK] wrote {len(today_rows)} Today rows + {len(week_rows)} rows to '{week_tab}'")
    print(f"     https://docs.google.com/spreadsheets/d/{SHEET_ID}")


if __name__ == "__main__":
    sys.exit(main() or 0)
