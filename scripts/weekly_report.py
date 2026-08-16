#!/usr/bin/env python3
"""Weekly outbound report — one HTML page, everything that sent.

WHY THIS EXISTS
    Two previous dashboards went stale for the same reason: they read a
    hand-maintained list of campaigns. The campaign-stats LIVE tab was never
    scheduled; REGISTRY.yaml missed 7 of 11 active campaigns (83% of send
    volume). Both looked healthy while being blind.

    So this one derives its campaign list from the Smartlead API every run.
    There is no list to maintain. A campaign that sends appears; one that
    goes quiet drops off and comes back when it sends again.

WHAT IT PRODUCES
    reports/weekly/index.html      the week: totals, per-venture, per-campaign
    reports/weekly/campaign-{id}.html   one page per campaign that sent
    reports/weekly/history.csv     append-only, one row per campaign per week

DRILL PATH
    week total -> campaign row -> that campaign's page -> its positive replies
    with the text the person actually wrote.

USAGE
    python weekly_report.py                 # last complete week (Mon-Sun)
    python weekly_report.py --week 2026-07-20
    python weekly_report.py --this-week     # week in progress
    python weekly_report.py --no-open
"""
import argparse
import csv
import html
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from secrets_util import get_secret

API_KEY = get_secret("SMARTLEAD_API_KEY")
BASE_URL = "https://server.smartlead.ai/api/v1"

SOS = Path(r"C:\Users\Devon\sos")
HOME = SOS.parent
REPLIES_CSV = SOS / "shared" / "data" / "smartlead" / "replies_log.csv"
OUT_DIR = SOS / "shared" / "reports" / "weekly"
HISTORY_CSV = OUT_DIR / "history.csv"

POSITIVE = {"Interested", "Information Request", "Meeting Request"}

# Campaign trees, one per venture. Each keeps its own so the brand firewall
# holds; the report is what joins them. Shape is
#   {product}/segments/{segment}/campaigns/{campaign}/README.md
# See sos/docs/adr/0004.
TREE_ROOTS = [
    HOME / "blue-summit" / "functions" / "outbound",
    SOS / "functions" / "gtm" / "products",
    HOME / "devon-kellar-freelance" / "growth" / "products",
]

# Venture is inferred from the campaign name. Smartlead has no venture field,
# and adding one by hand is exactly the manual step that rotted the registry.
VENTURE_RULES = [
    ("Blue Summit", "Blue Summit"),
    ("DK PVP", "DK Freelance"),
    ("DK Freelance", "DK Freelance"),
    ("B2B DS", "B2B Data Scout"),
    ("B2B Data Scout", "B2B Data Scout"),
    ("Asbestos", "B2B Data Scout"),
    ("NSL", "Smartbound"),
    ("Smartbound", "Smartbound"),
]


def venture_of(name):
    for token, venture in VENTURE_RULES:
        if token.lower() in (name or "").lower():
            return venture
    return "Other"


def api(path, **params):
    params["api_key"] = API_KEY
    r = requests.get(f"{BASE_URL}{path}", params=params, timeout=90)
    r.raise_for_status()
    return r.json()


def monday(d):
    return d - timedelta(days=d.weekday())


def parse_day(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def load_replies(ws, we):
    """Net-new positive leads in the window, keyed by campaign.

    A lead counts ONCE, in the week of their FIRST positive reply, ever.
    If someone replies "interested" in week 1 and again in week 3, week 3
    shows nothing — they are not a new lead, they are the same lead talking
    again. Without this the weekly number silently inflates: across the log
    40% of positive rows are repeat replies from people already counted.

    Dedup is global across all time (not per-week), so it is stable no matter
    which week you rebuild. Same rule the install-KPIs job uses for Leads.
    """
    positives = defaultdict(list)
    reply_counts = defaultdict(int)
    if not REPLIES_CSV.exists():
        return positives, reply_counts

    with open(REPLIES_CSV, encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))

    # all replies (any category) in-window, for the reply-rate denominator
    for row in rows:
        d = parse_day(row.get("reply_date"))
        if d and ws <= d <= we:
            reply_counts[(row.get("campaign_id") or "").strip()] += 1

    # walk every positive reply ever, oldest first, and keep only the first
    # one per lead. Falls back to lead_id when the email is blank.
    pos = [(parse_day(r.get("reply_date")), r) for r in rows
           if (r.get("lead_category") or "").strip() in POSITIVE]
    pos = [(d, r) for d, r in pos if d]
    pos.sort(key=lambda t: t[0])

    seen = set()
    for d, row in pos:
        key = (row.get("lead_email") or "").strip().lower() \
            or f"id:{(row.get('lead_id') or '').strip()}"
        if not key or key == "id:":
            continue
        if key in seen:
            continue                       # already counted in an earlier week
        seen.add(key)
        if ws <= d <= we:
            positives[(row.get("campaign_id") or "").strip()].append(row)

    return positives, reply_counts


def collect(ws, we):
    campaigns = api("/campaigns")
    rows = []
    for c in campaigns:
        cid = str(c["id"])
        try:
            d = api(f"/campaigns/{cid}/analytics-by-date",
                    start_date=str(ws), end_date=str(we))
        except Exception as exc:
            print(f"  [warn] {cid} analytics failed: {exc}")
            continue
        sent = int(d.get("sent_count") or 0)
        if sent == 0:
            continue                        # only campaigns that actually sent
        rows.append({
            "id": cid,
            "name": c.get("name") or f"Campaign {cid}",
            "status": c.get("status") or "",
            "venture": venture_of(c.get("name")),
            "sent": sent,
            "unique_sent": int(d.get("unique_sent_count") or 0),
            "replies": int(d.get("reply_count") or 0),
            "bounces": int(d.get("bounce_count") or 0),
            "unsubs": int(d.get("unsubscribed_count") or 0),
        })
    rows.sort(key=lambda r: -r["sent"])
    return rows


def pct(n, d):
    return round(100.0 * n / d, 2) if d else 0.0


def _frontmatter(path):
    """Parse YAML frontmatter. Returns {} if absent or unparseable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    try:
        import yaml
        return yaml.safe_load(m.group(1)) or {}
    except Exception as exc:
        print(f"  [warn] bad frontmatter in {path}: {exc}")
        return {}


def load_campaign_tree():
    """Index the campaign trees by Smartlead ID.

    Returns (by_id, unlinked_campaigns) where by_id maps a Smartlead ID to the
    campaign's own declared fields plus its parent segment and product.

    A Send with no entry here renders stats only — never a guessed match. That
    is the ADR 0001 rule: silence beats a confident mismatch.
    """
    by_id, unlinked = {}, []
    for root in TREE_ROOTS:
        if not root.exists():
            print(f"  [warn] tree root missing: {root}")
            continue
        for readme in root.glob("*/segments/*/campaigns/*/README.md"):
            camp = _frontmatter(readme)
            if camp.get("type") != "campaign":
                continue
            seg_readme = readme.parent.parent.parent / "README.md"
            prod_readme = seg_readme.parent.parent.parent / "README.md"
            seg = _frontmatter(seg_readme)
            prod = _frontmatter(prod_readme)
            entry = {
                "campaign": camp,
                "segment": seg,
                "product_name": camp.get("product") or prod.get("product")
                                or readme.parents[3].name,
                "segment_name": camp.get("segment") or seg.get("segment")
                                or readme.parents[1].name,
                "campaign_dir": readme.parent,
                "segment_dir": seg_readme.parent,
            }
            ids = camp.get("smartlead_ids") or []
            if not ids:
                unlinked.append(entry)
            for sid in ids:
                by_id[int(sid)] = entry
    return by_id, unlinked


def fetch_copy(campaign_id):
    """Email copy as actually sent, live from the API (ADR 0003).

    Copy can live either directly on the step or inside sequence_variants —
    A/B campaigns put it in the variants and leave the step's own subject and
    body empty, which reads as "no copy" if you only check the step.
    """
    try:
        steps = api(f"/campaigns/{campaign_id}/sequences")
    except Exception as exc:
        print(f"  [warn] {campaign_id} copy fetch failed: {exc}")
        return None
    if not isinstance(steps, list):
        return None

    out = []
    for step in steps:
        delay = (step.get("seq_delay_details") or {}).get("delayInDays")
        variants = step.get("sequence_variants") or []
        if variants:
            for v in variants:
                out.append({
                    "step": step.get("seq_number"),
                    "delay": delay,
                    "label": v.get("variant_label") or "",
                    "subject": (v.get("subject") or "").strip(),
                    "body": (v.get("email_body") or "").strip(),
                })
        else:
            out.append({
                "step": step.get("seq_number"),
                "delay": delay,
                "label": "",
                "subject": (step.get("subject") or "").strip(),
                "body": (step.get("email_body") or "").strip(),
            })
    return out


def fetch_lifetime(campaign_id):
    """Lifetime totals + leads left, live from the API.

    leads_left MUST come from campaign_lead_stats.notStarted. `drafted_count`
    counts queued EMAILS, not leads, so it roughly doubles on a 2-step sequence
    — it once reported 10,289 leads left on a campaign that had 10, so the
    top-up alarm never fired while 275 senders sat attached. See CONTEXT.md.
    """
    try:
        a = api(f"/campaigns/{campaign_id}/analytics")
    except Exception as exc:
        print(f"  [warn] {campaign_id} lifetime fetch failed: {exc}")
        return None
    s = a.get("campaign_lead_stats") or {}
    return {
        "sent": int(a.get("sent_count") or 0),
        "replies": int(a.get("reply_count") or 0),
        "bounces": int(a.get("bounce_count") or 0),
        "unsubscribes": int(a.get("unsubscribed_count") or 0),
        "loaded": int(s.get("total") or 0),
        "left": int(s.get("notStarted") or 0),
        "inprogress": int(s.get("inprogress") or 0),
        "completed": int(s.get("completed") or 0),
    }


def lifetime_new_leads_by_campaign():
    """Lifetime NEW POSITIVE LEADS per campaign, deduped globally. Computed once.

    The API's reply_count is raw replies and is NOT comparable with the weekly
    "new positive leads" figure — putting the two side by side would invite
    exactly the wrong comparison. So this recomputes lifetime leads from the
    reply log using the same first-ever-positive-reply rule (ADR 0003).

    A lead is credited to the campaign that got their FIRST positive reply, so
    a campaign can produce positive replies and still show 0 new leads if those
    people had already replied to something else.
    """
    counts = defaultdict(int)
    if not REPLIES_CSV.exists():
        return counts
    with open(REPLIES_CSV, encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))
    pos = [(parse_day(r.get("reply_date")), r) for r in rows
           if (r.get("lead_category") or "").strip() in POSITIVE]
    pos = sorted([(d, r) for d, r in pos if d], key=lambda t: t[0])
    seen = set()
    for _, row in pos:
        key = (row.get("lead_email") or "").strip().lower() \
            or f"id:{(row.get('lead_id') or '').strip()}"
        if not key or key == "id:" or key in seen:
            continue
        seen.add(key)
        counts[(row.get("campaign_id") or "").strip()] += 1
    return counts


def collect_deliverability():
    """Sender-health summary, reusing smartlead_deliverability.py directly.

    That script already computes this correctly every Sunday, but only ever
    wrote it to a local .txt nobody opened. Rather than reimplement the logic
    (and risk it drifting from the scheduled job), import its functions and
    render the same numbers here.

    Returns None if the module or its data is unavailable — the weekly report
    must still build without it.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import smartlead_deliverability as sd

        replies = sd.load_replies()
        stats = sd.load_stats()
        from_names = sd.load_accounts_api()
        rows = sd.build_rows(replies, stats, from_names)
        domains = sd.domain_summary(rows)
    except Exception as exc:
        print(f"  [warn] deliverability panel skipped: {exc}")
        return None

    # A domain is "dead" when it is actively sending and has never had a reply.
    dead = []
    for dom, d in domains.items():
        if d.get("active_today") and d.get("total_replies", 0) == 0:
            dead.append({
                "domain": dom.strip().strip('"'),
                "accounts": d.get("accounts", 0),
                "active": d.get("active_today", 0),
                "capacity": d.get("send_capacity_per_day", 0),
            })
    dead.sort(key=lambda x: -x["capacity"])

    active_accounts = sum(1 for r in rows if r.get("is_active_today"))
    zero_reply_active = sum(1 for r in rows
                            if r.get("is_active_today") and not r.get("total_replies"))

    return {
        "accounts": len(rows),
        "active_accounts": active_accounts,
        "zero_reply_active": zero_reply_active,
        "domains": len(domains),
        "dead": dead,
        "wasted_per_day": sum(x["capacity"] for x in dead),
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

CSS = """
:root{
  --paper:#FBFAF7;--ink:#17191E;--ink-soft:#3C424B;--neutral:#5D6673;
  --faint:#8C939F;--rule:#DEDDD5;--panel:#FFF;--sunk:#F2F1EC;
  --accent:#2F6F8F;--accent-soft:#E4EDF2;
  --ok:#2E7D57;--ok-bg:#E3F0E9;--warn:#B0761E;--warn-bg:#F7EEDC;
  --bad:#B33A34;--bad-bg:#F7E4E2;
  --mono:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  --slab:"Zilla Slab","Roboto Slab",Rockwell,Georgia,serif;
  --sans:"IBM Plex Sans","Segoe UI",system-ui,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#131519;--ink:#EDEEE9;--ink-soft:#C3C7CD;--neutral:#98A0AC;
  --faint:#6E7681;--rule:#2A2E36;--panel:#1A1D23;--sunk:#22262E;
  --accent:#6FB4D6;--accent-soft:#1E313D;
  --ok:#6FC79B;--ok-bg:#17301F;--warn:#E0AC5B;--warn-bg:#332616;
  --bad:#E8837B;--bad-bg:#331D1C;}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
     font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:clamp(1rem,2.4vw,1.6rem);
      display:flex;flex-direction:column;gap:1.6rem}
a{color:var(--accent)}
.eyebrow{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;
         text-transform:uppercase;color:var(--neutral);margin:0}
header{border-bottom:3px solid var(--ink);padding-bottom:1rem;
       display:flex;flex-direction:column;gap:.4rem}
h1{font-family:var(--slab);font-weight:700;font-size:clamp(1.8rem,4.4vw,2.7rem);
   line-height:1.05;margin:0;letter-spacing:-.015em;text-wrap:balance}
.crumb{font-family:var(--mono);font-size:.75rem;color:var(--neutral);margin:0}
.band{display:grid;gap:1px;background:var(--rule);border:1px solid var(--rule);
      grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.cell{background:var(--panel);padding:.9rem 1rem;display:flex;
      flex-direction:column;gap:.2rem}
.cell .k{font-family:var(--mono);font-size:.64rem;letter-spacing:.1em;
         text-transform:uppercase;color:var(--neutral)}
.cell .v{font-family:var(--slab);font-size:1.8rem;font-weight:700;line-height:1;
         font-variant-numeric:tabular-nums}
.cell .sub{font-size:.76rem;color:var(--faint)}
.v.ok{color:var(--ok)}.v.warn{color:var(--warn)}.v.bad{color:var(--bad)}
.delta{font-family:var(--mono);font-size:.72rem}
.up{color:var(--ok)}.down{color:var(--bad)}
h2.sec{font-family:var(--slab);font-size:1.35rem;margin:0 0 .1rem;
       padding-bottom:.4rem;border-bottom:1px solid var(--rule)}
.lede{margin:0;color:var(--ink-soft);max-width:70ch}
section{display:flex;flex-direction:column;gap:.85rem}
.scroll{overflow-x:auto;border:1px solid var(--rule);background:var(--panel)}
table{border-collapse:collapse;width:100%;min-width:640px;font-size:.87rem}
th{font-family:var(--mono);font-size:.64rem;letter-spacing:.09em;
   text-transform:uppercase;color:var(--neutral);text-align:left;
   padding:.55rem .75rem;border-bottom:1px solid var(--rule);
   background:var(--sunk);white-space:nowrap}
td{padding:.55rem .75rem;border-bottom:1px solid var(--rule);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
td.num,th.num{font-variant-numeric:tabular-nums;text-align:right;font-family:var(--mono)}
tr.total td{font-weight:700;background:var(--sunk)}
.pill{display:inline-block;font-family:var(--mono);font-size:.62rem;
      letter-spacing:.07em;text-transform:uppercase;padding:.18em .45em;
      border-radius:2px;font-weight:600;white-space:nowrap}
.p-ok{background:var(--ok-bg);color:var(--ok)}
.p-warn{background:var(--warn-bg);color:var(--warn)}
.p-bad{background:var(--bad-bg);color:var(--bad)}
.p-off{background:var(--sunk);color:var(--neutral)}
.vt{font-family:var(--mono);font-size:.66rem;color:var(--neutral);
    text-transform:uppercase;letter-spacing:.06em}
.reply{background:var(--panel);border:1px solid var(--rule);
       border-left:4px solid var(--ok);padding:.85rem 1rem;
       display:flex;flex-direction:column;gap:.4rem}
.reply .who{display:flex;flex-wrap:wrap;gap:.5rem;align-items:baseline}
.reply .nm{font-weight:650}
.reply .meta{font-family:var(--mono);font-size:.7rem;color:var(--faint)}
.reply .body{font-size:.88rem;color:var(--ink-soft);white-space:pre-wrap;
             border-top:1px dashed var(--rule);padding-top:.45rem;margin:0;
             max-height:11rem;overflow:auto}
.replies{display:flex;flex-direction:column;gap:.6rem}
.note{background:var(--accent-soft);border:1px solid var(--rule);
      border-left:5px solid var(--accent);padding:.9rem 1.1rem}
.note p{margin:0;font-size:.89rem;color:var(--ink-soft)}
.empty{color:var(--faint);font-style:italic;margin:0}
footer{border-top:1px solid var(--rule);padding-top:.9rem;font-size:.78rem;
       color:var(--faint)}

/* strategy panel */
.facts{display:grid;grid-template-columns:auto 1fr;gap:.45rem 1.1rem;
       font-size:.9rem;align-items:baseline}
.facts dt{font-family:var(--mono);font-size:.66rem;letter-spacing:.09em;
          text-transform:uppercase;color:var(--neutral);white-space:nowrap}
.facts dd{margin:0;color:var(--ink-soft)}
.unver{font-family:var(--mono);font-size:.6rem;letter-spacing:.06em;
       text-transform:uppercase;color:var(--warn);background:var(--warn-bg);
       padding:.1em .35em;border-radius:2px;margin-left:.35rem}
.prose{white-space:pre-wrap;margin:0}

/* room-left bar */
.room{display:flex;flex-direction:column;gap:.4rem}
.bar{height:.55rem;background:var(--sunk);border-radius:2px;overflow:hidden}
.bar span{display:block;height:100%;background:var(--accent)}
.bar span.full{background:var(--bad)}
.bar span.low{background:var(--warn)}

/* copy blocks */
.steps{display:flex;flex-direction:column;gap:.7rem}
.step{border:1px solid var(--rule);background:var(--panel)}
.step header{display:flex;flex-wrap:wrap;gap:.5rem;align-items:baseline;
             background:var(--sunk);padding:.5rem .8rem;border:0;
             border-bottom:1px solid var(--rule)}
.step .n{font-family:var(--mono);font-size:.66rem;letter-spacing:.08em;
         text-transform:uppercase;color:var(--neutral)}
.step .subj{font-weight:650;font-size:.92rem}
.step .em{padding:.7rem .8rem;font-size:.87rem;color:var(--ink-soft);
          max-height:22rem;overflow:auto}
.step .em p{margin:0 0 .6rem}

/* two-column week vs lifetime */
td.wk{font-variant-numeric:tabular-nums;text-align:right;font-family:var(--mono);
      font-weight:650}
"""


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><div class='wrap'>{body}</div></body></html>")


def status_pill(s):
    cls = {"ACTIVE": "p-ok", "PAUSED": "p-warn", "COMPLETED": "p-off",
           "DRAFTED": "p-off"}.get(s, "p-off")
    return f"<span class='pill {cls}'>{html.escape(s or '?')}</span>"


def _strategy_block(entry, cid):
    """Segment strategy + campaign narrative, from the tree (ADR 0001)."""
    if not entry:
        return ("<section><h2 class='sec'>Strategy</h2>"
                "<p class='empty'>No campaign doc linked. Add "
                f"<code>smartlead_ids: [{cid}]</code> to a campaign README under "
                "one of the product trees and this panel fills in.</p></section>")

    seg, camp = entry["segment"], entry["campaign"]
    aud = seg.get("audience") or {}
    review = set(seg.get("needs_review") or [])

    def flag(field):
        return "<span class='unver'>unverified</span>" if field in review else ""

    facts = [
        ("Product", html.escape(str(entry["product_name"])), ""),
        ("Segment", html.escape(str(entry["segment_name"]))
                    + (f" &middot; <span class='vt'>{html.escape(str(seg.get('axis')))}</span>"
                       if seg.get("axis") else ""), ""),
    ]
    if camp.get("variant"):
        facts.append(("Variant", html.escape(str(camp["variant"])), ""))
    if aud.get("definition"):
        facts.append(("Who", html.escape(str(aud["definition"]).strip()),
                      flag("audience.definition")))
    # A count is only shown alongside its definition (ADR 0002).
    if aud.get("count") and aud.get("definition"):
        facts.append(("Audience", f"{int(aud['count']):,}", flag("audience.count")))
    if aud.get("geo"):
        facts.append(("Geo", html.escape(", ".join(str(g) for g in aud["geo"])), ""))
    if seg.get("list_source"):
        facts.append(("List", html.escape(str(seg["list_source"]).strip()),
                      flag("list_source")))

    dl = "".join(f"<dt>{k}</dt><dd>{v}{f}</dd>" for k, v, f in facts)

    notes = ""
    if aud.get("notes"):
        notes = ("<h3 class='eyebrow'>Audience notes</h3>"
                 f"<p class='prose lede'>{html.escape(str(aud['notes']).strip())}</p>")

    related = ""
    if seg.get("related"):
        related = ("<p class='lede'><strong>Related:</strong> "
                   + ", ".join(f"<code>{html.escape(str(r))}</code>"
                               for r in seg["related"]) + "</p>")

    return f"""<section>
  <h2 class='sec'>Who we sent it to</h2>
  <dl class='facts'>{dl}</dl>
  {notes}
  {related}
</section>"""


def _copy_block(steps):
    if steps is None:
        return ("<section><h2 class='sec'>The copy</h2>"
                "<p class='empty'>Copy unavailable &mdash; the Smartlead "
                "sequence API did not respond for this campaign.</p></section>")
    if not steps:
        return ("<section><h2 class='sec'>The copy</h2>"
                "<p class='empty'>No sequence steps found. The campaign may "
                "have been deleted from Smartlead.</p></section>")
    out = []
    for s in steps:
        delay = s.get("delay")
        when = "day 0" if delay in (0, None) else f"+{delay}d"
        label = f" &middot; variant {html.escape(str(s['label']))}" if s.get("label") else ""
        subj = html.escape(s["subject"]) if s["subject"] else "<em>(no subject)</em>"
        out.append(
            "<div class='step'><header>"
            f"<span class='n'>Email {s.get('step') or '?'} &middot; {when}{label}</span>"
            f"<span class='subj'>{subj}</span></header>"
            f"<div class='em'>{s['body'] or '<p><em>(empty body)</em></p>'}</div></div>")
    return ("<section><h2 class='sec'>The copy</h2>"
            "<p class='lede'>Pulled live from Smartlead, so this is what "
            "actually sent &mdash; including edits made in the UI.</p>"
            "<div class='steps'>" + "".join(out) + "</div></section>")


def _totals_block(row, week_pos, life, life_leads):
    """Week beside lifetime, plus the room-left verdict."""
    if not life:
        return ""
    rows = [
        ("Sent", f"{row['sent']:,}", f"{life['sent']:,}"),
        ("Replies", str(row["replies"]), str(life["replies"])),
        ("New positive leads", str(week_pos),
         str(life_leads) if life_leads is not None else "&mdash;"),
        ("Bounces", str(row["bounces"]), str(life["bounces"])),
    ]
    trs = "".join(f"<tr><td>{k}</td><td class='wk'>{w}</td><td class='num'>{l}</td></tr>"
                  for k, w, l in rows)

    loaded, left = life["loaded"], life["left"]
    used = loaded - left
    frac = int(100 * used / loaded) if loaded else 0
    if left == 0 and loaded:
        cls, verdict = "full", "MAXED OUT &mdash; no leads left to send"
    elif left < 50:
        cls, verdict = "low", f"RUNNING DRY &mdash; only {left} leads left"
    else:
        cls, verdict = "", f"ROOM TO SEND &mdash; {left:,} leads still to go"

    return f"""<section>
  <h2 class='sec'>This week vs lifetime</h2>
  <div class='scroll'><table>
    <thead><tr><th>Metric</th><th class='num'>This week</th><th class='num'>Lifetime</th></tr></thead>
    <tbody>{trs}</tbody>
  </table></div>
  <div class='room'>
    <div class='bar'><span class='{cls}' style='width:{frac}%'></span></div>
    <p class='lede'><strong>{verdict}.</strong> {used:,} of {loaded:,} loaded leads sent ({frac}%).</p>
  </div>
</section>"""


def render_campaign(row, positives, ws, we, stamp, entry=None,
                    copy_steps=None, life=None, life_leads=None):
    pos = positives.get(row["id"], [])
    cards = []
    for p in sorted(pos, key=lambda r: r.get("reply_date") or ""):
        nm = " ".join(x for x in [p.get("lead_first_name"), p.get("lead_last_name")] if x) or p.get("lead_email", "")
        co = p.get("lead_company") or ""
        d = parse_day(p.get("reply_date"))
        body = (p.get("reply_body") or "").strip()
        if len(body) > 1500:
            body = body[:1500] + " …"
        cards.append(
            "<div class='reply'><div class='who'>"
            f"<span class='nm'>{html.escape(nm)}</span>"
            + (f"<span class='meta'>{html.escape(co)}</span>" if co else "")
            + f"<span class='pill p-ok'>{html.escape(p.get('lead_category',''))}</span>"
            + f"<span class='meta'>{d or ''}</span>"
            + f"<span class='meta'>{html.escape(p.get('lead_email',''))}</span>"
            "</div>"
            + (f"<p class='body'>{html.escape(body)}</p>" if body else "")
            + "</div>")

    reply_block = ("<div class='replies'>" + "".join(cards) + "</div>") if cards \
        else "<p class='empty'>No new positive leads from this campaign this week.</p>"

    body = f"""
<header>
  <p class='crumb'><a href='index.html'>&larr; Week of {ws:%d %b %Y}</a></p>
  <p class='eyebrow'>{html.escape(row['venture'])} &middot; Smartlead ID {row['id']} &middot; {status_pill(row['status'])}</p>
  <h1>{html.escape(row['name'])}</h1>
</header>
<div class='band'>
  <div class='cell'><span class='k'>Sent</span><span class='v'>{row['sent']:,}</span><span class='sub'>{row['unique_sent']:,} unique leads</span></div>
  <div class='cell'><span class='k'>Replies</span><span class='v'>{row['replies']}</span><span class='sub'>{pct(row['replies'], row['sent'])}% of sent</span></div>
  <div class='cell'><span class='k'>New positive leads</span><span class='v ok'>{len(pos)}</span><span class='sub'>first positive reply only</span></div>
  <div class='cell'><span class='k'>Bounces</span><span class='v {"bad" if pct(row["bounces"],row["sent"])>3 else "ok"}'>{row['bounces']}</span><span class='sub'>{pct(row['bounces'], row['sent'])}%</span></div>
</div>

{_totals_block(row, len(pos), life, life_leads)}

{_strategy_block(entry, row['id'])}

<section>
  <h2 class='sec'>New positive leads &mdash; what they actually wrote</h2>
  <p class='lede'>First-time positive repliers only. Someone who had already replied positively in an earlier week is not listed again.</p>
  {reply_block}
</section>

{_copy_block(copy_steps)}

<footer>Week {ws:%d %b} &ndash; {we:%d %b %Y}. Sending stats, copy and lifetime totals pulled live from the Smartlead API; reply text from replies_log.csv; strategy from the campaign tree. Built {stamp}.</footer>
"""
    return page(f"{row['name']} — week of {ws:%d %b}", body)


def render_index(rows, positives, ws, we, prev, stamp, deliv=None, tree=None, unlinked=None):
    tot_sent = sum(r["sent"] for r in rows)
    tot_rep = sum(r["replies"] for r in rows)
    tot_bnc = sum(r["bounces"] for r in rows)
    tot_pos = sum(len(positives.get(r["id"], [])) for r in rows)

    def delta(cur, was):
        if not was:
            return ""
        ch = round(100.0 * (cur - was) / was)
        cls = "up" if ch >= 0 else "down"
        return f"<span class='delta {cls}'>{'+' if ch>=0 else ''}{ch}% vs prior week</span>"

    # per-venture rollup
    vt = defaultdict(lambda: {"sent": 0, "rep": 0, "pos": 0, "n": 0})
    for r in rows:
        v = vt[r["venture"]]
        v["sent"] += r["sent"]; v["rep"] += r["replies"]
        v["pos"] += len(positives.get(r["id"], [])); v["n"] += 1
    vrows = "".join(
        f"<tr><td>{html.escape(k)}</td><td class='num'>{d['n']}</td>"
        f"<td class='num'>{d['sent']:,}</td><td class='num'>{d['rep']}</td>"
        f"<td class='num'>{pct(d['rep'], d['sent'])}%</td>"
        f"<td class='num'>{d['pos']}</td></tr>"
        for k, d in sorted(vt.items(), key=lambda kv: -kv[1]["sent"]))

    crows = []
    for r in rows:
        np_ = len(positives.get(r["id"], []))
        br = pct(r["bounces"], r["sent"])
        crows.append(
            f"<tr><td><a href='campaign-{r['id']}.html'>{html.escape(r['name'])}</a><br>"
            f"<span class='vt'>{html.escape(r['venture'])}</span> {status_pill(r['status'])}</td>"
            f"<td class='num'>{r['sent']:,}</td>"
            f"<td class='num'>{r['replies']}</td>"
            f"<td class='num'>{pct(r['replies'], r['sent'])}%</td>"
            f"<td class='num'>{'<strong>'+str(np_)+'</strong>' if np_ else '0'}</td>"
            f"<td class='num'>{r['bounces']}</td>"
            f"<td class='num'>{'<span class=\"pill p-bad\">'+str(br)+'%</span>' if br>3 else str(br)+'%'}</td></tr>")
    crows.append(
        f"<tr class='total'><td>TOTAL &mdash; {len(rows)} campaigns</td>"
        f"<td class='num'>{tot_sent:,}</td><td class='num'>{tot_rep}</td>"
        f"<td class='num'>{pct(tot_rep, tot_sent)}%</td><td class='num'>{tot_pos}</td>"
        f"<td class='num'>{tot_bnc}</td><td class='num'>{pct(tot_bnc, tot_sent)}%</td></tr>")

    # every positive reply this week, newest first
    allpos = []
    for r in rows:
        for p in positives.get(r["id"], []):
            allpos.append((p, r))
    allpos.sort(key=lambda t: t[0].get("reply_date") or "", reverse=True)
    pcards = []
    for p, r in allpos:
        nm = " ".join(x for x in [p.get("lead_first_name"), p.get("lead_last_name")] if x) or p.get("lead_email", "")
        co = p.get("lead_company") or ""
        d = parse_day(p.get("reply_date"))
        body = (p.get("reply_body") or "").strip()
        if len(body) > 900:
            body = body[:900] + " …"
        pcards.append(
            "<div class='reply'><div class='who'>"
            f"<span class='nm'>{html.escape(nm)}</span>"
            + (f"<span class='meta'>{html.escape(co)}</span>" if co else "")
            + f"<span class='pill p-ok'>{html.escape(p.get('lead_category',''))}</span>"
            + f"<span class='meta'>{d or ''}</span>"
            f"<span class='meta'>&rarr; <a href='campaign-{r['id']}.html'>{html.escape(r['name'][:44])}</a></span>"
            "</div>"
            + (f"<p class='body'>{html.escape(body)}</p>" if body else "")
            + "</div>")
    pblock = ("<div class='replies'>" + "".join(pcards) + "</div>") if pcards \
        else "<p class='empty'>No new positive leads this week.</p>"

    # ---- drift: what the tree is missing --------------------------------
    # The anti-drift mechanism from ADR 0004. Both previous dashboards died
    # silently; this one says every week what it cannot see.
    missing = [r for r in rows if not tree.get(int(r["id"]))]
    drift_bits = []
    if missing:
        lis = "".join(
            f"<tr><td>{html.escape(r['name'])}</td>"
            f"<td class='num'>{r['id']}</td>"
            f"<td class='num'>{r['sent']:,}</td></tr>" for r in missing)
        drift_bits.append(
            f"<p class='lede'><strong>{len(missing)} campaign"
            f"{'s' if len(missing)!=1 else ''} sent this week with no campaign "
            "doc linked.</strong> They show stats only &mdash; no strategy, no "
            "audience, no related campaigns. Add <code>smartlead_ids</code> to a "
            "campaign README under one of the product trees to fix.</p>"
            "<div class='scroll'><table><thead><tr><th>Campaign</th>"
            "<th class='num'>Smartlead ID</th><th class='num'>Sent</th></tr></thead>"
            f"<tbody>{lis}</tbody></table></div>")
    if unlinked:
        names = ", ".join(f"<code>{html.escape(str(e['segment_name']))}/"
                          f"{html.escape(e['campaign_dir'].name)}</code>"
                          for e in unlinked[:10])
        drift_bits.append(
            f"<p class='lede'><strong>{len(unlinked)} campaign folder"
            f"{'s' if len(unlinked)!=1 else ''} declare no Smartlead ID:</strong> "
            f"{names}. Either the send has not launched yet, or the ID was never "
            "recorded.</p>")

    drift_block = ""
    if drift_bits:
        drift_block = ("<section><h2 class='sec'>Needs attention</h2>"
                       + "".join(drift_bits) + "</section>")

    # ---- deliverability -------------------------------------------------
    if deliv:
        zr_pct = pct(deliv["zero_reply_active"], deliv["active_accounts"])
        drows = "".join(
            f"<tr><td>{html.escape(x['domain'])}</td>"
            f"<td class='num'>{x['accounts']}</td>"
            f"<td class='num'>{x['active']}</td>"
            f"<td class='num'>{x['capacity']}</td></tr>"
            for x in deliv["dead"][:15])
        more = (f"<p class='lede'>Showing the 15 highest-volume of "
                f"{len(deliv['dead'])} dead domains. Full list: "
                f"<code>shared/reports/smartlead-deliverability/</code>.</p>"
                if len(deliv["dead"]) > 15 else "")
        dead_tbl = (f"""<div class='scroll'><table>
    <thead><tr><th>Dead domain</th><th class='num'>Accounts</th><th class='num'>Active</th><th class='num'>Cap/day</th></tr></thead>
    <tbody>{drows}</tbody>
  </table></div>{more}""" if deliv["dead"]
                    else "<p class='empty'>No dead domains. Every sending domain has replies.</p>")

        deliv_block = f"""
<section>
  <h2 class='sec'>Sender health</h2>
  <p class='lede'>Whether the mailboxes doing the sending are actually landing. A domain is <strong>dead</strong> when it is sending today and has never once received a reply &mdash; the signature of a burnt domain.</p>
  <div class='band'>
    <div class='cell'><span class='k'>Sending accounts</span><span class='v'>{deliv['active_accounts']:,}</span><span class='sub'>of {deliv['accounts']:,} tracked</span></div>
    <div class='cell'><span class='k'>Zero-reply active</span><span class='v {"bad" if zr_pct > 25 else "warn"}'>{deliv['zero_reply_active']}</span><span class='sub'>{zr_pct}% of sending accounts</span></div>
    <div class='cell'><span class='k'>Dead domains</span><span class='v {"bad" if deliv['dead'] else "ok"}'>{len(deliv['dead'])}</span><span class='sub'>of {deliv['domains']} total</span></div>
    <div class='cell'><span class='k'>Wasted capacity</span><span class='v bad'>{deliv['wasted_per_day']:,}</span><span class='sub'>emails/day into a void</span></div>
  </div>
  {dead_tbl}
</section>"""
    else:
        deliv_block = ""

    body = f"""
<header>
  <p class='eyebrow'>Outbound weekly &middot; all ventures</p>
  <h1>Week of {ws:%d %B %Y}</h1>
  <p class='lede'>{ws:%d %b} &ndash; {we:%d %b}. Every campaign that sent, whatever its status. The campaign list comes from the Smartlead API each run, so nothing can be missed by forgetting to register it.</p>
</header>

<div class='band'>
  <div class='cell'><span class='k'>Emails sent</span><span class='v'>{tot_sent:,}</span><span class='sub'>{delta(tot_sent, prev.get('sent',0)) or f"{len(rows)} campaigns"}</span></div>
  <div class='cell'><span class='k'>Replies</span><span class='v'>{tot_rep}</span><span class='sub'>{pct(tot_rep, tot_sent)}% reply rate</span></div>
  <div class='cell'><span class='k'>New positive leads</span><span class='v ok'>{tot_pos}</span><span class='sub'>{delta(tot_pos, prev.get('pos',0)) or 'first positive reply only'}</span></div>
  <div class='cell'><span class='k'>Bounces</span><span class='v {"bad" if pct(tot_bnc,tot_sent)>3 else "ok"}'>{tot_bnc}</span><span class='sub'>{pct(tot_bnc, tot_sent)}% &mdash; {"investigate" if pct(tot_bnc,tot_sent)>3 else "healthy"}</span></div>
</div>

<section>
  <h2 class='sec'>By venture</h2>
  <div class='scroll'><table>
    <thead><tr><th>Venture</th><th class='num'>Campaigns</th><th class='num'>Sent</th><th class='num'>Replies</th><th class='num'>Reply %</th><th class='num'>New leads</th></tr></thead>
    <tbody>{vrows}</tbody>
  </table></div>
</section>

<section>
  <h2 class='sec'>By campaign</h2>
  <p class='lede'>Click any campaign to see its positive replies in full.</p>
  <div class='scroll'><table>
    <thead><tr><th>Campaign</th><th class='num'>Sent</th><th class='num'>Replies</th><th class='num'>Reply %</th><th class='num'>New leads</th><th class='num'>Bounces</th><th class='num'>Bounce %</th></tr></thead>
    <tbody>{''.join(crows)}</tbody>
  </table></div>
</section>

{deliv_block}

<section>
  <h2 class='sec'>New positive leads this week &mdash; {tot_pos}</h2>
  <p class='lede'>People whose <strong>first ever</strong> positive reply landed this week &mdash; interested, information request, or meeting request &mdash; and the text they wrote. Someone who already replied in an earlier week is not counted again here, so this is net-new interest, never the same person twice.</p>
  {pblock}
</section>

{drift_block}

<footer>
  Sending stats, copy and lifetime totals pulled live from the Smartlead API at
  build time; reply text from
  <code>shared/data/smartlead/replies_log.csv</code> (refreshed 08:00 + 18:00 daily);
  strategy from the campaign trees.
  Running history appended to <code>shared/reports/weekly/history.csv</code>.
  Built {stamp}.
</footer>
"""
    return page(f"Outbound week of {ws:%d %b %Y}", body)


# --------------------------------------------------------------------------

def append_history(rows, positives, ws, tree=None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tree = tree or {}
    cols = ["week_start", "campaign_id", "campaign_name", "venture",
            "product", "segment", "status",
            "sent", "unique_sent", "replies", "new_positive_leads", "bounces",
            "reply_rate", "bounce_rate", "captured_at"]
    existing = []
    if HISTORY_CSV.exists():
        with open(HISTORY_CSV, encoding="utf-8", errors="replace") as fh:
            existing = [r for r in csv.DictReader(fh)
                        if r.get("week_start") != str(ws)]      # idempotent re-run
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    new = []
    for r in rows:
        e = tree.get(int(r["id"])) or {}
        new.append({
            "week_start": str(ws), "campaign_id": r["id"], "campaign_name": r["name"],
            "venture": r["venture"],
            "product": e.get("product_name", ""), "segment": e.get("segment_name", ""),
            "status": r["status"], "sent": r["sent"],
            "unique_sent": r["unique_sent"], "replies": r["replies"],
            "new_positive_leads": len(positives.get(r["id"], [])),
            "bounces": r["bounces"],
            "reply_rate": pct(r["replies"], r["sent"]),
            "bounce_rate": pct(r["bounces"], r["sent"]), "captured_at": stamp,
        })
    with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(existing + new)
    return len(new)


def prior_week_totals(ws):
    if not HISTORY_CSV.exists():
        return {}
    prev = str(ws - timedelta(days=7))
    sent = pos = 0
    with open(HISTORY_CSV, encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if r.get("week_start") == prev:
                sent += int(r.get("sent") or 0)
                pos += int(r.get("new_positive_leads") or r.get("positive") or 0)
    return {"sent": sent, "pos": pos}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="Monday of the week, YYYY-MM-DD")
    ap.add_argument("--this-week", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if a.week:
        ws = monday(datetime.strptime(a.week, "%Y-%m-%d").date())
    elif a.this_week:
        ws = monday(date.today())
    else:
        ws = monday(date.today()) - timedelta(days=7)     # last complete week
    we = ws + timedelta(days=6)

    print(f"[i] week {ws} -> {we}")
    print("[i] discovering campaigns from Smartlead API ...")
    rows = collect(ws, we)
    print(f"[i] {len(rows)} campaigns sent this week")

    positives, _ = load_replies(ws, we)
    tot_pos = sum(len(positives.get(r["id"], [])) for r in rows)
    tot_sent = sum(r["sent"] for r in rows)
    print(f"[i] {tot_sent:,} sent, {tot_pos} positive replies")

    print("[i] reading campaign trees ...")
    tree, unlinked = load_campaign_tree()
    linked = sum(1 for r in rows if tree.get(int(r["id"])))
    print(f"[i] {len(tree)} campaigns indexed | {linked}/{len(rows)} of this "
          f"week's sends linked to a doc")

    n = append_history(rows, positives, ws, tree)
    print("[i] building sender-health panel ...")
    deliv = collect_deliverability()
    prev = prior_week_totals(ws)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("[i] fetching copy + lifetime totals per campaign ...")
    life_leads = lifetime_new_leads_by_campaign()      # one pass, not one per campaign
    for r in rows:
        cid = int(r["id"])
        (OUT_DIR / f"campaign-{r['id']}.html").write_text(
            render_campaign(r, positives, ws, we, stamp,
                            entry=tree.get(cid),
                            copy_steps=fetch_copy(cid),
                            life=fetch_lifetime(cid),
                            life_leads=life_leads.get(str(cid))),
            encoding="utf-8")
    idx = OUT_DIR / "index.html"
    idx.write_text(render_index(rows, positives, ws, we, prev, stamp, deliv, tree, unlinked), encoding="utf-8")

    # keep a dated snapshot so past weeks stay reachable
    (OUT_DIR / f"week-{ws}.html").write_text(
        render_index(rows, positives, ws, we, prev, stamp, deliv, tree, unlinked), encoding="utf-8")

    print(f"[OK] {len(rows)} campaigns | {tot_sent:,} sent | {tot_pos} positive | {n} history rows")
    print(f"     {idx}")
    if not a.no_open and sys.platform == "win32":
        subprocess.run(["cmd", "/c", "start", "", str(idx)], check=False)


if __name__ == "__main__":
    main()
