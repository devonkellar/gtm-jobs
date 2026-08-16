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

## The state problem -- read before enabling any cron

Most of these scripts still read and write **`replies_log.csv`** (3.5 MB, 4,597
rows) on local disk. One script writes it; **20 scripts across three repos read
it.** It is the shared bus of the whole system.

A CI runner is stateless. The file starts empty and is discarded at the end. So:

- `--dry-run` in CI is safe and meaningful.
- A **real** run in CI would produce a partial file that nothing else can see.

That is why **no workflow here has a `schedule:` trigger yet** -- they are
`workflow_dispatch` only. The cron goes on once the CSV moves to Supabase
(`campaign_sends` / a new `campaign_replies` table, project
`mgonnoxpaqqcbtrkzmpf`). Until then the laptop remains the system of record.

## Migration status

| Job | Ported | Cron on | Notes |
|---|---|---|---|
| smartlead-sync | yes | **no** | **Verified in Actions 2026-08-16: success in 6m15s**, 33 campaigns, live reply counts, `--dry-run`. The CSV producer, so the cron is blocked on the Supabase move. |
| smartlead-campaign-stats | not yet | no | |
| smartlead-account-stats | not yet | no | |
| smartlead-deliverability | not yet | no | |
| campaign-archive | not yet | no | Already dual-writes Supabase; closest to cron-ready. |
| weekly-report | not yet | no | Needs 27 campaign README files mirrored in. |
| signal-extraction | not yet | no | |
| founding-gtm-scan | not yet | no | Dedup cursor is local state. |
| install-kpis / install-replies | not yet | no | Cores live in blue-summit. |

Staying on the laptop deliberately: **FathomSync** (writes transcripts into the
local SOS tree, which is where they are read) and **FinanceDaily** (a `.bat`
outside all three repos).

## Running a job locally

```bash
export SMARTLEAD_API_KEY=...            # or let secrets_util read sos/shared/config/.env
export SOS_ROOT=/tmp/gtm-state          # redirect the data tree away from C:\Users\Devon\sos
python scripts/smartlead_sync.py --days 3 --dry-run
```
