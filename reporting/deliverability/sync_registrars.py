"""Pull both registrars + Smartlead into Supabase `domains`. Daily.

    python sync_registrars.py            # write
    python sync_registrars.py --dry-run  # show what would change, write nothing

This is the step that makes "buy a domain and it shows up in the system" true.

Writes to the `deliverability` schema ONLY, through ddb.py -- never `public`, where the
SDR hiring pipeline lives. See ddb.py for the three layers that enforce that.

WHAT THE SYNC MAY WRITE
-----------------------
Registrar facts, always: registrar, registered_at, expires_at, auto_renew,
nameservers, renewal_price, mailbox_count, last_synced.

Status, only along the READINESS LADDER (new <-> warming <-> active), plus expiry:
  * insert a newly discovered domain wherever its warmup evidence puts it
  * move it up or down the ladder as that evidence changes
  * anything -> expired once past expires_at, and back out again on renewal

`parked` and `retired` are OFF the ladder. They are statements of intent -- "we are
holding this", "we burned this" -- not facts an API can observe, so the sync never
moves a domain into or out of them.

WHAT IT MUST NEVER WRITE
------------------------
client, purpose, notes -- curation, hand-set, sync-invisible.

And any status on a row whose status_set_by='human'. Once a person has made the
call, the sync PROPOSES (prints) and moves on. Without that guard one run silently
undoes every manual decision -- the failure mode the Attio syncs were built around
("syncs create; only a human curates").

WHY DOWNGRADES ARE NOT AUTOMATIC
--------------------------------
active -> parked/retired is a human call. A mailbox disappearing from Smartlead is
ambiguous: it might be a finished campaign, or someone deleting a live sender by
mistake. Auto-parking would stop checking a domain at exactly the moment something
went wrong. So the sync flags it and you decide.
"""
import argparse
import datetime as dt
import ddb  # noqa: E402  -- pinned to the `deliverability` schema; see its docstring
import registrars  # noqa: E402
import smartlead  # noqa: E402

TABLE = "domains"

# Every status, in lifecycle order. ONE list -- the summary line silently dropped
# `warming` the day it was added because it had its own hardcoded tuple.
STATUSES = ("new", "warming", "active", "parked", "retired", "expired")

# What the weekly deliverability checker looks at. NOT just `active`.
#
# A `new` domain is where DNS is MOST likely to be broken -- no SPF, no DKIM, no MX --
# and checking only the ready ones means you discover that on the day you try to send.
# The check is what tells you a new domain is set up correctly in the first place, so
# it has to run before readiness, not after it.
#
# `parked`/`retired` are deliberately excluded: nothing sends from them, so their DNS
# is nobody's problem until someone decides otherwise. `expired` we no longer own.
CHECK_SCOPE = ("new", "warming", "active")

# The readiness ladder, lowest rung first. Used to decide DIRECTION when reporting a
# status change -- a demotion is the line worth reading first.
LADDER_ORDER = ("new", "warming", "active")

# Facts the registrar owns. Rewritten every run, no questions asked.
FACTS = ("registrar", "registered_at", "expires_at",
         "auto_renew", "nameservers", "renewal_price")


WARMUP_DAYS = 14        # the bar for "warmed", per Devon 2026-08-18


def readiness(warmup_days, mailboxes, health_ok=True):
    """What the EVIDENCE says a domain's sending readiness is.

    The lifecycle answers "can this domain send today?", NOT "does a mailbox exist".
    That distinction is the whole point: the SD Lifts set had 2 mailboxes each on day
    zero of warmup, and an earlier version of this code called that `active` -- which
    would have fed 10 domains into the weekly checker as if they were ready, while
    DOMAINS.md said plainly "nothing can send".

      new      no sender accounts, or accounts with warmup never started
      warming  warmup running but under WARMUP_DAYS, or failing health
      active   warmed >= WARMUP_DAYS and healthy

    `warmup_days` is the OLDEST mailbox's warmup age (None if none is warming), so a
    domain does not fall back to `warming` the moment a fresh mailbox is added
    alongside long-warmed ones.

    `health_ok` is the domain's latest check band, as a boolean: False only when the
    most recent row in domain_checks is RED. A RED domain drops to `warming` however
    long it has been warming -- which is what makes `active` mean "warmed AND healthy"
    rather than merely old enough.

    It DEFAULTS TRUE, and that default is load-bearing: a domain that has never been
    checked must not be demoted. Absence of evidence is not evidence of failure, and
    the alternative would dump every unchecked domain out of the sending fleet the
    first time this ran. Only a real RED demotes.
    """
    if not mailboxes or warmup_days is None:
        return "new"
    if warmup_days < WARMUP_DAYS or not health_ok:
        return "warming"
    return "active"


def decide(prior, mailboxes, expires_at, today, warmup_days=None, health_ok=True):
    """The whole status state machine, as one pure function.

    Returns (status_to_write | None, proposal | None). A None status means leave the
    row's status column exactly as it is.

    Pure and dependency-free on purpose: this is the part that can destroy manual
    work, and it is tested in test_sync_rules.py without touching a network or a
    database. Keep it that way -- do not reach for `db` or a registrar in here.
    """
    is_expired = bool(expires_at) and dt.date.fromisoformat(expires_at) < today

    want = readiness(warmup_days, mailboxes, health_ok)

    if prior is None:
        if is_expired:
            return "expired", None
        # Land on whatever the evidence says. Never manufacture 'parked' or 'retired'
        # -- those are deliberate human acts about intent, not facts an API knows.
        return want, None

    human = prior["status_set_by"] == "human"
    status = prior["status"]

    if is_expired and status != "expired":
        if human and status in ("parked", "retired"):
            # Parked/retired then lapsed is the expected end state, not a conflict.
            return "expired", None
        if human:
            return None, (status, "expired", "past expiry")
        return "expired", None

    # A renewed domain has to come back. Without 'expired' in this set a domain that
    # lapses and is then renewed by hand stays 'expired' forever -- no write, no
    # proposal, nothing printed -- and the weekly checker (which reads 'active' only)
    # never looks at it again. Not hypothetical: 32 domains expire 2026-08-26 with
    # auto-renew off, so this path runs as soon as any of them is renewed by hand.
    if not is_expired and status == "expired":
        if mailboxes:
            return ("active", None) if not human else (
                None, (status, "active", f"renewed, {mailboxes} mailbox(es)"))
        # Renewed but nothing sends from it -- back to needing a human call.
        return ("new", None) if not human else (
            None, (status, "new", "renewed, no mailbox"))

    # The readiness ladder: new <-> warming <-> active moves on evidence alone.
    # parked/retired are OFF the ladder -- they are statements of intent, so the sync
    # never moves a domain into or out of them.
    LADDER = LADDER_ORDER

    if status in LADDER and want != status:
        # Losing every mailbox is the one ambiguous case: a finished campaign, or
        # someone deleting a live sender by mistake? Dropping a domain out of the
        # sending fleet automatically would stop checking it at exactly the moment
        # something went wrong, so that one is always proposed.
        if status == "active" and not mailboxes:
            return None, (status, "new", "last mailbox disappeared")
        if human:
            return None, (status, want, _why(want, warmup_days, mailboxes))
        return want, None

    return None, None


def _why(want, warmup_days, mailboxes):
    if want == "active":
        return f"warmed {warmup_days}d"
    if want == "warming":
        return f"warming, day {warmup_days} of {WARMUP_DAYS}"
    return f"{mailboxes} mailbox(es), no warmup running"


SCHEMA_HELP = """
PREFLIGHT: `deliverability.domains` is not reachable.

Apply the schema:

  cd functions/growth/pipeline
  C:/Python314/python.exe apply_schema.py ../deliverability/schema_domains.sql

It is re-runnable. If PostgREST still 404s afterwards, the `deliverability` schema is
not on its exposed list -- the .sql sets that at the role level and NOTIFYs a reload,
so re-applying it is the fix.

Project: https://supabase.com/dashboard/project/{ref}
"""


def load_existing():
    rows, offset = {}, 0
    while True:
        # PostgREST caps a response at 1,000 rows and reports the cap only in a
        # header, so paginate rather than trusting one select.
        batch = ddb.select(TABLE, {
            # `registrar` is needed to spot the rows added by import_senders.py --
            # without it every one of them reads as "not at any registrar" forever.
            "select": "domain,status,status_set_by,mailbox_count,client,registrar",
            "order": "domain",
            "offset": offset,
            "limit": 1000,
        })
        rows.update({r["domain"]: r for r in batch})
        if len(batch) < 1000:
            return rows
        offset += 1000


def load_unhealthy():
    """Domains whose MOST RECENT check was RED.

    Reads the `domain_health` view, which already resolves "latest check per domain"
    in SQL. That LATEST matters: a domain that was RED last week and has since had its
    SPF fixed must be free to climb back to `active`, so an old RED must never hold it
    down. Reading domain_checks directly and forgetting to take the newest row per
    domain would make every historical failure permanent.

    A domain with no check at all is simply absent here, which readiness() reads as
    healthy -- see the default-True note there.
    """
    rows, offset, out = None, 0, set()
    while True:
        rows = ddb.select("domain_health", {
            "select": "domain,band",
            "order": "domain",
            "offset": offset,
            "limit": 1000,
        })
        out.update(r["domain"] for r in rows if r.get("band") == "RED")
        if len(rows) < 1000:
            return out
        offset += 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = dt.date.today()

    print("reading registrars...")
    live = registrars.fetch_all()
    print(f"  {len(live)} domains")

    # Smartlead is what classifies a domain, so a dry run wants it too -- but the key
    # lives in the Claude settings env, not a .env, and the person most likely to want
    # a preview is the one who has not exported it. Degrade instead of dying: a dry run
    # without the key still shows every registrar fact and every expiry.
    print("reading smartlead...")
    try:
        boxes = smartlead.by_domain()
        print(f"  {len(boxes)} sending domains, "
              f"{sum(v['mailbox_count'] for v in boxes.values())} mailboxes")
    except SystemExit:
        if not args.dry_run:
            raise
        boxes = {}
        print("  SKIPPED (no SMARTLEAD_API_KEY) -- classification not previewed")

    # An empty Smartlead result is indistinguishable from "every domain lost its
    # mailboxes", which would propose demoting the entire active set in one run. A key
    # can be revoked, an account emptied, or the API can degrade -- none of which
    # should be read as a fleet-wide event. Refuse to classify off nothing.
    if not boxes and not args.dry_run:
        raise SystemExit(
            "REFUSING TO RUN: Smartlead returned zero sending domains.\n"
            "That is indistinguishable from every domain losing its mailboxes, and "
            "would propose demoting the whole active set.\n"
            "Check the API/key, then re-run. Use --dry-run to inspect safely."
        )

    print("reading supabase...")
    try:
        existing = load_existing()
    except RuntimeError as e:
        if "PGRST205" in str(e) or "domains" in str(e) and "schema cache" in str(e):
            raise SystemExit(SCHEMA_HELP.format(ref=ddb.REF))
        raise
    print(f"  {len(existing)} already known")

    writes, new_rows = [], []
    promoted, expired, proposals = [], [], []

    # Domains at a registrar we cannot query (registrar='unknown', added by
    # import_senders.py) are ABSENT from `live` by definition -- nothing is wrong with
    # them. Listing them under "not at any registrar" every single run would be 25
    # permanent false alarms, and that warning only works if it is rare: it exists so a
    # registrar API blip cannot silently wipe the registry. Counted separately instead.
    unknown_reg = {d for d, r in existing.items()
                   if (r.get("registrar") or "") == "unknown"}

    unhealthy = load_unhealthy()
    if unhealthy:
        print(f"{len(unhealthy)} domain(s) RED at their last check -- these cannot "
              f"hold `active`")

    # Domains at a registrar we cannot query still need their STATUS maintained --
    # they send, so warmup evidence and the health gate apply to them exactly as they
    # do to everything else. Only their registrar FACTS are unknowable.
    #
    # Without this they are frozen at whatever status they were imported with: 18 of
    # the 25 came back RED on their first check and would have sat at `active`
    # forever, which is the precise failure the health gate exists to prevent.
    # `expires_at: None` keeps them off the expiry path, which is honest -- nobody has
    # told us when they lapse.
    # `registrar` must be carried through, NOT left absent: the facts row is built as
    # {k: d.get(k) for k in FACTS} and upserted, so a missing key writes NULL and would
    # wipe registrar='unknown' straight back out on the first sync after the import.
    unqueryable = [{"domain": d, "expires_at": None, "registrar": "unknown"}
                   for d in sorted(unknown_reg) if d not in
                   {x["domain"] for x in live}]

    for d in live + unqueryable:
        name = d["domain"]
        prior = existing.get(name)
        box = boxes.get(name, {})
        mailboxes = box.get("mailbox_count", 0)
        warmup_days = box.get("warmup_days")

        # .get, not [k]: a partial row from a future registrar path must not abort a
        # run that has already spent both registrar budgets. PostgREST maps None to NULL.
        row = {k: d.get(k) for k in FACTS}
        row["domain"] = name
        row["mailbox_count"] = mailboxes
        row["last_synced"] = dt.datetime.now(dt.timezone.utc).isoformat()

        status, proposal = decide(prior, mailboxes, d["expires_at"], today,
                                  warmup_days=warmup_days,
                                  health_ok=name not in unhealthy)

        if status:
            row["status"] = status
            row["status_set_by"] = "sync"
            if prior is None:
                new_rows.append((name, status, mailboxes))
            elif status == "expired":
                expired.append(name)
            elif status in ("active", "warming"):
                # Direction matters more than the fact of a change: a domain LEAVING
                # the sending fleet is the urgent line, and a bare "^" on every row
                # printed 14 health demotions as if they were promotions.
                rung = LADDER_ORDER.index(status) - LADDER_ORDER.index(prior["status"]) \
                    if prior and prior["status"] in LADDER_ORDER else 1
                promoted.append((name, status, mailboxes, warmup_days, rung))
        if proposal:
            proposals.append((name, *proposal))

        writes.append(row)

    gone = sorted(set(existing) - {d["domain"] for d in live} - unknown_reg)
    # A null expiry means the registrar gave us nothing parseable. Such a domain can
    # never hit the expiry path, so it must not pass silently.
    undated = sorted(d["domain"] for d in live if not d["expires_at"])

    # ---- report -------------------------------------------------------------
    print()
    if new_rows:
        print(f"NEW ({len(new_rows)}):")
        for name, status, mb in sorted(new_rows)[:20]:
            print(f"  + {name}  -> {status}" + (f"  ({mb} mailbox)" if mb else ""))
        if len(new_rows) > 20:
            print(f"  ... and {len(new_rows) - 20} more")

    if promoted:
        down = sum(1 for p in promoted if p[4] < 0)
        print(f"\nREADINESS CHANGED ({len(promoted)}"
              + (f", {down} LEAVING the sending fleet" if down else "") + "):")
        # Demotions first -- they are the ones that need a human today.
        for name, st, mb, wd, rung in sorted(promoted,
                                             key=lambda p: (p[4], p[0]))[:20]:
            age = f"warmup {wd}d" if wd is not None else "no warmup"
            arrow = "v" if rung < 0 else "^"
            print(f"  {arrow} {name:34} -> {st:8} ({mb} mailbox, {age})")
        if len(promoted) > 20:
            print(f"  ... and {len(promoted) - 20} more")

    if expired:
        print(f"\nEXPIRED ({len(expired)}):")
        for name in sorted(expired)[:20]:
            print(f"  x {name}")

    if proposals:
        print(f"\nNEEDS YOUR CALL ({len(proposals)}) -- nothing written for these:")
        for name, was, want, why in sorted(proposals):
            print(f"  ? {name}: {was} -> {want}   ({why})")

    if undated:
        print(f"\nNO PARSEABLE EXPIRY DATE ({len(undated)}) -- these can never auto-expire:")
        for name in undated[:20]:
            print(f"  ? {name}")

    if unknown_reg:
        print(f"\nAT A REGISTRAR WE CANNOT QUERY ({len(unknown_reg)}): "
              f"registrar='unknown', still checked, expiry unknown.")
        print("  (added by import_senders.py; name the registrar and they join the "
              "normal sync)")

    if gone:
        print(f"\nIN SUPABASE BUT NOT AT ANY REGISTRAR ({len(gone)}):")
        for name in gone[:20]:
            print(f"  ! {name}")
        print("  (not auto-expired -- a registrar API blip must not wipe the registry)")

    # SPLIT BY SHAPE, and never let db.py pad this batch.
    #
    # db.insert() normalises a mixed batch by filling absent keys with None, because
    # PostgREST rejects a bulk insert whose objects do not all carry an identical key
    # set. That is right for its usual callers and WRONG here: most domains are
    # unchanged and carry no `status` key at all, so padding would send
    # `status: null` for every one of them -- either violating the NOT NULL
    # constraint or, worse, blanking a status a human set.
    #
    # So the two shapes go as two batches, each internally uniform, and neither ever
    # gets padded. Rows without a status keep whatever the database already holds.
    changed = [r for r in writes if "status" in r]
    unchanged = [r for r in writes if "status" not in r]

    if args.dry_run:
        print(f"\n--dry-run: {len(changed)} rows with a status change, "
              f"{len(unchanged)} facts-only. Nothing written.")
        return 0

    # upsert=True is what makes the retry in db.py safe here: a replayed write merges
    # instead of duplicating.
    written = 0
    try:
        for batch in (changed, unchanged):
            for i in range(0, len(batch), 500):
                ddb.upsert(TABLE, batch[i:i + 500], on_conflict="domain")
                written += len(batch[i:i + 500])
    except Exception:
        # Chunks are separate requests, so a failure mid-run leaves the registry
        # partially updated. Say how far it got -- re-running is safe (upsert).
        print(f"\nFAILED after {written} of {len(writes)} rows. "
              "Re-run: upserts are idempotent.")
        raise
    print(f"\nupserted {written} rows "
          f"({len(changed)} with a status change, {len(unchanged)} facts-only)")

    # db.count() reads the content-range header, so it is not capped at the 1,000 rows
    # a bare select returns -- the number printed here is the one a human sanity-checks
    # the run against, so it must stay right past 1,000 domains.
    counts = {st: ddb.count(TABLE, {"status": f"eq.{st}"}) for st in STATUSES}
    print("status: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
