"""Archive Smartlead campaign leads + per-send results + sequence copy.

Writes BOTH:
  1. CSV/JSON snapshot per campaign under
     sos/shared/data/smartlead/archive/{campaign_id}-{slug}/
  2. Supabase tables campaign_leads / campaign_sends / campaign_sequences /
     campaign_summary (schema: archive/schema.sql, run once by hand)

SCOPE: EVERY CAMPAIGN IN THE ACCOUNT. Not a curated list. The scope is
computed fresh on every run from the union of three sources:

  1. every campaign the Smartlead API currently returns,
  2. every campaign_id ever seen in replies_log.csv,
  3. every campaign already archived on disk.

That means a campaign created tomorrow is archived tomorrow with nobody
editing this file, and a campaign deleted tomorrow keeps being tracked
because (2) and (3) still remember it. Do NOT reintroduce a hardcoded ID
list - that is what previously limited this to install campaigns and lost
the leads + copy for four of them.

WHY APPEND-ONLY: Smartlead campaigns get deleted, and when they do the API
404s and the leads + copy are gone for good. Seven install campaigns have
already been lost this way, three of them mid-build. So a run that gets
nothing back must NEVER blank an existing snapshot - it marks the campaign
deleted and leaves the last good data intact. Same reason Supabase writes
are upserts, never deletes.

Dead campaigns are backfilled from replies_log.csv, which retains their
replies even after the campaign is gone (identities + reply text survive;
full lead lists and copy do not).

Usage:
  python archive_campaign_results.py                 # EVERY campaign
  python archive_campaign_results.py 3458673 3439229 # specific IDs
  python archive_campaign_results.py --no-supabase   # CSV snapshot only
  python archive_campaign_results.py --no-backfill   # skip replies_log backfill
"""

import csv
import json
import os
import re
import sys
from pathlib import Path
import time
from datetime import datetime, timezone

import requests

# The reply log lives in Supabase now; this is the same lookup the other
# migrated jobs use. Falls back to the known path when the env var is unset.
for _cand in (Path(os.environ.get("GTM_JOBS_SCRIPTS", "")),
              Path(__file__).resolve().parent,
              Path(r"C:\Users\Devon\gtm-jobs\scripts")):
    if _cand and (_cand / "replies_store.py").exists():
        sys.path.insert(0, str(_cand))
        break
else:
    sys.exit("[FAIL] cannot find replies_store.py - set GTM_JOBS_SCRIPTS")
import replies_store  # noqa: E402

BASE = "https://server.smartlead.ai/api/v1"
SUPABASE_URL = "https://mgonnoxpaqqcbtrkzmpf.supabase.co/rest/v1"

SHARED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(SHARED, "data", "smartlead")
ARCHIVE = os.path.join(DATA, "archive")
REPLIES_LOG = os.path.join(DATA, "replies_log.csv")
ENV = os.path.join(SHARED, "config", ".env")

csv.field_size_limit(10_000_000)

# Campaigns known to have existed but which the API no longer lists. Kept so a
# deleted campaign is never silently dropped from scope. Additions are made by
# discovery (replies_log / on-disk), not by hand.
KNOWN_HISTORICAL_IDS = [
    2957281, 3439229, 3537167, 3439219, 3509953,
    3458692, 3458736, 3458673, 3458686,
    3501424, 3501444,
    3629783, 3629785,
    3696068, 3696086,
]


def discover_campaign_ids(campaigns_by_id, replies_by_campaign):
    """EVERY campaign, from all three sources. See module docstring."""
    ids, origin = set(), {}

    for cid in campaigns_by_id:
        ids.add(int(cid))
        origin[int(cid)] = "live"

    for cid in replies_by_campaign:
        try:
            n = int(cid)
        except (TypeError, ValueError):
            continue
        ids.add(n)
        origin.setdefault(n, "replies_log")

    if os.path.isdir(ARCHIVE):
        for d in os.listdir(ARCHIVE):
            m = re.match(r"^(\d+)-", d)
            if m and os.path.isdir(os.path.join(ARCHIVE, d)):
                n = int(m.group(1))
                ids.add(n)
                origin.setdefault(n, "on_disk")

    for cid in KNOWN_HISTORICAL_IDS:
        ids.add(cid)
        origin.setdefault(cid, "known_historical")

    counts = {}
    for v in origin.values():
        counts[v] = counts.get(v, 0) + 1
    print("scope: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return sorted(ids), origin


def env(name):
    with open(ENV, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip()
    raise KeyError(name)


def api_key():
    p = os.path.expanduser("~/.claude/settings.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)["mcpServers"]["smartlead"]["env"]["SMARTLEAD_API_KEY"]


KEY = api_key()
SB_KEY = env("SUPABASE_SERVICE_KEY")
SB_HEADERS = {
    "apikey": SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
}


class CampaignGone(Exception):
    """Campaign 404s - deleted from Smartlead."""


def get(path, **params):
    params["api_key"] = KEY
    for attempt in range(5):
        r = requests.get(f"{BASE}{path}", params=params, timeout=120)
        if r.status_code == 200:
            if not r.text.strip():
                raise CampaignGone(path)
            return r.json()
        if r.status_code == 404:
            raise CampaignGone(path)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (attempt + 1))
            continue
        raise RuntimeError(f"{path} -> {r.status_code}: {r.text[:300]}")
    raise RuntimeError(f"{path} failed after retries")


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "campaign"


def paginate(path, total_key, limit=100):
    out, offset = [], 0
    while True:
        d = get(path, offset=offset, limit=limit)
        batch = d.get("data") or []
        out.extend(batch)
        total = int(d.get(total_key) or 0)
        offset += len(batch)
        if not batch or offset >= total:
            break
    return out


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def truthy(v):
    return str(v).lower() in ("true", "1")


def nullable(v):
    """Empty string -> None, so Postgres timestamps don't choke."""
    return v if v not in ("", "None", None) else None


def sb_upsert(table, rows, conflict, chunk=500):
    """Upsert into Supabase. Never deletes; safe to re-run.

    Rows are DEDUPED on the conflict key first, last occurrence winning.
    Postgres cannot apply ON CONFLICT DO UPDATE to the same row twice inside one
    command -- it aborts the whole statement with SQLSTATE 21000. Smartlead
    genuinely returns the same lead twice in one campaign (one duplicate in
    campaign 3766636 alone), so this is real upstream data, not a fetch bug, and
    every campaign carrying one lost its ENTIRE archive: 3,820 leads across four
    campaigns wrote nothing while the run still reported per-campaign totals.
    """
    if not rows:
        return 0
    keys = [k.strip() for k in conflict.split(",")]
    deduped, seen = [], {}
    for row in rows:
        k = tuple(row.get(x) for x in keys)
        if k in seen:
            deduped[seen[k]] = row          # last write wins
        else:
            seen[k] = len(deduped)
            deduped.append(row)
    if len(deduped) != len(rows):
        print(f"  [dedupe] {table}: {len(rows) - len(deduped)} duplicate "
              f"{conflict} row(s) collapsed")
    rows = deduped

    done = 0
    headers = dict(SB_HEADERS)
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        for attempt in range(4):
            r = requests.post(
                f"{SUPABASE_URL}/{table}",
                headers=headers,
                params={"on_conflict": conflict},
                json=batch,
                timeout=180,
            )
            if r.status_code in (200, 201, 204):
                done += len(batch)
                break
            # A 500 from PostgREST that carries a SQLSTATE is a permanent data
            # error (bad payload), not a transient blip. Retrying it wasted four
            # attempts and then threw the response away, so the log said only
            # "failed after retries" and the actual cause -- 21000, duplicate
            # conflict key -- was invisible for as long as it had been happening.
            body = r.text[:400]
            permanent_500 = r.status_code == 500 and '"code"' in body
            if r.status_code in (429, 502, 503, 504) or (
                    r.status_code == 500 and not permanent_500):
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"supabase {table} -> {r.status_code}: {body}")
        else:
            raise RuntimeError(f"supabase {table} failed after retries")
    return done


def load_replies_log():
    """campaign_id -> reply rows, for backfilling deleted campaigns.

    Reads Supabase through the gtm-jobs seam, not replies_log.csv. This is
    the only record of what a campaign deleted from Smartlead produced, so
    it has to read the same source as everything else -- against the CSV it
    would archive a dead campaign from a log that had stopped updating.
    """
    by_campaign = {}
    for row in replies_store.load_all():
        cid = str(row.get("campaign_id") or "").strip()
        if cid:
            by_campaign.setdefault(cid, []).append(row)
    return by_campaign


def archive_campaign(cid, campaigns_by_id, replies_by_campaign, use_supabase=True):
    meta = campaigns_by_id.get(str(cid)) or {}
    name = meta.get("name") or f"campaign-{cid}"
    print(f"\n[{cid}] {name}")

    # --- probe: is the campaign still alive? ---
    try:
        sequences = get(f"/campaigns/{cid}/sequences")
        alive = True
    except CampaignGone:
        alive = False
        sequences = []

    if not alive:
        return archive_dead_campaign(cid, meta, replies_by_campaign, use_supabase)

    folder = os.path.join(ARCHIVE, f"{cid}-{slug(name)}")
    os.makedirs(folder, exist_ok=True)

    with open(os.path.join(folder, "sequences.json"), "w", encoding="utf-8") as f:
        json.dump(sequences, f, indent=2, ensure_ascii=False)

    seq_rows = [{
        "campaign_id": cid,
        "campaign_name": name,
        "seq_id": s.get("id"),
        "seq_number": s.get("seq_number"),
        "subject": s.get("subject"),
        "email_body": s.get("email_body"),
        "seq_delay_details": s.get("seq_delay_details"),
        "sequence_variants": s.get("sequence_variants"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    } for s in sequences]
    print(f"  sequences: {len(sequences)}")

    # --- leads ---
    leads = paginate(f"/campaigns/{cid}/leads", "total_leads")
    lead_rows, sb_leads, custom_keys = [], [], set()
    for item in leads:
        ld = item.get("lead") or {}
        cf = ld.get("custom_fields") or {}
        custom_keys.update(cf.keys())
        email = ld.get("email")
        if not email:
            continue
        row = {
            "campaign_id": cid, "campaign_name": name,
            "lead_id": ld.get("id"),
            "campaign_lead_map_id": item.get("campaign_lead_map_id"),
            "status": item.get("status"),
            "lead_category_id": item.get("lead_category_id"),
            "created_at": item.get("created_at"),
            "first_name": ld.get("first_name"), "last_name": ld.get("last_name"),
            "email": email, "phone_number": ld.get("phone_number"),
            "company_name": ld.get("company_name"), "website": ld.get("website"),
            "company_url": ld.get("company_url"), "location": ld.get("location"),
            "linkedin_profile": ld.get("linkedin_profile"),
            "is_unsubscribed": ld.get("is_unsubscribed"),
        }
        for k, v in cf.items():
            row[f"cf_{k}"] = v
        lead_rows.append(row)

        sb_leads.append({
            "campaign_id": cid, "campaign_name": name,
            "smartlead_lead_id": ld.get("id"),
            "campaign_lead_map_id": item.get("campaign_lead_map_id"),
            "email": email,
            "first_name": ld.get("first_name"), "last_name": ld.get("last_name"),
            "company_name": ld.get("company_name"), "website": ld.get("website"),
            "company_url": ld.get("company_url"),
            "phone_number": ld.get("phone_number"),
            "linkedin_profile": ld.get("linkedin_profile"),
            "location": ld.get("location"),
            "status": item.get("status"),
            "lead_category_id": item.get("lead_category_id"),
            "is_unsubscribed": bool(ld.get("is_unsubscribed")),
            "custom_fields": cf or None,
            "source": "smartlead_api",
            "lead_created_at": nullable(item.get("created_at")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    lead_fields = [
        "campaign_id", "campaign_name", "lead_id", "campaign_lead_map_id", "status",
        "lead_category_id", "created_at", "first_name", "last_name", "email",
        "phone_number", "company_name", "website", "company_url", "location",
        "linkedin_profile", "is_unsubscribed",
    ] + [f"cf_{k}" for k in sorted(custom_keys)]
    write_csv(os.path.join(folder, "leads.csv"), lead_rows, lead_fields)
    print(f"  leads: {len(lead_rows)}")

    # --- per-send stats ---
    stats = paginate(f"/campaigns/{cid}/statistics", "total_stats")
    for s in stats:
        s["campaign_id"] = cid
        s["campaign_name"] = name
    send_fields = [
        "campaign_id", "campaign_name", "lead_email", "lead_name", "lead_category",
        "sequence_number", "email_subject", "email_message", "sent_time",
        "open_time", "click_time", "reply_time", "open_count", "click_count",
        "is_unsubscribed", "is_bounced", "ignore_reply", "stats_id",
        "email_campaign_seq_id", "seq_variant_id",
    ]
    write_csv(os.path.join(folder, "sends.csv"), stats, send_fields)

    sb_sends = [{
        "stats_id": s.get("stats_id"),
        "campaign_id": cid, "campaign_name": name,
        "lead_email": s.get("lead_email"), "lead_name": s.get("lead_name"),
        "lead_category": s.get("lead_category"),
        "sequence_number": s.get("sequence_number"),
        "email_campaign_seq_id": s.get("email_campaign_seq_id"),
        "seq_variant_id": s.get("seq_variant_id"),
        "email_subject": s.get("email_subject"),
        "email_message": s.get("email_message"),
        "sent_time": nullable(s.get("sent_time")),
        "open_time": nullable(s.get("open_time")),
        "click_time": nullable(s.get("click_time")),
        "reply_time": nullable(s.get("reply_time")),
        "open_count": s.get("open_count") or 0,
        "click_count": s.get("click_count") or 0,
        "is_bounced": truthy(s.get("is_bounced")),
        "is_unsubscribed": truthy(s.get("is_unsubscribed")),
        "ignore_reply": truthy(s.get("ignore_reply")),
        "source": "smartlead_api",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    } for s in stats if s.get("stats_id")]
    print(f"  sends: {len(stats)}")

    try:
        analytics = get(f"/campaigns/{cid}/analytics")
    except (CampaignGone, RuntimeError):
        analytics = {}
    with open(os.path.join(folder, "campaign.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "analytics": analytics}, f, indent=2, ensure_ascii=False)

    sent = sum(1 for s in stats if s.get("sent_time"))
    opened = sum(1 for s in stats if s.get("open_time"))
    clicked = sum(1 for s in stats if s.get("click_time"))
    replied = sum(1 for s in stats if s.get("reply_time"))
    bounced = sum(1 for s in stats if truthy(s.get("is_bounced")))
    unsub = sum(1 for s in stats if truthy(s.get("is_unsubscribed")))
    uniq = len({s["lead_email"] for s in stats if s.get("reply_time")})

    def pct(n, d):
        return round(100.0 * n / d, 2) if d else 0.0

    summary = {
        "campaign_id": cid, "campaign_name": name,
        "status": meta.get("status"), "is_deleted": False,
        "sequence_steps": len(sequences),
        "leads": len(lead_rows), "sends": len(stats), "sent": sent,
        "opened": opened, "clicked": clicked, "replied": replied,
        "unique_leads_replied": uniq, "bounced": bounced, "unsubscribed": unsub,
        "open_rate_pct": pct(opened, sent), "click_rate_pct": pct(clicked, sent),
        "reply_rate_pct": pct(replied, sent),
        "unique_reply_rate_pct": pct(uniq, len(lead_rows)),
        "bounce_rate_pct": pct(bounced, sent),
        "campaign_created_at": nullable(meta.get("created_at")),
        "last_archived_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(folder, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    if use_supabase:
        n1 = sb_upsert("campaign_leads", sb_leads, "campaign_id,email")
        n2 = sb_upsert("campaign_sends", sb_sends, "stats_id")
        n3 = sb_upsert("campaign_sequences", seq_rows, "campaign_id,seq_number")
        sb_upsert("campaign_summary", [summary], "campaign_id")
        print(f"  supabase: {n1} leads, {n2} sends, {n3} seqs")

    print(f"  sent={sent} open={opened} reply={replied} bounce={bounced}")
    return summary


def archive_dead_campaign(cid, meta, replies_by_campaign, use_supabase=True):
    """Campaign deleted from Smartlead. Preserve what replies_log still knows.

    Critically: does NOT overwrite an existing snapshot with blanks.
    """
    replies = replies_by_campaign.get(str(cid), [])
    name = meta.get("name") or (replies[0].get("campaign_name") if replies else None) \
        or f"campaign-{cid}"
    print(f"  DELETED from Smartlead - preserving existing snapshot")

    existing = [d for d in os.listdir(ARCHIVE)
                if d.startswith(f"{cid}-") and
                os.path.isdir(os.path.join(ARCHIVE, d))] if os.path.isdir(ARCHIVE) else []
    folder = os.path.join(ARCHIVE, existing[0]) if existing \
        else os.path.join(ARCHIVE, f"{cid}-{slug(name)}")
    os.makedirs(folder, exist_ok=True)

    # Only write the replies backfill if we have no full snapshot already.
    has_snapshot = os.path.exists(os.path.join(folder, "sends.csv"))
    sb_leads = []
    if replies:
        seen = {}
        for r in replies:
            em = (r.get("lead_email") or "").strip()
            if em and em not in seen:
                seen[em] = r
        fields = ["campaign_id", "campaign_name", "email", "first_name", "last_name",
                  "company_name", "lead_category", "reply_date", "reply_body"]
        rows = [{
            "campaign_id": cid, "campaign_name": name, "email": em,
            "first_name": r.get("lead_first_name"), "last_name": r.get("lead_last_name"),
            "company_name": r.get("lead_company"), "lead_category": r.get("lead_category"),
            "reply_date": r.get("reply_date"), "reply_body": r.get("reply_body"),
        } for em, r in seen.items()]
        write_csv(os.path.join(folder, "replies_backfill.csv"), rows, fields)
        print(f"  backfilled {len(rows)} repliers from replies_log")

        sb_leads = [{
            "campaign_id": cid, "campaign_name": name, "email": em,
            "first_name": r.get("lead_first_name"),
            "last_name": r.get("lead_last_name"),
            "company_name": r.get("lead_company"),
            "status": "REPLIED",
            "source": "replies_log_backfill",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        } for em, r in seen.items()]
    else:
        print("  no replies in replies_log either - nothing recoverable")

    marker = {
        "campaign_id": cid, "campaign_name": name,
        "deleted_from_smartlead": True,
        "noticed_at": datetime.now(timezone.utc).isoformat(),
        "has_full_snapshot": has_snapshot,
        "repliers_recovered": len(sb_leads),
        "note": "Campaign 404s from the Smartlead API. Leads and copy are "
                "unrecoverable unless a prior snapshot exists in this folder.",
    }
    with open(os.path.join(folder, "DELETED.json"), "w", encoding="utf-8") as f:
        json.dump(marker, f, indent=2)

    if use_supabase:
        if sb_leads:
            sb_upsert("campaign_leads", sb_leads, "campaign_id,email")
        # Flag deleted without clobbering real stats from an earlier run.
        r = requests.patch(
            f"{SUPABASE_URL}/campaign_summary",
            headers=SB_HEADERS, params={"campaign_id": f"eq.{cid}"},
            json={"is_deleted": True,
                  "last_archived_at": datetime.now(timezone.utc).isoformat()},
            timeout=60,
        )
        if r.status_code in (200, 204) and r.text.strip() in ("", "[]"):
            sb_upsert("campaign_summary", [{
                "campaign_id": cid, "campaign_name": name, "is_deleted": True,
                "status": "DELETED", "leads": len(sb_leads), "sends": 0, "sent": 0,
                "unique_leads_replied": len(sb_leads),
                "last_archived_at": datetime.now(timezone.utc).isoformat(),
            }], "campaign_id")
    return None


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    use_supabase = "--no-supabase" not in flags
    backfill = "--no-backfill" not in flags

    os.makedirs(ARCHIVE, exist_ok=True)

    # replies_log is always loaded - it is a scope source, not just a backfill
    # source. --no-backfill only suppresses writing the recovered rows.
    replies_by_campaign = load_replies_log()

    campaigns, offset = {}, 0
    while True:
        d = get("/campaigns", offset=offset, limit=100)
        batch = d if isinstance(d, list) else (d.get("data") or [])
        for c in batch:
            campaigns[str(c.get("id"))] = c
        if len(batch) < 100:
            break
        offset += len(batch)
    print(f"live campaigns visible: {len(campaigns)}")

    if argv:
        ids = [int(a) for a in argv]
    else:
        ids, _ = discover_campaign_ids(campaigns, replies_by_campaign)
    print(f"campaigns in scope: {len(ids)}")

    if not backfill:
        replies_by_campaign = {}

    summaries, dead = [], []
    for cid in ids:
        try:
            s = archive_campaign(cid, campaigns, replies_by_campaign, use_supabase)
            if s:
                summaries.append(s)
            else:
                dead.append(cid)
        except Exception as e:
            print(f"  !! {cid} failed: {e}")

    if summaries:
        idx = os.path.join(ARCHIVE, "INDEX.csv")
        write_csv(idx, summaries, list(summaries[0].keys()))
        print(f"\nindex -> {idx}")
    print(f"archived {len(summaries)} live, {len(dead)} deleted")


if __name__ == "__main__":
    main()
