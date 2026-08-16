#!/usr/bin/env python3
"""Block until the network can actually resolve and reach the internet.

WHY THIS EXISTS
---------------
Every SOS morning task runs with `StartWhenAvailable=True`, so a task whose
trigger passed while the laptop was asleep fires the moment Windows wakes.
That catch-up run starts BEFORE Wi-Fi has associated and DNS is answering, so
the task dies on `socket.gaierror: [Errno 11001] getaddrinfo failed` seconds
into its run.

Measured on 2026-08-10: every task triggered 06:25-07:00 ran at 07:01 and
FAILED; every task triggered 07:45+ ran at 08:29 and SUCCEEDED. Same code, same
keys, same machine — the only difference was whether the network was up yet.
`SmartleadCampaignStats` had been failing this way most mornings.

`StartWhenAvailable` is not the problem and must stay on: without it a missed
window is simply skipped. The problem is that it starts the task at wake rather
than at connectivity. This module closes that gap.

USAGE — one line at the top of a wrapper, before any network call:

    from net_wait import wait_for_network
    wait_for_network()          # returns True, or False after the timeout

It resolves AND connects (DNS answering does not mean routable), tries several
hosts so one provider being down is not mistaken for "no internet", and returns
False rather than raising so the caller decides whether to continue.
"""

from __future__ import annotations

import socket
import time

# Several independent hosts: if only one were checked, that host being down
# would look identical to the laptop having no connection.
PROBES = [
    ("server.smartlead.ai", 443),   # the API most jobs actually need
    ("api.attio.com", 443),
    ("api.fathom.ai", 443),
    ("cloudflare.com", 443),
]

DEFAULT_TIMEOUT = 600     # 10 min: enough for a slow Wi-Fi association
DEFAULT_INTERVAL = 10


def _reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """True if the host resolves AND accepts a TCP connection.

    Both halves matter: DNS can start answering while the route is still
    unusable, which is exactly the window these jobs were dying in."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_network(timeout: int = DEFAULT_TIMEOUT,
                     interval: int = DEFAULT_INTERVAL,
                     verbose: bool = True) -> bool:
    """Block until any probe host is reachable, or `timeout` seconds pass.

    Returns True as soon as one host answers, False if the timeout expires.
    Never raises — a network check that crashes the job it is protecting would
    be worse than the failure it prevents."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        for host, port in PROBES:
            if _reachable(host, port):
                if verbose and attempt > 1:
                    waited = int(timeout - (deadline - time.monotonic()))
                    print(f"[net] up after {waited}s (reached {host})")
                return True
        if time.monotonic() >= deadline:
            if verbose:
                print(f"[net] still down after {timeout}s — giving up")
            return False
        if verbose and attempt == 1:
            print(f"[net] no connectivity yet, waiting up to {timeout}s ...")
        time.sleep(interval)


if __name__ == "__main__":
    import sys
    sys.exit(0 if wait_for_network() else 1)
