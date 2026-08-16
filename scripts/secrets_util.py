#!/usr/bin/env python3
"""
One way to get a credential, on the laptop and in CI.

WHY THIS EXISTS
The Smartlead key was hardcoded as a literal in smartlead_sync.py,
smartlead_account_stats.py, smartlead_campaign_stats.py and weekly_report.py.
Those four files are exactly the ones that have to go to GitHub for the cloud
migration, so the literal had to come out before the first push.

A bare os.environ swap would have broken the laptop, because Task Scheduler
does not set SMARTLEAD_API_KEY and nothing in those scripts loaded the shared
.env. So the lookup order is:

    1. os.environ            -- what CI (GitHub Actions secrets) provides
    2. sos/shared/config/.env -- what the laptop has today, unchanged

That means the same file works in both places with no branching at the call
site, and the laptop keeps running while the migration is in progress.

The .env path is overridable with SOS_ROOT so this module does not hardcode
C:\\Users\\Devon\\sos the way everything else here still does.
"""

import os
from pathlib import Path

_ENV_CACHE = None


def _sos_root() -> Path:
    return Path(os.environ.get("SOS_ROOT", r"C:\Users\Devon\sos"))


def _load_env_file() -> dict:
    """Parse sos/shared/config/.env once. Missing file is fine (CI has no .env)."""
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE

    _ENV_CACHE = {}
    env_path = _sos_root() / "shared" / "config" / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            _ENV_CACHE[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass  # no .env (CI) -- environment variables are the whole story there
    return _ENV_CACHE


def get_secret(name: str, required: bool = True) -> str:
    """Environment first, then the shared .env. Raises if required and absent."""
    val = os.environ.get(name) or _load_env_file().get(name)
    if not val and required:
        raise RuntimeError(
            f"{name} not set. Export it, or add it to "
            f"{_sos_root() / 'shared' / 'config' / '.env'}."
        )
    return val or ""
