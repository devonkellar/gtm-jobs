#!/usr/bin/env python3
"""
check_vendored.py — fail loudly when a vendored copy drifts from its original.

WHY THIS EXISTS
build_site.py exists twice: the original in the freelance repo, and a vendored
copy here because Actions can only see this repo. Two copies of a file that
must agree is the exact failure this project already paid for once -- an
install allowlist duplicated across four places drifted to 15 / 22 / 25
entries, and the gap silently hid 9 people who had replied from the CRM.

Nothing detected that. It was found by hand, weeks later. So this check is the
detector: it compares the two files and exits non-zero when they differ.

Runs in two places, on purpose:
  - CI (reporting-site.yml) -- but only when the freelance repo is present,
    which on a public runner it never is, so there it is a no-op by design.
  - The laptop, where BOTH files exist and drift is actually possible. That is
    the run that matters.

    python reporting/check_vendored.py

Exit: 0 = identical (or original absent, nothing to compare), 1 = drifted.
"""

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (vendored copy, original) pairs.
PAIRS = [
    (HERE / "build_site.py",
     Path(r"C:\Users\Devon\devon-kellar-freelance\functions\growth\reporting"
          r"\build_site.py")),
]

# Pairs whose other side is the SOS laptop copy rather than the freelance repo.
# SOS is never checked out in CI either, so these skip there by the same rule.
SOS_SCRIPTS = Path(r"C:\Users\Devon\sos\shared\scripts")
PAIRS += [
    (HERE.parent / "scripts" / "archive_campaign_results.py",
     SOS_SCRIPTS / "archive_campaign_results.py"),
]

# The domain-health page and everything it imports, vendored 2026-08-19 so CI
# can build the second page of the ops site instead of publishing without it.
# Flat here, split across two folders in the freelance repo -- ddb.py resolves
# db.py either way, which is why it can still be compared byte for byte.
FREELANCE_GROWTH = Path(
    r"C:\Users\Devon\devon-kellar-freelance\functions\growth")
_DELIV = HERE / "deliverability"
PAIRS += [
    (HERE / "build_deliverability.py",
     FREELANCE_GROWTH / "reporting" / "build_deliverability.py"),
    (HERE / "site_shell.py",
     FREELANCE_GROWTH / "reporting" / "site_shell.py"),
    (_DELIV / "ddb.py",            FREELANCE_GROWTH / "deliverability" / "ddb.py"),
    (_DELIV / "registrars.py",     FREELANCE_GROWTH / "deliverability" / "registrars.py"),
    (_DELIV / "smartlead.py",      FREELANCE_GROWTH / "deliverability" / "smartlead.py"),
    (_DELIV / "sync_registrars.py", FREELANCE_GROWTH / "deliverability" / "sync_registrars.py"),
    (_DELIV / "verify_anon.py",    FREELANCE_GROWTH / "deliverability" / "verify_anon.py"),
    (_DELIV / "db.py",             FREELANCE_GROWTH / "pipeline" / "db.py"),
]

# NOT COMPARED, AND WHY.
#
# smartlead_sync.py, smartlead_campaign_stats.py, smartlead_deliverability.py
# and weekly_report.py exist in both trees and are SUPPOSED to differ right
# now. Each gtm-jobs copy reads and writes through replies_store (Supabase);
# each SOS copy still talks to replies_log.csv directly. That gap IS the
# migration -- it closes when the last CSV reader is cut over, not before.
#
# Listing them here would print four permanent red lines on every run. A check
# that is always failing gets skimmed past, and then the one real drift hides
# in the noise -- which is precisely how assert_statuses went missing from
# attio_client.py for three days. So they stay out until their cutover lands,
# and then they get added in the same commit.
# The vendored copy carries a banner the original does not, so a byte compare
# would always fail. The banner is exactly the block of lines starting with
# "  #" inside the module docstring; dropping every such line from BOTH files
# makes the comparison about real code and nothing else. Deliberately dumb --
# a clever parser here would be one more thing that can be subtly wrong.
def strip_banner(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.startswith("  #")).strip()


def digest(text: str) -> str:
    return hashlib.sha256(strip_banner(text).encode("utf-8")).hexdigest()[:12]


def main() -> int:
    bad = 0
    for copy, original in PAIRS:
        if not copy.exists():
            print(f"[FAIL] vendored copy missing: {copy}")
            bad = 1
            continue
        if not original.exists():
            # Expected on a CI runner: the other repo is not checked out.
            print(f"[skip] original not present, nothing to compare: {original}")
            continue
        a, b = digest(copy.read_text(encoding="utf-8")), \
            digest(original.read_text(encoding="utf-8"))
        if a == b:
            print(f"[OK]   in sync ({a}): {copy.name}")
        else:
            print(f"[FAIL] DRIFTED: {copy.name}")
            print(f"       vendored {a}  !=  original {b}")
            print(f"       copy:     {copy}")
            print(f"       original: {original}")
            print("       Fix: copy the original over the vendored file.")
            bad = 1
    return bad


if __name__ == "__main__":
    sys.exit(main())
