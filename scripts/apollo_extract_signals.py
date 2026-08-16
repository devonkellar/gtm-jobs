#!/usr/bin/env python3
"""
Apollo Call Signal Extraction Pipeline
Extracts structured signals from cold call transcripts via LLM (Gemini Flash).
Writes signal_type, signal_summary, objection_reason, prospect_interest_level,
next_step, and analyzed_at back to Supabase apollo_calls.

PREREQUISITE — Run this SQL in Supabase SQL Editor (one-time):
    ALTER TABLE apollo_calls
      ADD COLUMN IF NOT EXISTS signal_type TEXT,
      ADD COLUMN IF NOT EXISTS signal_summary TEXT,
      ADD COLUMN IF NOT EXISTS objection_reason TEXT,
      ADD COLUMN IF NOT EXISTS prospect_interest_level SMALLINT,
      ADD COLUMN IF NOT EXISTS next_step TEXT,
      ADD COLUMN IF NOT EXISTS analyzed_at TIMESTAMPTZ;

Usage:
    python apollo_extract_signals.py              # Analyze all unanalyzed calls
    python apollo_extract_signals.py --dry-run    # Show what would be analyzed
    python apollo_extract_signals.py --limit 10   # Analyze only 10 calls
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ───────────────────────────────────────────────────────────────────
SOS_ROOT = Path(r"C:\Users\Devon\sos")
ENV_FILE = SOS_ROOT / "shared" / "config" / ".env"
LOG_DIR = SOS_ROOT / "shared" / "reports" / "signal-extraction"

SUPABASE_URL = "https://mgonnoxpaqqcbtrkzmpf.supabase.co"


def load_supabase_key():
    """Service-role key from .env.

    This script writes (analyzed_at, signal_*). It previously carried a
    hardcoded ANON key, which RLS lets SELECT but silently blocks from
    UPDATE — PostgREST answers 200 with 0 rows affected, so every run
    re-fetched the same 449 rows, re-ran the LLM, and discarded the
    results. Must be service_role for writes to land.
    """
    if not ENV_FILE.exists():
        print(f"ERROR: .env not found at {ENV_FILE}")
        sys.exit(1)
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("SUPABASE_SERVICE_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    print("ERROR: SUPABASE_SERVICE_KEY not found in .env")
    sys.exit(1)


SUPABASE_KEY = load_supabase_key()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL = "google/gemini-2.5-flash"  # 2.0-flash-001 retired; 404s on OpenRouter
LLM_DELAY = 0.15  # 150ms between calls

# Dispositions that are auto-tagged without LLM
NOISE_DISPOSITIONS = {
    "No Answer",
    "Voicemail",
    "Wrong Number for Target",
}
NO_FIT_DISPOSITIONS = {
    "Not a Phone Lead (meany)",
    "Wrong Target For Offer",
}

MIN_DURATION_FOR_LLM = 15  # seconds


def load_openrouter_key():
    if not ENV_FILE.exists():
        print(f"ERROR: .env not found at {ENV_FILE}")
        sys.exit(1)
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip()
    print("ERROR: OPENROUTER_API_KEY not found in .env")
    sys.exit(1)


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def sb_request(method, url, *, attempts=4, **kw):
    """Supabase call with backoff on transient faults.

    The scheduled run sits on a laptop that sleeps, so both ends flake:
    PostgREST 5xx and DNS getaddrinfo failures each killed ~1 run in 3
    (see shared/reports/signal-extraction/). Retrying makes those a
    non-event instead of a lost day of analysis.
    """
    kw.setdefault("headers", supabase_headers())
    kw.setdefault("timeout", 180)
    for attempt in range(attempts):
        try:
            resp = requests.request(method, url, **kw)
        except (requests.ConnectionError, requests.Timeout) as ex:
            if attempt == attempts - 1:
                raise
            print(f"  transient {type(ex).__name__}, retry {attempt + 1}/{attempts - 1}")
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code >= 500:
            if attempt == attempts - 1:
                resp.raise_for_status()
            print(f"  Supabase {resp.status_code}, retry {attempt + 1}/{attempts - 1}")
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")


def fetch_unanalyzed(limit=None):
    """Fetch calls with transcript but no signal analysis."""
    url = (
        f"{SUPABASE_URL}/rest/v1/apollo_calls"
        f"?select=id,call_date,contact_name,contact_company,contact_title,"
        f"disposition,duration_sec,transcript"
        f"&transcript=not.is.null"
        f"&analyzed_at=is.null"
        f"&order=call_date.desc"
    )
    if limit:
        url += f"&limit={limit}"
    return sb_request("GET", url).json()


def fetch_noise_calls(limit=None):
    """Fetch calls without transcripts that should be auto-tagged as noise/no_fit."""
    url = (
        f"{SUPABASE_URL}/rest/v1/apollo_calls"
        f"?select=id,disposition,duration_sec"
        f"&analyzed_at=is.null"
        f"&or=(transcript.is.null,transcript.eq.)"
        f"&order=call_date.desc"
    )
    if limit:
        url += f"&limit={limit}"
    return sb_request("GET", url).json()


def classify_without_llm(call):
    """Auto-classify calls that don't need LLM analysis. Returns signal dict or None."""
    disp = (call.get("disposition") or "").strip()
    duration = call.get("duration_sec") or 0

    if disp in NOISE_DISPOSITIONS:
        return {
            "signal_type": "noise",
            "signal_summary": f"Auto-tagged: {disp}",
            "objection_reason": None,
            "prospect_interest_level": None,
            "next_step": "No follow-up warranted",
        }

    if disp in NO_FIT_DISPOSITIONS:
        return {
            "signal_type": "no_fit",
            "signal_summary": f"Auto-tagged: {disp}",
            "objection_reason": None,
            "prospect_interest_level": 1 if "meany" in disp.lower() else 2,
            "next_step": "No follow-up warranted",
        }

    if duration < MIN_DURATION_FOR_LLM:
        return {
            "signal_type": "no_fit",
            "signal_summary": f"Auto-tagged: duration {duration}s (under {MIN_DURATION_FOR_LLM}s threshold)",
            "objection_reason": None,
            "prospect_interest_level": None,
            "next_step": "No follow-up warranted",
        }

    return None


LLM_PROMPT = """You are analyzing a cold call transcript from a B2B sales team. Extract structured signal data.

CONTEXT:
- Caller works for a B2B outbound/GTM services company
- They are calling prospects to offer services (outbound sales, data feeds, prospect intelligence)
- Disposition logged by caller: {disposition}
- Call duration: {duration_sec} seconds
- Contact: {contact_name} at {contact_company} ({contact_title})

TRANSCRIPT:
{transcript}

Analyze this conversation and return a JSON object with these fields:

1. "signal_type": One of: "win", "warm", "objection", "competitor", "timing", "no_fit"
   - "win": Prospect agreed to meeting/demo/explicit next step with calendar commitment
   - "warm": Showed interest, asked for info, gave referral, or agreed to follow up
   - "objection": Gave a specific business reason for declining
   - "competitor": Specifically named a competing product or service
   - "timing": Said not now but potentially later (budget cycle, contract, wrong time)
   - "no_fit": Wrong person, hostile, or no substantive conversation happened

2. "signal_summary": 1-2 sentences capturing the KEY moment. Use the prospect's actual words when possible. Focus on what makes this actionable.

3. "objection_reason": Only if signal_type is "objection", "competitor", or "timing". One of: "competitor_in_place", "budget_constraint", "timing_bad", "no_need", "no_authority", "satisfied_current", "other". Otherwise null.

4. "prospect_interest_level": Integer 1-5 (1=hostile, 2=dismissive, 3=polite decline, 4=curious/engaged, 5=enthusiastic/agreed)

5. "next_step": What happened or should happen. Be specific: "Booked meeting Thursday", "Sending case study to jane@acme.com", "Referred to VP Sales Tom Smith", "No follow-up warranted".

Return ONLY valid JSON."""


def analyze_with_llm(call, api_key):
    """Send transcript to Gemini Flash for signal extraction. Returns signal dict."""
    prompt = LLM_PROMPT.format(
        disposition=call.get("disposition") or "Unknown",
        duration_sec=call.get("duration_sec") or 0,
        contact_name=call.get("contact_name") or "Unknown",
        contact_company=call.get("contact_company") or "Unknown",
        contact_title=call.get("contact_title") or "Unknown",
        transcript=call.get("transcript") or "",
    )

    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 500,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    result = json.loads(content)

    # Validate signal_type
    valid_types = {"win", "warm", "objection", "competitor", "timing", "no_fit"}
    if result.get("signal_type") not in valid_types:
        result["signal_type"] = "no_fit"

    # Validate objection_reason
    valid_reasons = {
        "competitor_in_place", "budget_constraint", "timing_bad",
        "no_need", "no_authority", "satisfied_current", "other",
    }
    if result.get("signal_type") not in ("objection", "competitor", "timing"):
        result["objection_reason"] = None
    elif result.get("objection_reason") not in valid_reasons:
        result["objection_reason"] = "other"

    # Validate interest level
    level = result.get("prospect_interest_level")
    if isinstance(level, (int, float)) and 1 <= level <= 5:
        result["prospect_interest_level"] = int(level)
    else:
        result["prospect_interest_level"] = None

    return result


def write_signal(call_id, signal):
    """Write signal data back to Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/apollo_calls?id=eq.{call_id}"
    payload = {
        "signal_type": signal["signal_type"],
        "signal_summary": signal.get("signal_summary"),
        "objection_reason": signal.get("objection_reason"),
        "prospect_interest_level": signal.get("prospect_interest_level"),
        "next_step": signal.get("next_step"),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }
    # return=representation so a 0-row write is caught, not assumed to work
    resp = sb_request("PATCH", url, json=payload,
                      headers={**supabase_headers(), "Prefer": "return=representation"})
    if not resp.json():
        raise RuntimeError(
            f"write_signal({call_id}) affected 0 rows — PostgREST returned 200 but "
            f"nothing updated. Usually an RLS/permission issue: confirm "
            f"SUPABASE_SERVICE_KEY in .env is the service_role key, not anon.")


def main():
    parser = argparse.ArgumentParser(description="Extract signals from Apollo call transcripts")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be analyzed")
    parser.add_argument("--limit", type=int, default=None, help="Max calls to process")
    args = parser.parse_args()

    api_key = load_openrouter_key()

    # Phase 1: Auto-tag noise calls (no transcript needed)
    noise_calls = fetch_noise_calls()
    auto_tagged = 0
    auto_skipped = 0

    for call in noise_calls:
        signal = classify_without_llm(call)
        if signal:
            if not args.dry_run:
                write_signal(call["id"], signal)
            auto_tagged += 1
        else:
            auto_skipped += 1

    print(f"Phase 1 (auto-tag): {auto_tagged} noise/no_fit tagged, {auto_skipped} need LLM")

    # Phase 2: LLM analysis for transcribed calls
    calls = fetch_unanalyzed(args.limit)

    # Separate auto-classifiable from LLM-needed
    llm_calls = []
    for call in calls:
        signal = classify_without_llm(call)
        if signal:
            if not args.dry_run:
                write_signal(call["id"], signal)
            auto_tagged += 1
        else:
            llm_calls.append(call)

    print(f"Phase 2: {len(calls)} transcribed unanalyzed, {len(calls) - len(llm_calls)} auto-tagged, {len(llm_calls)} need LLM")

    if args.dry_run:
        print(f"\n-- DRY RUN --")
        print(f"Total auto-tag: {auto_tagged}")
        print(f"Would send {len(llm_calls)} calls to LLM:")
        for c in llm_calls[:20]:
            disp = (c.get("disposition") or "—")[:30]
            name = (c.get("contact_name") or "—")[:25]
            comp = (c.get("contact_company") or "—")[:25]
            dur = c.get("duration_sec", 0)
            print(f"  {c['call_date']} | {name:25s} | {comp:25s} | {disp:30s} | {dur}s")
        if len(llm_calls) > 20:
            print(f"  ... and {len(llm_calls) - 20} more")
        return

    # LLM analysis
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_lines = []
    success = 0
    failed = 0
    signal_counts = {}

    for i, call in enumerate(llm_calls):
        cid = call["id"]
        label = (
            f"[{i+1}/{len(llm_calls)}] {call['call_date']} "
            f"{call.get('contact_name') or '—'} ({call.get('duration_sec', 0)}s)"
        )
        try:
            signal = analyze_with_llm(call, api_key)
            write_signal(cid, signal)
            success += 1
            st = signal["signal_type"]
            signal_counts[st] = signal_counts.get(st, 0) + 1
            log_lines.append(f"OK   {cid} — {st}: {signal.get('signal_summary', '')[:80]}")
            print(f"  OK   {label} — {st}")
        except json.JSONDecodeError as e:
            failed += 1
            log_lines.append(f"FAIL {cid} — JSON parse error: {e}")
            print(f"  FAIL {label} — JSON parse error: {e}")
        except Exception as e:
            failed += 1
            log_lines.append(f"FAIL {cid} — {e}")
            print(f"  FAIL {label} — {e}")

        time.sleep(LLM_DELAY)

    # Summary
    print(f"\nDone: {auto_tagged} auto-tagged, {success} LLM-analyzed, {failed} failed")
    if signal_counts:
        print("Signal breakdown:", ", ".join(f"{k}={v}" for k, v in sorted(signal_counts.items())))

    # Write log
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{today}.log"
    log_content = f"Signal Extraction — {today}\n"
    log_content += f"Auto-tagged: {auto_tagged}, LLM: {success} ok / {failed} failed\n"
    if signal_counts:
        log_content += f"Breakdown: {', '.join(f'{k}={v}' for k, v in sorted(signal_counts.items()))}\n"
    log_content += f"---\n"
    log_content += "\n".join(log_lines) + "\n"
    log_file.write_text(log_content, encoding="utf-8")
    print(f"Log: {log_file}")


if __name__ == "__main__":
    main()
