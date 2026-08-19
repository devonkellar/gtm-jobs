# gtm-jobs

Scheduled GTM jobs, running in GitHub Actions instead of on Devon's laptop.

This repo exists because ~20 jobs ran on one Windows machine under Task Scheduler.
When the laptop slept, they did not run. When a folder moved, seven of them broke
silently for over a week. This is where they move to.

## Why this is a new repo and not `sos`

The jobs' code lives in `sos/shared/scripts/`, but SOS itself cannot be pushed:

| | |
|---|---|
| SOS on disk | **8.4 GB** |
| `shared/data/` (2.9 GB Crunchbase DB, 97 MB campaign archive) | 3.1 GB |
| `functions/` | 4.7 GB |
| **Code these jobs actually need** | **~210 KB, 11 files** |

`shared/data/` is not gitignored in SOS and 71 files from it are already tracked,
including purchased lead PII. SOS's local history also contains a hardcoded live
Smartlead key. Pushing it would publish both. So this repo takes only the code,
with no inherited history.

**SOS stays a local-only working folder.** It is not going to GitHub.

## Layout

```
scripts/     the 11 vendored job scripts (see "Vendored modules" below)
.github/workflows/   one workflow per job
requirements.txt     derived from actual imports, not guessed
```

## Vendored modules

`sheets_client.py`, `blitz_client.py`, `net_wait.py` and `secrets_util.py` are
copies of the SOS originals, not imports. On the laptop, blue-summit and
freelance scripts reach into SOS with a hardcoded
`sys.path.insert(0, r"C:\Users\Devon\sos\shared\scripts")`. In a container there
is no such path.

Vendoring was chosen over a pip package or a submodule: no build step, no
checkout friction, one repo. The cost is that these four files exist in two
places until SOS is retired. **If you change one here, change it in SOS too** --
until the laptop copies are switched off, they are the ones still running.

## Secrets

Set as repository secrets. Never commit a key.

| Secret | Used by |
|---|---|
| `SMARTLEAD_API_KEY` | every smartlead job |
| `SUPABASE_SERVICE_KEY` | archive, signal extraction |
| `BLITZ_API_KEY` | founding-gtm scan |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | anything writing a Google Sheet (base64, written to a temp file at job start) |

`scripts/secrets_util.py` reads the environment first and falls back to
`sos/shared/config/.env`. That is deliberate: the same file works in CI and on
the laptop, so the laptop keeps running during the migration.

## The state problem -- SOLVED for replies, still real for everything else

**This section used to say no workflow here had a `schedule:` trigger. Two do.**
`smartlead-sync` runs at 05:30 UTC and `reporting-site` at 06:10 UTC, in that
order, because the site reads what the sync just wrote.

The problem it describes was real: `replies_log.csv` was the shared bus, a CI
runner is stateless, and a real run in CI would have produced a partial file
nothing else could see. That is fixed for replies. `scripts/replies_store.py`
is the seam; the reply log lives in Supabase (`campaign_replies`, project
`ddpxbmsiiwtjjpsguege` -- the FREELANCE project, not Smartbound's
`mgonnoxpaqqcbtrkzmpf` this section used to name); `smartlead_sync.py` reads its
dedup set from the same place it writes, so it holds nothing on disk and runs
anywhere. The CSV write was dropped on 2026-08-19 and **nothing writes that file
now** -- see PLAN.md, "The CSV is dead", including the one job that has not
noticed.

**The rule still stands for every job not yet ported.** Local state is what
blocks a cron, so before enabling one, find what the job reads and writes:
`founding-gtm-scan`'s dedup cursor and FathomSync's transcript tree are the two
known cases, and neither has a seam yet.

## Migration status

| Job | Ported | Cron on | Notes |
|---|---|---|---|
| smartlead-sync | yes | **YES, 05:30 UTC** | Cron enabled 2026-08-19. Holds no local state: writes `campaign_replies` and reads its dedup set from there. Took three failures the same morning to get there -- int `campaign_id` from the API, PostgREST's inability to name an expression index as a conflict target, and a DB password the workflow never passed. |
| smartlead-campaign-stats | not yet | no | Already on the seam. Open question is where its output goes now sheets are being retired, not the port. |
| smartlead-account-stats | not yet | no | |
| smartlead-deliverability | not yet | no | Already on the seam. Its page is already built in CI by `reporting-site`. |
| campaign-archive | not yet | no | **Next up.** Already dual-writes Supabase and already reads replies through the seam, so it needs a workflow and nothing else. |
| weekly-report | not yet | no | Needs 27 campaign README files mirrored in. |
| signal-extraction | not yet | no | |
| founding-gtm-scan | not yet | no | Dedup cursor is local state. |
| install-kpis / install-replies | not yet | no | Cores live in blue-summit. |

Staying on the laptop deliberately: **FinanceDaily** (a `.bat` outside all three
repos, ADR-0012). **FathomSync is no longer on this list** -- it was excluded for
the file-is-the-interface problem, not a privacy rule, and Devon confirmed on
2026-08-19 that the transcripts can live in Supabase. It needs a seam like
`replies_store.py`, then it migrates. Three readers, not twenty-seven.

## Running a job locally

```bash
export SMARTLEAD_API_KEY=...            # or let secrets_util read sos/shared/config/.env
export SOS_ROOT=/tmp/gtm-state          # redirect the data tree away from C:\Users\Devon\sos
python scripts/smartlead_sync.py --days 3 --dry-run
```
