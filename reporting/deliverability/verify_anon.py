"""Prove the anon key can ONLY edit `client` and `status`. Run before every deploy.

    python verify_anon.py

WHY THIS EXISTS
---------------
The ops site is unlisted but NOT authenticated, so the anon key in the page is
readable by anyone with the link. "It only edits client" is a claim about a Postgres
grant, and a claim about security that nobody tests is just a hope.

This asserts it against the LIVE database, the same way deploy_dashboard.py does for
the hiring pipeline -- where the same class of check caught an anon key that could
delete the suppression list.

Exit 0 = safe to publish. Exit 1 = do not deploy.
"""
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

import ddb

REPO = Path(__file__).resolve().parents[3]
for _p in (REPO / ".env", Path(r"C:\Users\Devon\sos\shared\config\.env")):
    if _p.exists():
        load_dotenv(_p, override=False)

REST = f"https://{ddb.REF}.supabase.co/rest/v1"
SCHEMA = "deliverability"


def key():
    k = os.getenv("HIRING_SUPABASE_ANON_KEY")
    if not k:
        raise SystemExit("PREFLIGHT: HIRING_SUPABASE_ANON_KEY not found.")
    return k


def head(k, write=False):
    h = {"apikey": k, "Authorization": f"Bearer {k}"}
    h["Content-Profile" if write else "Accept-Profile"] = SCHEMA
    return h


def main():
    k = key()
    fails, checks = [], []

    def ok(label, cond, detail=""):
        checks.append((label, cond, detail))
        if not cond:
            fails.append(f"{label} {detail}")

    # ---- must be able to READ what the page draws --------------------------
    for table in ("domains", "domain_checks", "domain_health"):
        r = requests.get(f"{REST}/{table}", headers=head(k),
                         params={"select": "*", "limit": 1}, timeout=30)
        ok(f"read {table}", r.status_code == 200, f"-> {r.status_code}")

    # ---- must be able to write ONLY client ---------------------------------
    row = ddb.select("domains", {"select": "domain,client", "limit": 1})[0]
    dom, before = row["domain"], row["client"]

    r = requests.patch(f"{REST}/domains", headers=head(k, write=True),
                       params={"domain": f"eq.{dom}"},
                       json={"client": before}, timeout=30)
    ok("UPDATE client", r.status_code in (200, 204), f"-> {r.status_code}")

    # ---- status: allowed, but it MUST stamp provenance ---------------------
    # A hand-set status that is not marked `human` gets silently overwritten by the
    # next sync -- the failure classify.py exists to prevent -- so the trigger that
    # stamps it is load-bearing and gets asserted, not assumed.
    #
    # The subject is a PARKED domain with NO mailboxes, chosen deliberately: this test
    # moves a real row, and an earlier version grabbed the first `active` row it found,
    # which left salesauto.co (2 live mailboxes) sitting at `expired` when a later
    # assertion failed before the restore. Nothing sends from a parked zero-mailbox
    # domain, so a wrong status on one cannot mislead anyone about the fleet.
    sdom = sbefore = pbefore = None
    cands = ddb.select("domains", {"select": "domain,status,status_set_by",
                                   "mailbox_count": "eq.0", "status": "eq.parked",
                                   "limit": 1})
    if cands:
        sdom, sbefore, pbefore = (cands[0]["domain"], cands[0]["status"],
                                  cands[0]["status_set_by"])
        try:
            r = requests.patch(f"{REST}/domains", headers=head(k, write=True),
                               params={"domain": f"eq.{sdom}"},
                               json={"status": "retired"}, timeout=30)
            ok("UPDATE status", r.status_code in (200, 204), f"-> {r.status_code}")

            after = ddb.select("domains", {"select": "status_set_by",
                                           "domain": f"eq.{sdom}"})[0]
            ok("status change stamps status_set_by=human",
               after["status_set_by"] == "human", f"-> {after['status_set_by']}")

            # The CHECK constraint must reject anything outside the six statuses.
            r = requests.patch(f"{REST}/domains", headers=head(k, write=True),
                               params={"domain": f"eq.{sdom}"},
                               json={"status": "banana"}, timeout=30)
            ok("invalid status REFUSED", r.status_code not in (200, 204),
               f"-> {r.status_code} (it went through!)")

            # Editing ONLY the client must not flip a sync-managed row to human --
            # that would freeze it off the readiness ladder for good. Reset in TWO
            # steps: the trigger fires on any status change, so restoring status and
            # provenance together re-stamps 'human' and undoes half the reset.
            ddb.patch("domains", {"domain": sdom}, {"status": sbefore})
            ddb.patch("domains", {"domain": sdom}, {"status_set_by": pbefore})
            cur = ddb.select("domains", {"select": "client",
                                         "domain": f"eq.{sdom}"})[0]["client"]
            requests.patch(f"{REST}/domains", headers=head(k, write=True),
                           params={"domain": f"eq.{sdom}"},
                           json={"client": cur}, timeout=30)
            still = ddb.select("domains", {"select": "status_set_by",
                                           "domain": f"eq.{sdom}"})[0]["status_set_by"]
            ok("client-only edit does NOT stamp human", still == pbefore,
               f"-> {still} (was {pbefore})")
        finally:
            # ALWAYS. A failed assertion must not leave a real row mis-statused.
            ddb.patch("domains", {"domain": sdom}, {"status": sbefore})
            ddb.patch("domains", {"domain": sdom},
                      {"status_set_by": pbefore, "status_note": None})

    # ---- and nothing else. These MUST be refused. --------------------------
    for col, val in (("status_set_by", "human"),
                     ("mailbox_count", 0), ("registrar", "hacked"),
                     ("expires_at", "2000-01-01"), ("purpose", "brand")):
        r = requests.patch(f"{REST}/domains", headers=head(k, write=True),
                           params={"domain": f"eq.{dom}"},
                           json={col: val}, timeout=30)
        ok(f"UPDATE {col} REFUSED", r.status_code not in (200, 204),
           f"-> {r.status_code} (it went through!)")

    r = requests.delete(f"{REST}/domains", headers=head(k, write=True),
                        params={"domain": f"eq.{dom}"}, timeout=30)
    ok("DELETE domains REFUSED", r.status_code not in (200, 204),
       f"-> {r.status_code} (it went through!)")

    r = requests.post(f"{REST}/domains", headers=head(k, write=True),
                      json={"domain": "anon-probe-should-not-exist.test"}, timeout=30)
    ok("INSERT domains REFUSED", r.status_code not in (200, 201),
       f"-> {r.status_code} (it went through!)")

    r = requests.patch(f"{REST}/domain_checks", headers=head(k, write=True),
                       params={"id": "eq.1"}, json={"score": 100}, timeout=30)
    ok("UPDATE domain_checks REFUSED", r.status_code not in (200, 204),
       f"-> {r.status_code} (it went through!)")

    # ---- and CANNOT reach the SENSITIVE hiring tables in `public` -----------
    # The whole point of the schema split. An anon key that could read
    # research_people or suppression would undo it from the browser.
    #
    # `jobs`, `ads` and `runs` are deliberately EXCLUDED from this list: the hiring
    # pipeline's own dashboard at research.devonkellar.com/pipeline reads them with
    # this same key, and deploy_dashboard.py names them in its READABLE set. That is
    # a pre-existing, deliberate grant -- asserting against it here would fail on a
    # decision someone else already made for good reason, and the noise would make
    # this check ignorable. The tables that carry NAMED PEOPLE are what matter.
    for table in ("research_people", "research_inputs", "research_findings",
                  "suppression", "campaign_replies", "decisions"):
        r = requests.get(f"{REST}/{table}", headers={"apikey": k,
                                                     "Authorization": f"Bearer {k}"},
                         params={"select": "*", "limit": 1}, timeout=30)
        ok(f"public.{table} BLOCKED", r.status_code != 200 or r.json() == [],
           f"-> {r.status_code} returned rows!")

    print()
    for label, cond, detail in checks:
        print(f"  {'PASS' if cond else 'FAIL'}  {label} {'' if cond else detail}")

    # Leave the row exactly as found.
    ddb.patch("domains", {"domain": dom}, {"client": before})

    print()
    if fails:
        print(f"{len(fails)} FAILED -- DO NOT DEPLOY THE ANON KEY")
        return 1
    print(f"all {len(checks)} passed: the anon key reads the registry, writes ONLY "
          f"`client` and `status`, and a status change is always stamped human")
    return 0


if __name__ == "__main__":
    sys.exit(main())
