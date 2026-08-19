#!/usr/bin/env python3
"""
build_site.py — the reporting site that replaces the Google Sheets.
  ############################################################################
  # VENDORED COPY. The original lives in the freelance repo at              #
  #   functions/growth/reporting/build_site.py                              #
  # and this copy exists because Actions can only see THIS repo.            #
  #                                                                         #
  # Two copies of a file is exactly the drift that hid 9 people from the    #
  # CRM (four copies of an install allowlist, at 15/22/25 entries). Edit the #
  # freelance one and re-copy; never edit only this. check_vendored.py in   #
  # this repo fails CI if they diverge, so drift is loud instead of silent. #
  ############################################################################

THE RULE: every number is a link. "23 replies this week" opens the 23 replies,
with who, which campaign, and what they wrote. No number exists on this site
that you cannot open.

That rule is the whole point. The sheets it replaces showed totals with no way
to see the rows behind them, so a number that looked wrong could not be checked
without going back to the API by hand.

Reads Supabase directly through the gtm-jobs seam (scripts/replies_store.py),
so the page is live rather than a nightly snapshot and there is no sheet to
fall out of sync with.

    python build_site.py              build into out/
    python build_site.py --open       build and open it

Deploy with deploy_site.py (Cloudflare Pages, same pattern as the morning brief).
"""

import argparse
import collections
import datetime as dt
import html
import json
import os
import sys
import webbrowser
from pathlib import Path

# Windows pipes default to cp1252; a single smart quote in a reply body would
# otherwise crash the build. Same failure that hid ApolloSync for months.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).parent
OUT = HERE / "out"
# replies_store lives in the gtm-jobs repo. Two layouts must both work:
#   - laptop: this file is in devon-kellar-freelance, gtm-jobs is a sibling
#   - CI:     this file IS in gtm-jobs, next to scripts/
# Resolved by looking rather than hardcoding, so the same file runs in both.
for _cand in (HERE / "scripts",                       # vendored into gtm-jobs
              HERE.parent / "scripts",                # gtm-jobs/reporting/
              Path(os.environ.get("GTM_JOBS_SCRIPTS", "")),
              Path(r"C:\Users\Devon\gtm-jobs\scripts")):
    if _cand and (_cand / "replies_store.py").exists():
        sys.path.insert(0, str(_cand))
        break
else:
    sys.exit("[FAIL] cannot find replies_store.py - set GTM_JOBS_SCRIPTS")
os.environ.setdefault("REPLIES_BACKEND", "supabase")
import replies_store as rs  # noqa: E402

import site_shell  # noqa: E402  -- the shared nav; see its docstring

POSITIVE = ("Interested", "Information Request", "Meeting Request")
NEGATIVE = ("Not Interested", "Do Not Contact", "Wrong Person")
AUTOMATED = ("Out Of Office", "Bounce", "Uncategorized", "Uncategorizable")

# Lift-installer campaigns are SD Lifts client delivery, not Devon's pipeline.
# Same rule as the CRM sync — matched on name so a new one needs no code edit.
CLIENT_PATTERNS = ("LIFT INSTALLER", "LIFT PERMIT", "LIFTS UK")


def is_client(name):
    u = (name or "").upper()
    return any(p in u for p in CLIENT_PATTERNS)


def esc(s):
    return html.escape(str(s or ""))


def as_date(s):
    s = str(s or "")
    return s[:10] if len(s) >= 10 else ""


# Where a reply stops being the reply and becomes the thread quoted back at us.
# Bodies averaged ~1 KB each and were 2.4 MB of a 2.8 MB page; almost all of it
# is the chain below the actual message. Cutting at the quote marker keeps what
# the person wrote and drops what we already sent them.
QUOTE_MARKERS = (
    "\nOn ", "\r\nOn ", "-----Original Message", "________________________",
    "\nFrom: ", "\r\nFrom: ", "> On ", "wrote:",
)


def unquote(body, cap=900):
    """The reply itself, without the quoted thread underneath it."""
    t = str(body or "").strip()
    cut = len(t)
    for m in QUOTE_MARKERS:
        i = t.find(m)
        # only trust a marker that appears after a bit of real text, else a
        # reply that literally opens with "On..." would be erased entirely
        if 40 < i < cut:
            cut = i
    return t[:cut].strip()[:cap]


def monday(d):
    return d - dt.timedelta(days=d.weekday())


# ---- data -------------------------------------------------------------------

def load():
    rows = rs.load_all()
    for r in rows:
        r["_date"] = as_date(r.get("reply_date"))
        r["_client"] = is_client(r.get("campaign_name"))
        cat = r.get("lead_category") or "Uncategorized"
        r["_cat"] = cat
        r["_positive"] = cat in POSITIVE
    return rows


def buckets(rows, today):
    """Week / month / all, on DATED rows only.

    Undated rows are excluded from every time window on purpose: they are not
    reply events at all but lead STATUS records (one per campaign+lead), and
    counting them in "this week" would inflate every figure on the page.
    """
    wk, mo = monday(today), today.replace(day=1)
    dated = [r for r in rows if r["_date"]]
    return {
        "week": [r for r in dated if r["_date"] >= wk.isoformat()],
        "month": [r for r in dated if r["_date"] >= mo.isoformat()],
        "all": dated,
    }


# ---- render -----------------------------------------------------------------

CSS = """
:root{--paper:#F6EFE3;--card:#FFFBF4;--ink:#2B2118;--mut:#6B5B4A;--rule:#DCCEB8;
--terra:#B4552D;--ok:#3F6B3F;--okb:#DFEBDD;--hot:#9B3226;--hotb:#F5DAD5;
--inf:#8A6A1F;--infb:#F4E9CC;--neg:#6B5B4A;--negb:#EDE4D6}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--paper:#191512;--card:#211C17;--ink:#EFE6D8;--mut:#AD9C87;--rule:#3A3129;
--terra:#E08A5C;--ok:#8FBF8A;--okb:#1E2C1E;--hot:#E28878;--hotb:#331C18;
--inf:#D9B75E;--infb:#2E2716;--neg:#AD9C87;--negb:#26211B}}
:root[data-theme=dark]{--paper:#191512;--card:#211C17;--ink:#EFE6D8;--mut:#AD9C87;
--rule:#3A3129;--terra:#E08A5C;--ok:#8FBF8A;--okb:#1E2C1E;--hot:#E28878;
--hotb:#331C18;--inf:#D9B75E;--infb:#2E2716;--neg:#AD9C87;--negb:#26211B}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);margin:0;
font:15px/1.6 Karla,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:30px 20px 80px}
h1{font-size:2.1rem;margin:0 0 3px;letter-spacing:-.02em}
h2{font-size:1.15rem;margin:34px 0 12px;letter-spacing:-.01em}
.sub{color:var(--mut);margin:0 0 24px;font-size:.92rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:13px}
.stat{background:var(--card);border:1px solid var(--rule);box-shadow:3px 3px 0 var(--rule);
padding:14px 16px;text-decoration:none;color:inherit;display:block;cursor:pointer}
.stat:hover{box-shadow:5px 5px 0 var(--terra);transform:translate(-1px,-1px)}
.stat b{font-size:1.9rem;color:var(--terra);display:block;line-height:1.05}
.stat span{font-size:.71rem;text-transform:uppercase;letter-spacing:.07em;color:var(--mut)}
.stat em{font-style:normal;font-size:.72rem;color:var(--mut);display:block;margin-top:3px}
.bar{background:var(--card);border-left:4px solid var(--terra);padding:12px 16px;
box-shadow:3px 3px 0 var(--rule);margin:20px 0}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--rule);box-shadow:3px 3px 0 var(--rule)}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--rule);
font-size:.88rem;vertical-align:top}
th{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);
cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--terra)}
tr:last-child td{border-bottom:none}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--paper)}
.pill{font-size:.68rem;font-weight:700;padding:2px 7px;border-radius:3px;white-space:nowrap}
.p-ok{background:var(--okb);color:var(--ok)}.p-hot{background:var(--hotb);color:var(--hot)}
.p-inf{background:var(--infb);color:var(--inf)}.p-neg{background:var(--negb);color:var(--neg)}
.scroll{overflow-x:auto}
.tools{display:flex;gap:9px;flex-wrap:wrap;margin:16px 0 10px;align-items:center}
input[type=search],select{background:var(--card);color:var(--ink);border:1px solid var(--rule);
padding:7px 10px;font:inherit;font-size:.86rem}
input[type=search]{flex:1;min-width:190px}
.count{color:var(--mut);font-size:.82rem}
.body{white-space:pre-wrap;font-size:.85rem;background:var(--paper);
border:1px solid var(--rule);padding:11px 13px;margin-top:8px;max-height:300px;overflow:auto}
.hide{display:none}
.nm{font-weight:700}.em{color:var(--mut);font-size:.8rem}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--rule);
color:var(--mut);font-size:.8rem}
"""

JS = """
function pill(c){return c==='Meeting Request'?'p-hot':c==='Interested'?'p-ok':
c==='Information Request'?'p-inf':'p-neg';}
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;
return d.innerHTML;}
var SORT={col:'reply_date',dir:-1};
function apply(){
  var q=(document.getElementById('q').value||'').toLowerCase();
  var cat=document.getElementById('cat').value;
  var win=document.getElementById('win').value;
  var camp=document.getElementById('camp').value;
  var rows=DATA.filter(function(r){
    if(win!=='all'&&r.win.indexOf(win)<0)return false;
    if(cat==='positive'){if(!r.pos)return false;}
    else if(cat&&r.lead_category!==cat)return false;
    if(camp&&r.campaign_name!==camp)return false;
    if(q){var h=(r.lead_first_name+' '+r.lead_last_name+' '+r.lead_email+' '+
      r.lead_company+' '+r.campaign_name+' '+r.reply_body).toLowerCase();
      if(h.indexOf(q)<0)return false;}
    return true;});
  rows.sort(function(a,b){var x=a[SORT.col]||'',y=b[SORT.col]||'';
    return x<y?-SORT.dir:x>y?SORT.dir:0;});
  document.getElementById('n').textContent=rows.length+' repl'+(rows.length===1?'y':'ies');
  var h='';
  for(var i=0;i<rows.length;i++){var r=rows[i];
    h+='<tr onclick="tog('+i+')"><td>'+esc(r.reply_date||'—')+'</td>'+
      '<td><span class=nm>'+esc((r.lead_first_name+' '+r.lead_last_name).trim()||'—')+
      '</span><br><span class=em>'+esc(r.lead_email)+'</span></td>'+
      '<td>'+esc(r.lead_company)+'</td>'+
      '<td>'+esc(r.campaign_name)+'</td>'+
      '<td><span class="pill '+pill(r.lead_category)+'">'+esc(r.lead_category)+
      '</span></td></tr>'+
      '<tr id=b'+i+' class=hide><td colspan=5><div class=body>'+
      esc(r.reply_body|| (r.pos?'(no reply text recorded)'
        :'Body not stored for '+r.lead_category+' replies.'))+
      '</div></td></tr>';}
  document.getElementById('rows').innerHTML=h||
    '<tr><td colspan=5 style="color:var(--mut)">Nothing matches.</td></tr>';
}
function tog(i){var e=document.getElementById('b'+i);
  e.className=e.className==='hide'?'':'hide';}
function sortBy(c){SORT.dir=(SORT.col===c)?-SORT.dir:-1;SORT.col=c;apply();}
function setw(w,c){document.getElementById('win').value=w;
  document.getElementById('cat').value=c||'';
  document.getElementById('camp').value='';
  document.getElementById('q').value='';apply();
  document.getElementById('tbl').scrollIntoView({behavior:'smooth'});}
"""


def render(rows, today):
    b = buckets(rows, today)
    own = [r for r in rows if not r["_client"]]
    ob = buckets(own, today)

    def pos(rs_):
        return [r for r in rs_ if r["_positive"]]

    def meet(rs_):
        return [r for r in rs_ if r["_cat"] == "Meeting Request"]

    # payload: dated own-pipeline rows, newest first
    payload = []
    wk, mo = monday(today).isoformat(), today.replace(day=1).isoformat()
    for r in sorted((r for r in own if r["_date"]),
                    key=lambda x: x["reply_date"] or "", reverse=True):
        win = ["all"]
        if r["_date"] >= mo:
            win.append("month")
        if r["_date"] >= wk:
            win.append("week")
        payload.append({
            "reply_date": r.get("reply_date") or "",
            "lead_first_name": r.get("lead_first_name") or "",
            "lead_last_name": r.get("lead_last_name") or "",
            "lead_email": r.get("lead_email") or "",
            "lead_company": r.get("lead_company") or "",
            "campaign_name": r.get("campaign_name") or "",
            "lead_category": r["_cat"],
            # Bodies only for the replies anyone opens. Bounces and
            # out-of-offices are machine text; carrying 2,000 of them was
            # most of the page weight and none of the value.
            "reply_body": (unquote(r.get("reply_body")) if r["_positive"]
                           else ""),
            "pos": r["_positive"],
            "win": win,
        })

    camps = sorted({p["campaign_name"] for p in payload})
    cats = sorted({p["lead_category"] for p in payload})

    stats = [
        ("week", "", len(ob["week"]), "replies this week", "all categories"),
        ("week", "positive", len(pos(ob["week"])), "positive this week", "interested / info / meeting"),
        ("week", "Meeting Request", len(meet(ob["week"])), "meetings this week", "asked to talk"),
        ("month", "positive", len(pos(ob["month"])), "positive this month",
         "since " + today.replace(day=1).strftime("%d %b").lstrip("0")),
        ("all", "positive", len(pos(ob["all"])), "positive all time", f"of {len(ob['all'])} dated replies"),
    ]

    cards = "".join(
        f'<a class=stat onclick="setw(\'{w}\',\'{c}\')">'
        f"<b>{n}</b><span>{esc(lab)}</span><em>{esc(sub)}</em></a>"
        for w, c, n, lab, sub in stats)

    opts_camp = "".join(f'<option value="{esc(c)}">{esc(c[:60])}</option>' for c in camps)
    opts_cat = "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in cats)

    client_n = len([r for r in rows if r["_client"] and r["_date"]])
    undated = len([r for r in rows if not r["_date"]])

    # The nav comes from site_shell so this page and the domain-health page cannot
    # drift apart. Only the nav is shared: this page keeps its own CSS and layout,
    # because rewriting a working page to fit a shell is a change with no upside.
    return f"""<title>Replies &middot; Ops</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<style>{CSS}{site_shell.NAV_CSS}</style>
{site_shell.nav("replies")}
<div class=wrap>
<h1>Replies</h1>
<p class=sub>Live from Supabase &middot; built {today.isoformat()} &middot;
every number below opens the rows behind it</p>

<div class=grid>{cards}</div>

<div class=bar><b>Click any number.</b> It filters the table to exactly those
replies &mdash; then click a row to read what the person actually wrote.</div>

<h2 id=tbl>Replies</h2>
<div class=tools>
  <input type=search id=q placeholder="Search name, company, campaign, reply text&hellip;"
    oninput=apply()>
  <select id=win onchange=apply()>
    <option value=week>This week</option>
    <option value=month>This month</option>
    <option value=all selected>All time</option>
  </select>
  <select id=cat onchange=apply()>
    <option value="">Every category</option>
    <option value=positive>Positive only</option>
    {opts_cat}
  </select>
  <select id=camp onchange=apply()>
    <option value="">Every campaign</option>
    {opts_camp}
  </select>
  <span class=count id=n></span>
</div>
<div class=scroll><table>
<thead><tr>
  <th onclick="sortBy('reply_date')">Date</th>
  <th onclick="sortBy('lead_last_name')">Who</th>
  <th onclick="sortBy('lead_company')">Company</th>
  <th onclick="sortBy('campaign_name')">Campaign</th>
  <th onclick="sortBy('lead_category')">Category</th>
</tr></thead>
<tbody id=rows></tbody>
</table></div>

<footer>
{len(payload)} dated replies across {len(camps)} campaigns.
{undated} undated rows are excluded from every count on this page: they are lead
status records (one per campaign+lead), not reply events, and counting them
would inflate every figure. {client_n} lift-installer replies are excluded too
&mdash; those are SD Lifts client delivery, not this pipeline.
</footer>
</div>
<script>var DATA={json.dumps(payload, ensure_ascii=False)};{JS}
document.getElementById('win').value='all';apply();</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    today = dt.date.today()
    rows = load()
    OUT.mkdir(parents=True, exist_ok=True)
    page = OUT / "index.html"
    page.write_text(render(rows, today), encoding="utf-8")

    kb = page.stat().st_size / 1024
    print(f"[OK] {page}  ({kb:.0f} KB, {len(rows)} source rows)")
    if args.open:
        webbrowser.open(page.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
