#!/usr/bin/env python3
"""
build_deliverability.py — the domain-health page of the ops site.

    python build_deliverability.py            build into out/deliverability.html
    python build_deliverability.py --open

Sibling of build_site.py. That one answers "who replied"; this one answers
"can we still send at all". They share a nav, a stylesheet and a deploy, and
nothing else -- neither imports the other's data path.

WHY IT LIVES HERE AND NOT IN functions/growth/deliverability/
-------------------------------------------------------------
The checker (`deliverability/check.py`) OWNS the data: it queries DNS, scores,
and writes `deliverability.domain_checks`. This file only READS the result and
draws it. Keeping the drawing here means the ops site has one build, one deploy
and one set of styles, while the checker stays a headless job that a scheduled
task can run without a browser anywhere in the picture.

The reverse split -- a page builder inside the checker folder -- would put two
deploy targets in the repo and guarantee they drift.

READS `deliverability.domain_health`, the view that already resolves
latest-check-per-domain. Never re-derive that join here.
"""

import argparse
import datetime as dt
import html
import json
import os
import sys
import webbrowser
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).parent
OUT = HERE / "out"

# The registry's own access layer, pinned to the `deliverability` schema.
# Imported rather than reimplemented: ddb.py is what keeps this code off the
# hiring pipeline's tables in `public`.
# VENDORED LAYOUT. In the freelance repo the checker lives one level up in
# functions/growth/deliverability/; here it is vendored into a subfolder so
# Actions can see it. Prefer the vendored copy, fall back to the original.
DELIV = HERE / "deliverability"
if not DELIV.exists():
    DELIV = HERE.parent / "deliverability"
sys.path.insert(0, str(DELIV))
import ddb  # noqa: E402
from sync_registrars import CHECK_SCOPE, STATUSES  # noqa: E402  -- one list, shared

import site_shell  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

# The page ships the anon key, so the build needs it from the same places every other
# script here reads credentials.
for _p in (HERE.parents[2] / ".env",
           Path(r"C:\Users\Devon\sos\shared\config\.env")):
    if _p.exists():
        load_dotenv(_p, override=False)


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def load():
    """The registry, plus the detail of each domain's latest check."""
    rows = ddb.select("domain_health", {
        "select": "domain,registrar,status,client,purpose,expires_at,auto_renew,"
                  "mailbox_count,days_to_expiry,checked_at,score,band,"
                  "gate_tripped,top_action",
        "order": "domain", "limit": 1000,
    })

    # One pass, newest first -- the first row seen per domain is its latest check.
    latest = {}
    for c in ddb.select("domain_checks", {
            "select": "domain,checked_at,spf,dkim,dmarc,mx,ip_blacklists,"
                      "domain_blacklists,age_days,smartlead",
            "order": "checked_at.desc", "limit": 1000}):
        latest.setdefault(c["domain"], c)

    # Inbox placement, per provider. A separate question from the DNS check and a
    # separate table: `check.py` proves a domain is CONFIGURED right, placement
    # proves mail from it actually lands. A domain can pass every DNS check and
    # still be 0% at Microsoft -- 20 of them are, on 2026-08-19.
    place = {p["domain"]: p for p in ddb.select("domain_placement_latest", {
        "select": "domain,google_inbox_pct,google_spam_pct,google_seeds,google_band,"
                  "ms_inbox_pct,ms_spam_pct,ms_seeds,ms_band,tested_at",
        "limit": 1000})}

    for r in rows:
        # `None` here means NEVER TESTED, and the page must say so rather than
        # render a blank that reads as "fine". Untested is a third state, not a
        # missing value.
        r["place"] = place.get(r["domain"])
        c = latest.get(r["domain"])
        r["detail"] = None
        if not c:
            continue
        dbl = c.get("domain_blacklists") or {}
        r["detail"] = {
            "spf": (c.get("spf") or {}).get("status"),
            "spf_policy": (c.get("spf") or {}).get("policy"),
            "dkim": (c.get("dkim") or {}).get("status"),
            "dkim_sel": (c.get("dkim") or {}).get("selectors"),
            "dmarc": (c.get("dmarc") or {}).get("status"),
            "dmarc_policy": (c.get("dmarc") or {}).get("policy"),
            "mx": (c.get("mx") or {}).get("status"),
            "provider": (c.get("mx") or {}).get("provider"),
            "surbl": [l.get("blacklist") for l in (dbl.get("listings") or [])],
            "surbl_lists": [x for l in (dbl.get("listings") or [])
                            for x in (l.get("lists") or [])],
            "blocked": dbl.get("blocked_lists") or [],
            "age_days": c.get("age_days"),
            "sl": c.get("smartlead") or {},
        }
    return rows


def unregistered():
    """Domains that SEND but the registry has never heard of.

    The registry is built from two registrar APIs. A domain registered anywhere
    else is invisible to it -- and therefore never checked -- while still having
    live mailboxes in Smartlead. 25 of them on 2026-08-18, four with a failing
    mailbox. A page that showed only the registry would quietly imply the estate
    is fully covered, so this gap is stated rather than omitted.

    Degrades to [] without a Smartlead key: the rest of the page is still true,
    and a build that dies over a missing optional key is worse than one that
    says less.
    """
    try:
        sys.path.insert(0, str(DELIV))
        import smartlead
        sl = smartlead.by_domain()
    except (SystemExit, Exception) as e:            # noqa: BLE001
        print(f"  ! no Smartlead ({type(e).__name__}) - "
              f"skipping the unregistered-senders panel")
        return []

    known = {r["domain"] for r in ddb.select("domains",
                                             {"select": "domain", "limit": 1000})}
    out = []
    for d in sorted(set(sl) - known):
        e = sl[d]
        out.append({
            "domain": d,
            "mailboxes": e["mailbox_count"],
            "dead": sum(1 for m in e["mailboxes"]
                        if not m["smtp_ok"] or not m["imap_ok"]),
        })
    return out


CSS = """
.dl-alert{background:var(--card);border:1px solid var(--rule);
border-left:4px solid var(--hot);box-shadow:3px 3px 0 var(--rule);
padding:15px 18px;margin:0 0 18px}
.dl-alert.warn{border-left-color:var(--inf)}
.dl-alert h3{margin:0 0 6px;font-size:1.05rem;color:var(--hot);letter-spacing:-.01em}
.dl-alert.warn h3{color:var(--inf)}
.dl-alert p{margin:0;font-size:.88rem;color:var(--mut);max-width:76ch}
.dl-doms{display:flex;flex-wrap:wrap;gap:5px;margin-top:11px}
.dl-doms code{font:inherit;font-size:.76rem;font-weight:700;background:var(--paper);
border:1px solid var(--rule);padding:2px 7px;border-radius:3px}
.dl-doms code.dead{border-color:var(--hot);color:var(--hot)}
.dl-why{margin-top:10px!important;padding-top:9px;border-top:1px solid var(--rule);
font-size:.8rem!important}
td.sev{width:4px;padding:0}
tr.b-RED td.sev{background:var(--hot)}
tr.b-AMBER td.sev{background:var(--inf)}
tr.b-GREEN td.sev{background:var(--ok)}
.sc{font-weight:700;font-variant-numeric:tabular-nums}
.sc.b-RED{color:var(--hot)}.sc.b-AMBER{color:var(--inf)}.sc.b-GREEN{color:var(--ok)}
.gate{font-size:.72rem;color:var(--hot);font-weight:700;margin-top:2px}
.norenew{font-size:.7rem;color:var(--inf);font-weight:700}
.dl-detail{background:var(--paper);padding:14px 16px}
.dl-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:12px}
.dl-card{background:var(--card);border:1px solid var(--rule);padding:11px 13px}
.dl-card h4{margin:0 0 7px;font-size:.68rem;text-transform:uppercase;
letter-spacing:.07em;color:var(--mut)}
.kv{display:flex;justify-content:space-between;gap:10px;font-size:.83rem;padding:2px 0}
.kv span{color:var(--mut)}
.kv b{text-align:right}
.kv b.ok{color:var(--ok)}.kv b.bad{color:var(--hot)}.kv b.mid{color:var(--inf)}
.dl-blocked{grid-column:1/-1;font-size:.8rem;color:var(--mut);
border-left:3px solid var(--inf);background:var(--card);padding:8px 11px}
.mut{color:var(--mut)}
td.cl{white-space:nowrap}
.clsel{font:inherit;font-size:.8rem;padding:3px 6px;max-width:170px;
background:var(--card);color:var(--ink);border:1px solid var(--rule);border-radius:2px}
.clsel:hover{border-color:var(--terra)}
.clsel.st-retired,.clsel.st-expired{color:var(--hot)}
.clsel.st-parked{color:var(--mut)}
.clsel.st-active{color:var(--ok)}
.saved{font-size:.7rem;margin-left:6px;color:var(--mut)}
.saved.ok{color:var(--ok);font-weight:700}
.saved.bad{color:var(--hot);font-weight:700}
td.pl{white-space:nowrap;font-variant-numeric:tabular-nums}
.seeds{font-size:.68rem;color:var(--mut);margin-top:1px}
.untested{font-size:.74rem;font-style:italic}
"""

JS = """
var OPEN={};
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;
return d.innerHTML;}
var SORT={col:'score',dir:1};

/* The client dropdown. Its own cell, and clicks must NOT bubble to the row (the row
   toggles the detail panel, so a stray bubble would open a panel on every edit). */
function sel(d){
  var o='<option value="">unassigned</option>';
  for(var i=0;i<CLIENTS.length;i++){
    o+='<option'+(CLIENTS[i]===d.client?' selected':'')+'>'+esc(CLIENTS[i])+'</option>';
  }
  o+='<option value="__new">+ new client...</option>';
  return '<select class="clsel" data-d="'+esc(d.domain)+'" onchange="setClient(this)">'
    +o+'</select><span class="saved" id="s-'+esc(d.domain)+'"></span>';
}

function stsel(d){
  var o='';
  for(var i=0;i<STATUSES.length;i++){
    o+='<option'+(STATUSES[i]===d.status?' selected':'')+'>'+STATUSES[i]+'</option>';
  }
  return '<select class="clsel st-'+esc(d.status)+'" data-d="'+esc(d.domain)
    +'" onchange="setStatus(this)">'+o+'</select>';
}

function setClient(el){
  var dom=el.dataset.d, val=el.value;
  if(val==='__new'){
    var name=(prompt('New client name for '+dom+':')||'').trim();
    if(!name){ el.value=curClient(dom)||''; return; }
    val=name;
    if(CLIENTS.indexOf(val)<0){ CLIENTS.push(val); CLIENTS.sort(); }
  }
  save(dom, 'client', val || null, el);
}

/* Status is a real decision, not a label: it decides what the weekly checker looks
   at. Retiring a domain that still has live mailboxes takes it OUT of check scope
   while it carries on sending, so that combination is confirmed rather than assumed.
   The database stamps status_set_by='human' on any change from here, so the sync
   proposes but never overwrites it afterwards. */
function setStatus(el){
  var dom=el.dataset.d, val=el.value, row=rowOf(dom);
  // Warn for ANY status outside CHECK_SCOPE, not a hand-written list. The first
  // version named retired/parked and missed `expired` -- 15 domains with 29 live
  // mailboxes were taken out of the weekly check with no warning at all, because
  // `expired` simply was not in the tuple. Deriving it from CHECK_SCOPE means a new
  // status is covered the day it is added instead of the day someone remembers.
  if(CHECK_SCOPE.indexOf(val)<0 && row && row.mailbox_count>0){
    if(!confirm(dom+' still has '+row.mailbox_count+' live mailbox(es). '
      +'Marking it '+val+' takes it OUT of the weekly deliverability check while '
      +'it can still send. Continue?')){
      el.value=row.status; return;
    }
  }
  // `expired` means "past expires_at, we no longer own it" -- it is what the SYNC
  // writes on a lapse, not a way to say "stop using this". Setting it by hand on a
  // domain that has not lapsed leaves the sync proposing a reversal every run.
  if(val==='expired' && row && !row.expires_at){
    if(!confirm(dom+' has no expiry date on record, so nothing has lapsed. '
      +'`expired` is for domains we no longer own -- if you mean "stop using this", '
      +'`retired` is the one that says so. Set expired anyway?')){
      el.value=row.status; return;
    }
  }
  save(dom, 'status', val, el);
}

function rowOf(dom){
  for(var i=0;i<DATA.length;i++){ if(DATA[i].domain===dom) return DATA[i]; }
  return null;
}

function save(dom, field, val, el){
  var flag=document.getElementById('s-'+dom);
  var body={}; body[field]=val;
  var was=rowOf(dom)?rowOf(dom)[field]:null;
  flag.textContent='saving...'; flag.className='saved';
  fetch(REST+'/domains?domain=eq.'+encodeURIComponent(dom),{
    method:'PATCH',
    headers:{'apikey':KEY,'Authorization':'Bearer '+KEY,
             'Content-Type':'application/json','Content-Profile':'deliverability',
             'Prefer':'return=minimal'},
    body:JSON.stringify(body)
  }).then(function(r){
    if(!r.ok) throw new Error('HTTP '+r.status);
    /* Update the in-memory row so a re-render (sort, filter, expand) does not snap
       the dropdown back to the stale value. */
    var row=rowOf(dom); if(row) row[field]=val;
    flag.textContent='saved'; flag.className='saved ok';
    setTimeout(function(){ flag.textContent=''; },1600);
    apply();
  }).catch(function(e){
    /* Say what went wrong and put the value back. A dropdown that silently fails is
       worse than one that cannot edit at all -- you would believe the change landed. */
    flag.textContent='FAILED: '+e.message; flag.className='saved bad';
    el.value=was==null?'':was;
  });
}
function curClient(dom){ var r=rowOf(dom); return r?r.client:null; }

function kv(l,v,c){return '<div class="kv"><span>'+l+'</span><b class="'+(c||'')+'">'
+esc(v)+'</b></div>';}

/* Placement filter. "Safe" is the measured GREEN band (>=90% inbox, <2% spam),
   never an assumption -- an untested domain matches only `untested`, so asking for
   "safe for Microsoft" can never hand back a domain nobody has measured. */
function plmatch(d,mode){
  var p=d.place;
  if(mode==='untested') return !p || (p.google_inbox_pct==null&&p.ms_inbox_pct==null);
  if(!p) return false;
  var g=p.google_band, m=p.ms_band;
  if(mode==='g-ok')    return g==='GREEN';
  if(mode==='m-ok')    return m==='GREEN';
  if(mode==='both-ok') return g==='GREEN'&&m==='GREEN';
  if(mode==='m-bad')   return m==='RED'||m==='AMBER';
  if(mode==='g-bad')   return g==='RED'||g==='AMBER';
  return true;
}

/* One placement cell: inbox% at ONE provider, or an explicit "not tested".

   Never tested is NOT the same as tested-and-fine, and a blank cell would read as
   the latter. It renders as the word `untested`, so a domain nobody has measured
   can never be mistaken for one that passed. */
function plcell(d,which){
  var p=d.place;
  if(!p) return '<td class="pl"><span class="mut untested">untested</span></td>';
  var pct=p[which==='google'?'google_inbox_pct':'ms_inbox_pct'];
  var n  =p[which==='google'?'google_seeds':'ms_seeds'];
  var bnd=p[which==='google'?'google_band':'ms_band'];
  if(pct==null) return '<td class="pl"><span class="mut untested">untested</span></td>';
  /* Seed counts are small by nature (7-8 per provider per test). Show n so a 100%
     off 8 seeds is not read with the same confidence as 100% off 200. */
  return '<td class="pl"><span class="sc b-'+bnd+'">'+pct+'%</span>'
    +'<div class="seeds">n='+n+'</div></td>';
}

/* The placement card. Always rendered, even with no data -- "never tested" is the
   finding in that case, and omitting the card would hide it. */
function placecard(d){
  var p=d.place;
  if(!p||(p.google_inbox_pct==null&&p.ms_inbox_pct==null)){
    return '<div class="dl-card"><h4>Inbox placement</h4>'
      +kv('Google','never tested','mid')+kv('Microsoft','never tested','mid')
      +'<div class="seeds">No seed test has ever covered this domain, so nothing '
      +'here is known either way.</div></div>';
  }
  var line=function(lbl,pct,spam,n,bnd){
    if(pct==null) return kv(lbl,'never tested','mid');
    return kv(lbl,pct+'% inbox / '+spam+'% spam (n='+n+')',
      bnd==='GREEN'?'ok':(bnd==='AMBER'?'mid':'bad'));
  };
  var when=p.tested_at?String(p.tested_at).slice(0,10):'-';
  return '<div class="dl-card"><h4>Inbox placement</h4>'
    +line('Google',p.google_inbox_pct,p.google_spam_pct,p.google_seeds,p.google_band)
    +line('Microsoft',p.ms_inbox_pct,p.ms_spam_pct,p.ms_seeds,p.ms_band)
    +kv('Last tested',when,'')
    +'</div>';
}

function detail(d){
  var v=d.detail;
  /* An unchecked domain (parked/retired, no DNS check) may still have placement
     history worth reading, so the panel opens with just that card rather than
     returning nothing. */
  if(!v) return '<div class="dl-detail"><div class="dl-cards">'
    +placecard(d)+'</div></div>';
  var yn=function(s,g){return s===g?'ok':(s==='UNKNOWN'?'mid':'bad');};
  var sl=v.sl||{};
  var auth=kv('SPF',v.spf==='PRESENT'?(v.spf_policy||'present'):(v.spf||'-'),yn(v.spf,'PRESENT'))
    +kv('DKIM',v.dkim==='PRESENT'?((v.dkim_sel||[]).join(', ')||'present'):(v.dkim||'-'),yn(v.dkim,'PRESENT'))
    +kv('DMARC',v.dmarc==='PRESENT'?(v.dmarc_policy||'present'):(v.dmarc||'-'),yn(v.dmarc,'PRESENT'));
  var nb=(v.surbl||[]).length;
  var rep=kv('SURBL',nb?((v.surbl_lists||[]).join(', ')||'listed'):'clean',nb?'bad':'ok')
    +kv('MX',v.provider||v.mx||'-',v.mx==='PRESENT'?'ok':'bad')
    +kv('Age',v.age_days!=null?v.age_days+' days':'-',
        v.age_days>=365?'ok':(v.age_days<30?'mid':''));
  var warm = sl.status==='CHECKED'
    ? kv('Mailboxes',sl.mailboxes||0,'')
      +kv('Failing',sl.dead_mailboxes||0,sl.dead_mailboxes?'bad':'ok')
      +kv('Warmup',sl.warmup_days!=null?sl.warmup_days+' days':'-','')
      +kv('Reputation',sl.worst_reputation!=null?sl.worst_reputation+'%':'too new to grade',
          sl.worst_reputation==null?'mid':(sl.worst_reputation>=95?'ok':'bad'))
      +kv('Spam rate',sl.spam_rate!=null?(sl.spam_rate*100).toFixed(2)+'%':'-',
          sl.spam_rate>0.03?'bad':'ok')
    : kv('Mailboxes','none - nothing sends from this domain','mid');
  var blk=(v.blocked||[]).length
    ? '<div class="dl-blocked"><b>Reported nothing, not &ldquo;clean&rdquo;:</b> '
      +v.blocked.map(esc).join(', ')+' refused our queries. Spamhaus blocks public '
      +'resolvers, so those lists contributed no information either way.</div>' : '';
  return '<div class="dl-detail"><div class="dl-cards">'
    +'<div class="dl-card"><h4>Authentication</h4>'+auth+'</div>'
    +'<div class="dl-card"><h4>Reputation &amp; infrastructure</h4>'+rep+'</div>'
    +'<div class="dl-card"><h4>Warmup (Smartlead)</h4>'+warm+'</div>'
    +placecard(d)
    +blk+'</div></div>';
}

function apply(){
  var q=(document.getElementById('q').value||'').toLowerCase();
  var st=document.getElementById('st').value;
  var bd=document.getElementById('bd').value;
  var pl=document.getElementById('pl').value;
  var rows=DATA.filter(function(d){
    if(st&&d.status!==st)return false;
    if(bd==='NONE'){if(d.band)return false;}
    else if(bd&&d.band!==bd)return false;
    if(pl&&!plmatch(d,pl))return false;
    if(q){var h=(d.domain+' '+(d.client||'')+' '
      +(d.top_action||'')+' '+(d.registrar||'')+' '+d.status).toLowerCase();
      if(h.indexOf(q)<0)return false;}
    return true;
  });
  rows.sort(function(a,b){
    var x=a[SORT.col],y=b[SORT.col];
    /* Placement lives on a nested object, so it needs pulling out before it can
       be compared -- sorting on the bare column name would compare undefined to
       undefined and silently do nothing. */
    if(SORT.col==='google_inbox'||SORT.col==='ms_inbox'){
      var f=SORT.col==='google_inbox'?'google_inbox_pct':'ms_inbox_pct';
      x=a.place?a.place[f]:null; y=b.place?b.place[f]:null;
      /* Untested sorts LAST in both directions. It is not a good score and not a
         bad one -- treating null as 0 would put every untested domain at the top
         of a worst-first list and bury the domains actually measured as failing. */
      if(x==null)return 1; if(y==null)return -1;
      return (x-y)*SORT.dir;
    }
    /* unchecked domains have no score: park them last rather than let a null
       sort as if it were zero and lead the worst-first list. */
    if(SORT.col==='score'||SORT.col==='days_to_expiry'){
      x=(x==null?1e9:x); y=(y==null?1e9:y);
    }
    if(typeof x==='string')return x.localeCompare(y)*SORT.dir;
    return (x-y)*SORT.dir;
  });
  document.getElementById('count').textContent=rows.length+' of '+DATA.length+' domains';
  var h='';
  for(var i=0;i<rows.length;i++){
    var d=rows[i], b=d.band||'', o=OPEN[d.domain];
    h+='<tr class="b-'+b+'" onclick="tog(this,\\''+d.domain.replace(/'/g,"\\\\'")+'\\')">'
      +'<td class="sev"></td>'
      +'<td><span class="nm">'+esc(d.domain)+'</span>'
+'</td>'
      +'<td class="cl" onclick="event.stopPropagation()">'+sel(d)+'</td>'
      +'<td class="em">'+esc(d.registrar||'-')+'</td>'
      +'<td class="sc b-'+b+'">'+(d.score!=null?d.score:'<span class="mut">-</span>')+'</td>'
      +'<td>'+(b?'<span class="pill '+(b==='RED'?'p-hot':b==='AMBER'?'p-inf':'p-ok')
        +'">'+b+'</span>':'<span class="mut">unchecked</span>')+'</td>'
      +plcell(d,'google')+plcell(d,'ms')
      +'<td class="cl" onclick="event.stopPropagation()">'+stsel(d)+'</td>'
      +'<td class="em">'+(d.mailbox_count||'')+'</td>'
      +'<td>'+(d.top_action?esc(d.top_action):'<span class="mut">'
        +(b?'':'not in check scope')+'</span>')
      +(d.gate_tripped?'<div class="gate">'
        +esc(d.gate_tripped.replace('soft:','').replace(/_/g,' '))+'</div>':'')+'</td>'
      +'<td class="em">'+(d.days_to_expiry!=null?d.days_to_expiry+'d':'-')
      +(d.auto_renew===false?'<div class="norenew">no auto-renew</div>':'')+'</td></tr>';
    if(o){h+='<tr class="det"><td colspan="12">'+detail(d)+'</td></tr>';}
  }
  document.getElementById('rows').innerHTML=h;
}
function tog(tr,dom){OPEN[dom]=!OPEN[dom];apply();}
function sortby(c){if(SORT.col===c){SORT.dir*=-1;}else{SORT.col=c;SORT.dir=1;}apply();}
document.addEventListener('DOMContentLoaded',function(){
  ['q','st','bd','pl'].forEach(function(id){
    var el=document.getElementById(id);
    el.addEventListener(el.tagName==='SELECT'?'change':'input',apply);
  });
  apply();
});
"""


def render(rows, gap, today):
    checked = [r for r in rows if r["band"]]
    counts = {b: sum(1 for r in checked if r["band"] == b)
              for b in ("RED", "AMBER", "GREEN")}
    blacklisted = [r for r in checked if r["gate_tripped"] == "domain_blacklisted"]
    stamp = max((r["checked_at"] for r in checked if r["checked_at"]), default=None)

    stats = [
        ("RED", counts["RED"], "red", "blacklisted, or missing SPF/DKIM"),
        ("AMBER", counts["AMBER"], "amber", "works, but not trusted yet"),
        ("GREEN", counts["GREEN"], "green", "authenticated and clean"),
        ("NONE", len(rows) - len(checked), "unchecked", "parked or retired"),
    ]
    cards = "".join(
        f'<a class="stat" href="#" onclick="document.getElementById(\'bd\').value='
        f'\'{k}\';apply();return false;">'
        f'<b>{n}</b><span>{lbl}</span><em>{note}</em></a>'
        for k, n, lbl, note in stats)

    alerts = ""
    if blacklisted:
        alerts += (
            '<div class="dl-alert"><h3>'
            f'{len(blacklisted)} domains you were sending from are blacklisted</h3>'
            '<p>Each was <b>active</b> with live mailboxes and 170&ndash;300 days of '
            'warmup &mdash; healthy by every warmup signal. They are on SURBL&rsquo;s '
            '<b>ABUSE</b> list. The sync dropped them to <b>warming</b>, which is the '
            'most it may do: delisting or retiring is a human call.</p>'
            '<div class="dl-doms">'
            + "".join(f"<code>{esc(r['domain'])}</code>" for r in blacklisted)
            + '</div><p class="dl-why"><b>Verified before it was believed.</b> SURBL '
              'returns <code>127.0.0.64</code> for these and NXDOMAIN for invented '
              'control domains, confirmed through three independent resolvers. '
              '<code>64</code> is the ABUSE bit in SURBL&rsquo;s published bitmask, '
              'not a blocked-query code.</p></div>')

    # Placement, stated before the table. A domain that passes every DNS check and
    # still cannot reach Microsoft is invisible in the band column, because the band
    # only grades configuration. It has to be said in words or it is not said.
    ms_dead = sorted(
        (r for r in rows
         if (r.get("place") or {}).get("ms_band") in ("RED", "AMBER")),
        key=lambda r: r["place"]["ms_inbox_pct"])
    sending = [r for r in rows
               if r["status"] in ("new", "warming", "active")
               and (r.get("mailbox_count") or 0) > 0]
    never = [r for r in sending
             if not (r.get("place") or {}).get("google_inbox_pct")
             and not (r.get("place") or {}).get("ms_inbox_pct")]

    if ms_dead:
        both_ok = sum(1 for r in rows
                      if (r.get("place") or {}).get("ms_band") == "GREEN"
                      and (r.get("place") or {}).get("google_band") == "GREEN")
        alerts += (
            '<div class="dl-alert"><h3>'
            f'{len(ms_dead)} domains reach Google but not Microsoft</h3>'
            '<p>These are not weak domains, they are <b>split</b> domains: mail from '
            'them inboxes at Google and goes to spam at Office365. A blended '
            'placement number hides this entirely &mdash; the smartbound.* pool reads '
            '52% overall, which sounds like &ldquo;warm it up&rdquo;, when it is '
            'really <b>96% Google / 6% Microsoft</b>. Those need opposite decisions, '
            'so placement is stored and shown per provider and never averaged.</p>'
            '<div class="dl-doms">'
            + "".join(
                f'<code class="dead">{esc(r["domain"])} &middot; '
                f'{r["place"]["ms_inbox_pct"]}%</code>' for r in ms_dead[:28])
            + '</div><p class="dl-why">Sending to a Microsoft-hosted recipient from '
              'one of these is a wasted send. <b>' + str(both_ok) + ' domains are '
              'measured GREEN at both providers</b> &mdash; route Microsoft '
              'recipients through those. Filter the table by <b>Safe for '
              'Microsoft</b> to list them.</p></div>')

    if never:
        mbx = sum(r.get("mailbox_count") or 0 for r in never)
        alerts += (
            '<div class="dl-alert warn"><h3>'
            f'{len(never)} sending domains have never been placement tested</h3>'
            f'<p>They hold <b>{mbx} live mailboxes</b> and pass the DNS check, but '
            'nothing has ever measured where their mail actually lands. A DNS check '
            'cannot answer that &mdash; every domain in the alert above is '
            'authenticated correctly and still fails at Microsoft. <b>Untested is '
            'shown as <em>untested</em>, never as a blank</b>, so it cannot be read '
            'as a pass.</p><div class="dl-doms">'
            + "".join(
                f'<code>{esc(r["domain"])} &middot; {r.get("mailbox_count") or 0} mbx'
                '</code>' for r in sorted(
                    never, key=lambda r: -(r.get("mailbox_count") or 0))[:28])
            + '</div><p class="dl-why">Run <code>/placement-test</code> against the '
              'campaigns these send from, then <code>placement.py</code> to fold the '
              'result in here.</p></div>')

    if gap:
        dead = sum(g["dead"] for g in gap)
        alerts += (
            '<div class="dl-alert warn"><h3>'
            f'{len(gap)} domains are sending, but are not in the registry</h3>'
            '<p>They have live mailboxes in Smartlead and are at <b>neither Porkbun '
            'nor name.com</b>, so they are registered somewhere the sync cannot see '
            'and <b>nothing has ever checked them</b>.'
            + (f' {dead} of their mailboxes are failing right now.' if dead else '')
            + ' The registry claims to hold every domain we own; for these it does '
              'not.</p><div class="dl-doms">'
            + "".join(
                f'<code{" class=\"dead\"" if g["dead"] else ""}>{esc(g["domain"])}'
                + (f' &middot; {g["dead"]} failing' if g["dead"] else '')
                + '</code>' for g in gap)
            + '</div><p class="dl-why">Fixing this needs a decision, not code: name '
              'the third registrar (or the other account) and the sync pulls them in '
              'like the other two.</p></div>')

    when = (dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            .strftime("%d %b %Y, %H:%M UTC") if stamp else "never")

    body = f"""
<p class="sub">Every domain we own, scored on whether it can actually send today.
{len(rows)} in the registry; the {len(checked)} that are <em>new</em>,
<em>warming</em> or <em>active</em> get checked. Parked and retired domains send
nothing, so they are left alone. Last checked {when}.</p>

{alerts}

<div class="grid">{cards}</div>

<div class="tools">
  <input type="search" id="q" placeholder="Search domain, client, registrar, action...">
  <select id="st">
    <option value="">All statuses</option>
    <option>active</option><option>warming</option><option>new</option>
    <option>parked</option><option>retired</option><option>expired</option>
  </select>
  <select id="bd">
    <option value="">All bands</option>
    <option value="RED">Red</option><option value="AMBER">Amber</option>
    <option value="GREEN">Green</option><option value="NONE">Unchecked</option>
  </select>
  <select id="pl" title="Filter by measured inbox placement">
    <option value="">All placement</option>
    <option value="g-ok">Safe for Google</option>
    <option value="m-ok">Safe for Microsoft</option>
    <option value="both-ok">Safe for both</option>
    <option value="m-bad">Fails at Microsoft</option>
    <option value="g-bad">Fails at Google</option>
    <option value="untested">Never placement-tested</option>
  </select>
  <span class="count" id="count"></span>
</div>

<div class="scroll"><table>
<thead><tr>
  <th class="sev"></th>
  <th onclick="sortby('domain')">Domain</th>
  <th onclick="sortby('client')">Client</th>
  <th onclick="sortby('registrar')">Registrar</th>
  <th onclick="sortby('score')">Score</th>
  <th onclick="sortby('band')">Band</th>
  <th onclick="sortby('google_inbox')" title="Inbox rate at Google seed inboxes">Google</th>
  <th onclick="sortby('ms_inbox')" title="Inbox rate at Microsoft/Office365 seed inboxes">Microsoft</th>
  <th onclick="sortby('status')">Status</th>
  <th onclick="sortby('mailbox_count')">Mbx</th>
  <th>What to do</th>
  <th onclick="sortby('days_to_expiry')">Expires</th>
</tr></thead>
<tbody id="rows"></tbody>
</table></div>

<h2>How the score works</h2>
<div class="bar">
<p style="margin:0;font-size:.88rem">Authentication 45 (SPF/DKIM/DMARC) &middot;
reputation 20 &middot; warmup 15 &middot; MX 10 &middot; age 10.
A <b>hard gate</b> caps a burned or broken domain at 39 (RED); a <b>soft gate</b>
caps a domain that is fine on paper but cannot send today &mdash; a dead mailbox,
spam over 3% &mdash; at 79 (AMBER), so no row reads GREEN next to a corrective
action.</p>
<p style="margin:9px 0 0;font-size:.85rem;color:var(--mut)">Every sending domain
sits on shared Google/Microsoft MX, so IP-blacklist results grade the
<em>provider</em>, not us &mdash; which is why SURBL outweighs them 15:5. And
Spamhaus answers nothing through a public resolver, so its lists are reported as
<b>blocked</b> rather than passed: a blocked list is not a clean list.</p>
</div>
"""

    # The anon key ships IN THE PAGE, so it must be provably narrow before it is
    # published: verify_anon.py asserts against the live database that it can write
    # `client` and nothing else (no status, no deletes, no other table, and none of
    # the hiring pipeline's people tables). deploy_site.py runs it and refuses to
    # deploy on a failure. Never publish this key without that gate.
    key = os.getenv("HIRING_SUPABASE_ANON_KEY")
    if not key:
        raise SystemExit(
            "PREFLIGHT: HIRING_SUPABASE_ANON_KEY not set -- the page needs it to save "
            "client edits. Put it in the repo .env or the SOS shared .env.")

    clients = sorted({r["client"] for r in rows if r.get("client")})

    data = ("var DATA=" + json.dumps(rows, separators=(",", ":"), default=str) + ";\n"
            + "var CLIENTS=" + json.dumps(clients) + ";\n"
            # Imported, never re-typed: the sync's STATUSES is the one list, and the
            # database's CHECK constraint rejects anything outside it anyway.
            + "var STATUSES=" + json.dumps(list(STATUSES)) + ";\n"
            # What the weekly checker actually looks at. The page warns when an edit
            # would move a domain OUT of this, so it must be the checker's own list
            # rather than a hand-written copy that goes stale.
            + "var CHECK_SCOPE=" + json.dumps(list(CHECK_SCOPE)) + ";\n"
            + "var REST=" + json.dumps(f"https://{ddb.REF}.supabase.co/rest/v1") + ";\n"
            + "var KEY=" + json.dumps(key) + ";\n")
    return site_shell.page(
        title="Domain Health",
        active="deliverability",
        body=body,
        css=CSS,
        js=data + JS,
        footer=f"Source <code>deliverability.domain_health</code>. Built "
               f"{today.isoformat()}. Regenerate with <code>check.py</code> then "
               f"<code>sync_registrars.py</code>, in that order.",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    rows = load()
    gap = unregistered()
    OUT.mkdir(parents=True, exist_ok=True)
    page = OUT / "deliverability.html"
    page.write_text(render(rows, gap, dt.date.today()), encoding="utf-8")

    checked = sum(1 for r in rows if r["band"])
    kb = page.stat().st_size / 1024
    print(f"[OK] {page}  ({kb:.0f} KB, {len(rows)} domains, {checked} checked, "
          f"{len(gap)} unregistered senders)")
    if args.open:
        webbrowser.open(page.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
