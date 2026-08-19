"""The one way this pipeline talks to Supabase.

TRANSPORT: PostgREST over HTTPS, not a direct Postgres socket.

Not a style choice - measured 2026-08-15. `db.<ref>.supabase.co` resolves **IPv6-only**
(2600:1f16:...; no A record), so a direct psycopg2 connection works only while the
machine has working IPv6 routing. It worked at 15:40 and failed at 16:10 on this
laptop, mid-migration, with the database perfectly healthy. Supabase sells an IPv4
add-on precisely because of this. The transaction pooler is IPv4 but rejected these
credentials in every region tried. [SUPERSEDED - see UPDATE below.]

PostgREST is IPv4, needs no DB password, and is the same API the rest of the stack
already uses. DDL still needs psycopg2 - that is a one-off, and `connect()` is kept
for it.

UPDATE 2026-08-18: this docstring used to add that "the transaction pooler is IPv4 but
rejected these credentials in every region tried", which sent people to the SQL editor
by hand. That was wrong. The SESSION pooler (port 5432, user `postgres.<ref>`) works
fine - this project lives in `aws-0-us-east-2`, and a wrong region answers with
"Tenant or user not found", which is also what a bad password gives, so a region miss
reads as an auth failure. `connect()` now walks the regions. Meanwhile the direct host
has degraded further: `db.<ref>.supabase.co` no longer resolves here AT ALL.

Credentials are HIRING_SUPABASE_* in this repo's .env - never the SOS env, so DK spend
stays attributable and an SOS key rotation cannot break this pipeline.
"""
import datetime
import os
import socket
import time
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

# Env-first, file-second. In CI and on Railway the credentials arrive as real environment
# variables and there is no .env at all, so the file is loaded only when it exists and
# must NOT override what is already set (override=True would let a stale laptop file beat
# the secrets a cloud run was given). HIRING_ENV_PATH lets a container point elsewhere.
ENV = os.getenv("HIRING_ENV_PATH", r"C:\Users\Devon\devon-kellar-freelance\.env")
if os.path.exists(ENV):
    load_dotenv(ENV, override=False)

URL = (os.getenv("HIRING_SUPABASE_URL") or "").rstrip("/")
KEY = os.getenv("HIRING_SUPABASE_SERVICE_KEY")
PW = os.getenv("HIRING_SUPABASE_DB_PASSWORD")
REF = URL.split("//")[1].split(".")[0] if URL else None

H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
TIMEOUT = 60


def _now():
    """PostgREST sends JSON - "now()" would be stored as a literal string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _check():
    if not (URL and KEY):
        raise SystemExit(f"PREFLIGHT: HIRING_SUPABASE_URL / _SERVICE_KEY missing from {ENV}")


RETRIES = 4


def _send(method, table, *, ok, label, retry=True, **kw):
    """One PostgREST call, retried on TRANSPORT failures only.

    Supabase resets idle-ish connections: a 40-company research run died at company 7
    on WinError 10054 (connection forcibly closed) with the database perfectly healthy.
    Losing a long Blitz-bound stage to a single blip is not acceptable, so transport
    failures are replayed.

    RETRY IS NOT UNIVERSALLY SAFE, and this docstring used to claim it was ("every write
    in this module is either an upsert or an idempotent patch"). That was true when it
    was written and then quietly stopped being true. A plain INSERT is not idempotent,
    and the dangerous case is not a failed write - it is a write that SUCCEEDS on the
    server while the response is lost in transit. The retry then writes the rows again.

    That is exactly what happened: 219 duplicate rows across 20 companies in
    research_people, each person present exactly twice, in one run, at contiguous ids.
    Because research_people is deliberately append-only (a repeat sighting is how you
    see someone changed title), nothing upstream could absorb it, and every duplicated
    company reported roughly double its real headcount into synthesis.

    So retry is now OPT-OUT per call: any non-idempotent write passes retry=False and
    fails loudly rather than risking a silent double-write. Retries still cover
    connection/timeout errors and 5xx/429 ONLY - a 4xx is our bug (bad column,
    constraint violation) and retrying just repeats it more slowly.
    """
    _check()
    url = f"{URL}/rest/v1/{table}"
    attempts = RETRIES if retry else 1
    last = None
    for attempt in range(attempts):
        try:
            r = requests.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code in ok:
                return r
            if r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"{label} {table} -> {r.status_code}: {r.text[:300]}")
            last = RuntimeError(f"{label} {table} -> {r.status_code}: {r.text[:300]}")
        except (requests.ConnectionError, requests.Timeout) as e:
            last = RuntimeError(f"{label} {table} -> {type(e).__name__}: {str(e)[:200]}")
        if attempt < attempts - 1:
            time.sleep(2 ** attempt)          # 1s, 2s, 4s
    raise last


def select(table, params):
    """GET /rest/v1/<table>?<params>  ->  list[dict]"""
    return _send("GET", table, ok=(200,), label="select",
                 headers=H, params=params).json()


def patch(table, match, values):
    """PATCH one or more rows. `match` is {column: value} -> ?column=eq.value"""
    params = {k: f"eq.{v}" for k, v in match.items()}
    _send("PATCH", table, ok=(200, 204), label="patch",
          headers={**H, "Prefer": "return=minimal"}, params=params, json=values)


def patch_where(table, filters, values):
    """PATCH with RAW PostgREST filters - `filters` is {column: "in.(1,2,3)"} etc.

    patch() above prefixes every value with `eq.`, which is right for the common case and
    makes any other operator impossible to express: {"id": "in.(1,2)"} becomes the
    nonsense `id=eq.in.(1,2)`. Rather than hand-build a URL at the call site, this takes
    the operator verbatim.

    It exists because setting one derived value on 7,598 rows one PATCH at a time takes
    ~40 minutes, and the value is identical for every row in a group. Keep chunks to a
    few hundred ids: the limit is URL length, and PostgREST answers a too-long request
    with a 414 rather than a partial write.

    NOT retried - the same reasoning as insert(). An `in.()` PATCH is idempotent, but the
    retry-safety argument here rests on the FILTER, not on the verb, so a future caller
    passing a non-idempotent filter would inherit a guarantee that no longer holds.
    """
    _send("PATCH", table, ok=(200, 204), label="patch_where", retry=False,
          headers={**H, "Prefer": "return=minimal"}, params=dict(filters), json=values)


def insert(table, rows, on_conflict=None, upsert=False):
    """POST rows. upsert=True + on_conflict='domain' does an upsert.

    A plain insert is NOT retried. If the server commits the rows and the response is
    lost in transit, a retry writes them a second time - which is how 219 duplicate
    people landed in research_people. An upsert IS retried, because merge-duplicates
    makes the replay a no-op.

    A caller that wants both durability and safety on a plain insert should give the
    table a natural key and pass upsert=True, not ask for retries here.
    """
    # PostgREST rejects a bulk insert whose objects do not all carry an IDENTICAL key set
    # (400 PGRST102 "All object keys must match"), and it fails the whole batch, not the odd
    # row. Callers build dicts by hand and add a key on one branch only - `status_reason` on
    # over-cap rows in resolve_domains.py is the case that killed the first full-scale run
    # after all 913 companies had already resolved. Small runs hide it: the batch has to
    # actually mix both shapes to trip.
    #
    # So normalise here, in the one place that knows the wire format, rather than asking
    # eight callers to remember. Missing keys are filled with None, which PostgREST maps to
    # SQL NULL - the same value an omitted column would take on insert. Note this is NOT
    # true for an UPDATE, so it is deliberately applied only on this insert path.
    if isinstance(rows, list) and len(rows) > 1:
        allkeys = {k for row in rows if isinstance(row, dict) for k in row}
        if any(isinstance(row, dict) and row.keys() != allkeys for row in rows):
            rows = [{**{k: None for k in allkeys}, **row} for row in rows]

    params = {"on_conflict": on_conflict} if on_conflict else {}
    prefer = "return=representation"
    if upsert:
        prefer += ",resolution=merge-duplicates"
    r = _send("POST", table, ok=(200, 201), label="insert", retry=bool(upsert),
              headers={**H, "Prefer": prefer}, params=params, json=rows)
    return r.json() if r.text else []


def count(table, params=None):
    """Exact row count from the content-range header.

    Asks the SERVER to count rather than len()-ing a select: PostgREST caps a response
    at 1,000 rows and reports it only in that header, so counting client-side silently
    truncates (it stopped the suppression sync at 1,000 of 29,867).
    """
    p = dict(params or {})
    p["select"] = p.get("select", "*")
    r = _send("GET", table, ok=(200, 206), label="count",
              headers={**H, "Prefer": "count=exact", "Range": "0-0"}, params=p)
    return int(r.headers.get("content-range", "0-0/0").split("/")[-1])


# The session pooler that actually works from this machine. IPv4, so it does not
# depend on the IPv6 routing the direct host needs.
#
# This module used to say the pooler "rejected these credentials in every region
# tried" and only offered `db.<ref>.supabase.co`. As of 2026-08-18 that host does not
# resolve here AT ALL (getaddrinfo fails, not a timeout), which made DDL impossible and
# sent people to the SQL editor by hand. The pooler works fine - the earlier attempt
# just never hit the right region, and a wrong region answers with the same
# "Tenant or user not found" a bad password gives, so it reads as an auth failure.
#
# Region is NOT derivable from the project ref or any response header; find it by
# trying. Order matters only for speed.
POOLER_REGIONS = ("us-east-2", "us-east-1", "us-west-1", "ap-southeast-1",
                  "eu-west-1", "eu-west-2", "eu-central-1", "ap-northeast-1",
                  "ap-south-1", "ca-central-1", "sa-east-1", "ap-southeast-2")


def connect(autocommit=True):
    """Direct psycopg2, for DDL only (PostgREST cannot execute DDL).

    Tries the direct host first, then walks the pooler regions. Prints the working
    host so the next caller can pin it.
    """
    import psycopg2
    if not (REF and PW):
        raise SystemExit(f"PREFLIGHT: HIRING_SUPABASE_DB_PASSWORD missing from {ENV}")

    attempts = [(f"db.{REF}.supabase.co", "postgres")]
    attempts += [(f"aws-0-{r}.pooler.supabase.com", f"postgres.{REF}")
                 for r in POOLER_REGIONS]

    last = None
    for host, user in attempts:
        try:
            c = psycopg2.connect(host=host, port=5432, user=user, password=PW,
                                 dbname="postgres", connect_timeout=8,
                                 sslmode="require")
            c.autocommit = autocommit
            return c
        except Exception as e:                      # noqa: BLE001 - report, keep trying
            last = f"{host}: {str(e).strip().splitlines()[-1][:120]}"
    raise SystemExit(
        "PREFLIGHT: no Postgres route worked (direct host + "
        f"{len(POOLER_REGIONS)} pooler regions).\nLast: {last}\n"
        "Fallback: paste the .sql into the Supabase SQL editor."
    )


@contextmanager
def run(run_key, stage, geo=None):
    """Wrap a stage so it ALWAYS lands a `runs` row - success or failure.

    The digest's silence check can only work if a crashed stage still leaves a trace,
    which is why this is a context manager and not two calls a script can forget to pair.

    Each write opens its own request. A 12-hour enrichment does its work in Blitz calls,
    not in Postgres; anything held open across that gets closed by the server and the
    stage dies at the finish line with its results already committed (measured, P2
    smoke test).
    """
    state = {"counts": {}, "summary": None}
    row = insert("runs", [{"run_key": run_key, "stage": stage, "geo": geo,
                           "status": "running", "host": socket.gethostname()}])
    rid = row[0]["id"]
    try:
        yield state
    except BaseException as e:
        patch("runs", {"id": rid},
              {"status": "failed", "ended_at": _now(),
               "error": f"{type(e).__name__}: {e}"[:2000],
               "counts": state["counts"], "summary": state["summary"]})
        raise
    else:
        patch("runs", {"id": rid},
              {"status": "ok", "ended_at": _now(),
               "counts": state["counts"], "summary": state["summary"]})
