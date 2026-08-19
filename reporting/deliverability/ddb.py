"""Supabase access for the deliverability registry ONLY.

A thin wrapper over the pipeline's `db.py` that pins every request to the
`deliverability` schema. It exists so this code cannot reach the SDR hiring pipeline's
tables even by accident.

WHY A WRAPPER AND NOT A COPY
----------------------------
`db.py` carries retry logic that was paid for in real damage (a retried plain insert
once wrote 219 duplicate rows into research_people). Re-implementing that here would
mean re-learning it. So the transport is reused and only the SCHEMA is overridden.

THE ISOLATION, IN THREE LAYERS
------------------------------
1. Different schema. `deliverability.domains`, never `public.domains`. Nothing this
   module can name collides with a hiring table.
2. PostgREST profile headers. Every request carries Accept-Profile (reads) and
   Content-Profile (writes), so the server resolves names in `deliverability` and a
   bare table name cannot fall through to `public`.
3. A dedicated DB role. `deliverability_rw` has USAGE on this schema and REVOKE on
   `public` -- verified 2026-08-18: it reads/writes its own tables and is denied on
   research_inputs, research_people, jobs and suppression. That layer binds whenever
   a connection authenticates as that role.

Layers 1 and 2 bind for the service-key path this module uses day to day. Layer 3 is
the backstop for anything that connects as the role directly.

The credentials are still HIRING_SUPABASE_* because it is one Supabase project (one
bill, one dashboard, one backup) -- the data is separated, not the billing account.
"""
import sys
from pathlib import Path

# VENDORED LAYOUT. In the freelance repo db.py lives in ../pipeline; here the
# whole dependency set is vendored flat into this folder, so db.py is a sibling.
# Try the sibling first and fall back to the original layout, so this one file
# works unchanged in both trees.
sys.path.insert(0, str(Path(__file__).resolve().parent))
_pipeline = Path(__file__).resolve().parents[1] / "pipeline"
if _pipeline.exists():
    sys.path.insert(0, str(_pipeline))
import db as _db  # noqa: E402

SCHEMA = "deliverability"

# PostgREST resolves unqualified table names against the profile header, so these are
# what actually keep a bare "domains" pointing at deliverability.domains.
_READ = {**_db.H, "Accept-Profile": SCHEMA}
_WRITE = {**_db.H, "Content-Profile": SCHEMA}

REF = _db.REF


def select(table, params):
    return _db._send("GET", table, ok=(200,), label="select",
                     headers=_READ, params=params).json()


def count(table, params=None):
    p = dict(params or {})
    p["select"] = p.get("select", "*")
    r = _db._send("GET", table, ok=(200, 206), label="count",
                  headers={**_READ, "Prefer": "count=exact", "Range": "0-0"}, params=p)
    return int(r.headers.get("content-range", "0-0/0").split("/")[-1])


def upsert(table, rows, on_conflict):
    """Upsert only -- this module deliberately exposes no plain insert.

    A plain insert is not idempotent, which is why `db.insert` refuses to retry one.
    Every write here has a natural key (`domain`), so upsert is always available and
    a replayed request merges instead of duplicating.

    NOTE: `db.insert` normalises a mixed-key batch by filling absent keys with None.
    That is wrong for `domains` -- a padded `status: null` would violate NOT NULL or
    blank a status a human set -- so callers must pass batches that are already
    uniform in shape. sync_registrars.py splits by shape for exactly this reason.
    """
    r = _db._send("POST", table, ok=(200, 201), label="upsert", retry=True,
                  headers={**_WRITE,
                           "Prefer": "return=representation,resolution=merge-duplicates"},
                  params={"on_conflict": on_conflict}, json=rows)
    return r.json() if r.text else []


def append(table, rows):
    """Insert HISTORY rows into an append-only table. Never overwrites.

    `domain_checks` is deliberately append-only -- "was this domain fine last week?"
    is the question actually asked when a campaign's replies fall off a cliff, and an
    overwritten row cannot answer it.

    Why not upsert(): the table's key is a `bigserial id` the database generates. An
    upsert on `id` only appends because callers happen not to send one; the day a
    caller does, it silently REPLACES a historical check instead of adding to it. This
    strips `id` outright so that cannot happen, whatever a caller passes.

    NOT retried, and that is the trade. A retried insert is not idempotent (it is how
    219 duplicate rows once landed in research_people), so a timeout here may leave a
    partial batch. Duplicate history rows would be worse than a short one: they would
    silently double-count a domain in any trend.
    """
    clean = [{k: v for k, v in r.items() if k != "id"} for r in rows]
    _db._send("POST", table, ok=(200, 201), label="append", retry=False,
              headers={**_WRITE, "Prefer": "return=minimal"}, json=clean)


def patch(table, match, values):
    params = {k: f"eq.{v}" for k, v in match.items()}
    _db._send("PATCH", table, ok=(200, 204), label="patch",
              headers={**_WRITE, "Prefer": "return=minimal"},
              params=params, json=values)


def patch_where(table, filters, values):
    """PATCH with RAW PostgREST filters -- `{"domain": "in.(a.com,b.com)"}`.

    patch() prefixes every value with `eq.`, which makes any other operator
    impossible to express: {"domain": "in.(a,b)"} becomes the nonsense
    `domain=eq.in.(a,b)`. This takes the operator verbatim so a bulk classify is one
    request per 100 rows instead of one per row.

    Keep chunks to a few hundred: the limit is URL length, and PostgREST answers a
    too-long request with a 414 rather than a partial write.

    Not retried. An `in.()` PATCH is idempotent, but the safety of that rests on the
    FILTER rather than the verb, so a future caller passing a non-idempotent filter
    would inherit a guarantee that no longer holds. Same reasoning as db.patch_where.
    """
    _db._send("PATCH", table, ok=(200, 204), label="patch_where", retry=False,
              headers={**_WRITE, "Prefer": "return=minimal"},
              params=dict(filters), json=values)


if __name__ == "__main__":
    print(f"schema  : {SCHEMA}")
    print(f"project : {REF}")
    print(f"domains : {count('domains')}")
    for st in ("new", "active", "parked", "retired", "expired"):
        n = count("domains", {"status": f"eq.{st}"})
        if n:
            print(f"  {st:8} {n:>4}")
