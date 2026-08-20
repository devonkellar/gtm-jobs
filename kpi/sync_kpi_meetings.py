#!/usr/bin/env python3
"""
sync_kpi_meetings.py — fill the Appointments + Presentations rows of the weekly
funnel on the KPIs tab of the install reply-tracking sheet, from Attio.

Both rows read STAGE TRANSITIONS (agreed with Devon 2026-08-01):

  Appointments  = leads entering "Meeting Booked" OR "Converted to Deal",
                  counted once per lead at the first such move.
  Presentations = deals reaching "Proposal Sent" or beyond,
                  counted once per deal at the first such move.

**Why transitions and not booking fields.** Devon books meetings many ways —
cal.com, Calendly, the prospect's own calendar, over email — and when a sync
didn't see it he drags the card in Attio himself. Attio is the single source of
truth; a transition fires whether a sync moved the card or he did. The previous
version counted the `meeting_date` field, which only the two booking syncs ever
write, so it missed every manually-handled meeting: 2 of the 3 real bookings.

A transition is also stable after the fact. A deal now at Closed Won still counts
in the week it passed through Discovery Complete, and reading the CURRENT stage
instead would re-count it into whatever week the script happens to run. Attio
versions status cells, so `active_from` on the historic values of the `stage`
attribute (`/attributes/stage/values?show_historic=true`) gives the real date.

Scope is EVERY source in Attio (cal.com, Calendly, manual) — Devon's call,
matching the "(other)" email rows that count all campaigns. Honest limit: these
rows measure total meeting activity, NOT activity attributable to the install
emails in the rows above. Install-campaign bookings only reach Attio if that lead
already replied and got synced (`sync_calendly_attio.py` bumps existing leads, it
never creates them), while most appointments arrive via the personal cal.com link.

Usage:
    python sync_kpi_meetings.py                 # current week
    python sync_kpi_meetings.py --dry-run
    python sync_kpi_meetings.py --backfill      # every week column on the tab

Exit codes: 0 = ok, 1 = error.
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# VENDORED COPY -- original:
#   devon-kellar-freelance/functions/growth/crm/sync_kpi_meetings.py
# Only the import paths differ. The original reaches into three repos by
# absolute Windows path; here the modules sit beside this file (attio_client,
# update_install_kpis) or one level up in scripts/ (sheets_client), so the
# same code runs on a Linux runner with nothing checked out but this repo.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                 # attio_client, update_install_kpis
sys.path.insert(0, str(_HERE.parent / "scripts"))  # sheets_client
# Laptop fallbacks, so this file still runs in place if copied back.
for _p in (r"C:\Users\Devon\sos\shared\scripts",
           r"C:\Users\Devon\blue-summit\product\outbound-install\scripts",
           r"C:\Users\Devon\devon-kellar-freelance\functions\growth\crm"):
    if Path(_p).exists():
        sys.path.append(_p)

from attio_client import Attio  # noqa: E402
from sheets_client import SheetsClient  # noqa: E402
# Reuse the KPI tab's layout + week-label handling so the two scripts can never
# disagree about which column is which week.
from update_install_kpis import (  # noqa: E402
    SHEET_ID,
    TAB,
    HEADER_MARKER,
    col_letter,
    parse_week_label,
    week_label,
)

LEADS_LIST = "pipeline"
DEALS_LIST = "deals_pipeline"

# Attio is the SINGLE SOURCE OF TRUTH and both rows read STAGE TRANSITIONS, not
# booking fields. Devon books meetings many ways — cal.com, Calendly, the
# prospect's own calendar, over email — and drags the card himself when a sync
# didn't see it. A transition fires either way; `meeting_date` only ever gets
# written by the two booking syncs, so counting it missed every manually-handled
# meeting (2 of the 3 real bookings as of 2026-08-01).
#
# Appointments: first entry into Meeting Booked OR Converted to Deal. "Or later"
# has to stop there — Nurture and Dead are reachable WITHOUT a meeting ever
# happening (New Lead -> Nurture is 24 of 43 leads, plain cold-lead
# housekeeping), so sweeping those in would inflate the row roughly 8x.
# Converted to Deal is included because a deal can be created straight from a
# prospect when the meeting was booked outside the syncs.
APPOINTMENT_STAGES = {"Meeting Booked", "Converted to Deal"}

# Presentations: deals reaching "Proposal Sent" or beyond.
#
# THIS USED TO BE {"Discovery Complete"} AND THAT IS NOW WRONG. When it was
# written, "Discovery Complete" was a LATER stage meaning "the discovery call
# happened". It has since been renamed/reordered in the Attio UI into the FIRST
# stage of the Deals list — the stage every promotion lands in by default (see
# promote_to_deal.FIRST_STAGE). Counting entries into it would therefore have
# counted every newly-created deal as a delivered presentation.
#
# Measured 2026-08-20 before changing it: only 3 of 11 deals had ever passed
# through that stage, and all 3 were genuine, so the row was not yet inflated —
# this is a fix landed before the damage, not after it.
#
# The replacement counts a deal ONCE, at its first entry into any stage at or
# beyond Proposal Sent, because reaching those stages is what evidences a pitch
# actually delivered. Counted once per deal (`first_only=True`) so a deal that
# moves Proposal Sent -> Verbal Yes -> Closed Won is one presentation, not three.
PRESENTATION_STAGES = {"Proposal Sent", "Verbal Yes", "Closed Won"}
# Sales = deals entering Closed Won. Per the CRM README, Closed Won means the
# contract is signed and `value` is the first 3 months of revenue
# (setup + 3x retainer) — that is what the evidence record reports.
SALES_STAGE = "Closed Won"

# The Attio workspace was created 2026-07-18 — the earliest lead AND deal both
# date from then. Weeks before that hold no data, so a 0 would assert "no
# meetings happened" when the truth is simply unrecorded. Left BLANK instead.
# (Same rule as the blank pre-13-July `Emails Sent (other)` cells, where deleted
# Smartlead campaigns made the real figure unknowable.)
#
# CRITICAL: the setup backfill stamped `active_from` at IMPORT time — 7 leads all
# "entered Meeting Booked" at 2026-07-18T08:24, which is one import, not seven
# bookings. Historical stage timestamps therefore record when data was LOADED,
# not when events happened. So the floor is the Monday AFTER the import week:
# only transitions Devon (or a sync) actually made in Attio are counted.
# Do not lower this to capture more history — it would be counting import noise.
ATTIO_COVERAGE_FROM = date(2026, 7, 20)
IMPORT_CUTOFF = datetime(2026, 7, 19)  # ignore any transition stamped before this

APPT_ROW_LABEL = "Appointments"
PRES_ROW_LABEL = "Presentations"
SALES_ROW_LABEL = "Sales"

# Human-readable evidence for every number written, so the sheet can be checked
# rather than trusted. Regenerated in full on each run.
EVIDENCE_PATH = Path(os.environ.get(
    "KPI_EVIDENCE_PATH",
    r"C:\Users\Devon\sos\shared\reports\kpi-meetings-sync\EVIDENCE.md"))


def week_of(iso_ts):
    """ISO timestamp -> Monday of its week, or None if unparseable."""
    if not iso_ts:
        return None
    try:
        d = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None
    return d - timedelta(days=d.weekday())


def _stage_history(client, list_slug, entry_id):
    """Chronological historic values of an entry's `stage` attribute.

    Attio versions status cells, so each past stage survives with the
    `active_from` timestamp of when it became active. That is what makes a
    transition countable after the fact — the entry's CURRENT stage would
    re-count a deal into whatever week the script happens to run."""
    resp = client.get(
        f"/lists/{list_slug}/entries/{entry_id}/attributes/stage/values",
        params={"show_historic": "true"},
    )
    return sorted(resp.get("data", []), key=lambda v: v.get("active_from") or "")


def _entered_week(vals, wanted, first_only, with_meta=False):
    """Weeks in which a stage in `wanted` became active.

    first_only=True returns at most one hit (the earliest qualifying transition)
    so a lead is counted once no matter how it moves afterwards.
    Transitions stamped before IMPORT_CUTOFF are dropped as setup-import noise.
    with_meta=True yields (week, stage_title, iso_date) instead of bare weeks,
    which is what the evidence record needs."""
    hits = []
    for val in vals:
        title = (val.get("status") or {}).get("title")
        if title not in wanted:
            continue
        raw = val.get("active_from")
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts.replace(tzinfo=None) < IMPORT_CUTOFF:
            continue
        wk = week_of(raw)
        if wk:
            hits.append((wk, title, str(raw)[:10]) if with_meta else wk)
            if first_only:
                break
    return hits


def _person_label(client, entry, cache):
    """'Full Name (email)' for the person a list entry hangs off.

    Every count in the evidence record must name a human Devon can recognise and
    check — a bare tally is not verifiable."""
    pid = entry.get("parent_record_id")
    if not pid:
        return "(no linked person)"
    if pid in cache:
        return cache[pid]
    try:
        vals = client.get(f"/objects/people/records/{pid}").get("data", {}).get("values", {})
        name = (vals.get("name") or [{}])[0].get("full_name") or ""
        email = (vals.get("email_addresses") or [{}])[0].get("email_address") or ""
        label = f"{name} ({email})".strip() if (name or email) else pid[:8]
    except Exception:  # noqa: BLE001 - a lookup failure must not kill the sync
        label = pid[:8]
    cache[pid] = label
    return label


def _deal_value(entry_values):
    """Deal value as a float, or None. Attio returns it as a versioned cell."""
    cells = entry_values.get("value")
    if not isinstance(cells, list) or not cells:
        return None
    raw = cells[0].get("currency_value")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def collect(client):
    """Single pass over both lists. Returns (counts, evidence) where
        counts   = {metric: {monday: n}}
        evidence = [{week, metric, who, stage, when, detail}, ...]
    Counts and evidence come from the SAME traversal, so the record can never
    disagree with the number written to the sheet."""
    counts = {"appointments": {}, "presentations": {}, "sales": {}}
    evidence = []
    cache = {}

    def add(metric, wk, who, stage, when, detail=""):
        counts[metric][wk] = counts[metric].get(wk, 0) + 1
        evidence.append({"week": wk, "metric": metric, "who": who,
                         "stage": stage, "when": when, "detail": detail})

    for entry in client.list_entries(LEADS_LIST):
        vals = _stage_history(client, LEADS_LIST, entry["id"]["entry_id"])
        hits = _entered_week(vals, APPOINTMENT_STAGES, first_only=True, with_meta=True)
        for wk, stage, when in hits:
            add("appointments", wk, _person_label(client, entry, cache), stage, when)

    for entry in client.list_entries(DEALS_LIST):
        vals = _stage_history(client, DEALS_LIST, entry["id"]["entry_id"])
        who = _person_label(client, entry, cache)
        val = _deal_value(entry.get("entry_values", {}))
        money = f"${val:,.0f}" if val is not None else ""
        for wk, stage, when in _entered_week(vals, PRESENTATION_STAGES,
                                             first_only=True, with_meta=True):
            add("presentations", wk, who, stage, when, money)
        for wk, stage, when in _entered_week(vals, {SALES_STAGE},
                                             first_only=False, with_meta=True):
            add("sales", wk, who, stage, when, money)

    return counts, evidence


def resolve_rows(grid):
    """Find the header row + the Appointments / Presentations rows in the
    contiguous funnel block under it. Mirrors update_install_kpis.resolve_layout:
    the TARGETS table further down repeats these labels and must not match."""
    def cell_a(row):
        return str(row[0]).strip() if row else ""

    header_idx = None
    for i, row in enumerate(grid):
        if cell_a(row).upper() == HEADER_MARKER:
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(f"Could not find the '{HEADER_MARKER}' header row")

    wanted = {APPT_ROW_LABEL: "appointments",
              PRES_ROW_LABEL: "presentations",
              SALES_ROW_LABEL: "sales"}
    found = {}
    started = False
    for i in range(header_idx + 1, len(grid)):
        label = cell_a(grid[i])
        if label.upper() == "TARGETS":
            break
        if not label:
            if started:
                break
            continue
        started = True
        if label in wanted and wanted[label] not in found:
            found[wanted[label]] = i
    missing = set(wanted.values()) - set(found)
    if missing:
        raise RuntimeError(
            f"Could not find row(s) {sorted(missing)} in the funnel block below the header"
        )
    return header_idx, found


def write_evidence(counts, evidence, weeks, dry_run=False):
    """Write the human-checkable record behind every number on the sheet.

    One section per week, naming the person and the exact stage transition (with
    its date) that produced each count, so a figure can be traced back to a card
    in Attio rather than taken on trust. Leads is NOT included — that row comes
    from Smartlead replies via update_install_kpis.py, not from Attio, and this
    file only vouches for what it actually counted."""
    lines = [
        "# KPI evidence — Appointments / Presentations / Sales",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} from the Attio workspace "
        "(`pipeline` + `deals_pipeline`).",
        "",
        "Each row below is one **stage transition** — the thing that was counted.",
        "Cross-check any of them by opening the person in Attio and looking at the",
        "stage history. Definitions:",
        "",
        "| Metric | Counted when |",
        "|---|---|",
        "| Appointments | lead first enters **Meeting Booked** or **Converted to Deal** |",
        "| Presentations | deal first reaches **Proposal Sent** or beyond |",
        "| Sales | deal enters **Closed Won** (value = first 3 months: setup + 3x retainer) |",
        "",
        "**Not covered here:** the *Leads* row comes from Smartlead positive replies "
        "(`update_install_kpis.py`), not Attio.",
        "",
        f"**Coverage starts {ATTIO_COVERAGE_FROM}.** Earlier weeks are blank, not 0 — "
        "the workspace was imported 2026-07-18 and that import stamped 7 leads as "
        "entering Meeting Booked in a single minute, so pre-import history records "
        "when data was *loaded*, not when meetings happened.",
        "",
    ]

    by_week = {}
    for row in evidence:
        by_week.setdefault(row["week"], []).append(row)

    for monday in sorted(weeks, reverse=True):
        rows = sorted(by_week.get(monday, []), key=lambda r: (r["metric"], r["when"]))
        a = counts["appointments"].get(monday, 0)
        p = counts["presentations"].get(monday, 0)
        s = counts["sales"].get(monday, 0)
        lines.append(f"## W/C {week_label(monday)}  ({monday})")
        lines.append("")
        lines.append(f"**Appointments {a} · Presentations {p} · Sales {s}**")
        lines.append("")
        if not rows:
            lines.append("_Nothing recorded in Attio this week._")
            lines.append("")
            continue
        lines.append("| Metric | Who | Stage entered | Date | Value |")
        lines.append("|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['metric'].capitalize()} | {r['who']} | {r['stage']} "
                f"| {r['when']} | {r['detail'] or ''} |"
            )
        lines.append("")

    text = "\n".join(lines)
    if dry_run:
        print("\n--- evidence (dry-run, not written) ---")
        print(text)
        return
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(text, encoding="utf-8")
    print(f"[i] Evidence written to {EVIDENCE_PATH}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    ap.add_argument("--backfill", action="store_true",
                    help="fill every week column present on the tab, not just this week")
    args = ap.parse_args()

    client = Attio()
    print("[i] Reading Attio ...", flush=True)
    counts, evidence = collect(client)
    for metric in ("appointments", "presentations", "sales"):
        print(f"[i] {metric}: { {str(k): v for k, v in sorted(counts[metric].items())} }")

    ss = SheetsClient(SHEET_ID)
    grid = ss.read(TAB, range_a1="A1:Z40", value_render="FORMATTED_VALUE")
    header_idx, rows_idx = resolve_rows(grid)
    header = grid[header_idx]

    today = date.today()
    this_monday = today - timedelta(days=today.weekday())

    # Map each week column on the tab to its Monday.
    cols = {}
    for c in range(1, len(header)):
        d = parse_week_label(header[c], this_monday.year)
        if d:
            cols[d - timedelta(days=d.weekday())] = c
    if not cols:
        raise RuntimeError("No week columns parsed from the header row")

    targets = sorted(cols) if args.backfill else [this_monday]
    data = []
    for monday in targets:
        col_idx = cols.get(monday)
        if col_idx is None:
            print(f"[!] no column for week {monday} — skipping")
            continue
        col = col_letter(col_idx)
        if monday < ATTIO_COVERAGE_FROM:
            print(f"[i] W/C {week_label(monday)} (col {col}): "
                  "SKIPPED — predates Attio, leaving blank not 0")
            continue
        wrote = []
        for metric, row_i in rows_idx.items():
            n = counts[metric].get(monday, 0)
            data.append({"range": f"{TAB}!{col}{row_i + 1}", "values": [[n]]})
            wrote.append(f"{metric}={n}")
        print(f"[i] W/C {week_label(monday)} (col {col}): {', '.join(wrote)}")

    written_weeks = [m for m in targets if m >= ATTIO_COVERAGE_FROM and m in cols]
    write_evidence(counts, evidence, written_weeks, dry_run=args.dry_run)

    if args.dry_run:
        print("\n[dry-run] no sheet write")
        return 0

    ss._svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    print(f"[OK] wrote {len(data)} cells")
    print(f"     https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=249207925")
    return 0


if __name__ == "__main__":
    sys.exit(main())
