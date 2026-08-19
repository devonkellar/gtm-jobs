# Where this is going

Re-scoped 2026-08-16. Supersedes the original migration plan on three points:
**no filters, no sheets, no cost.**

## What changed

| Before | Now |
|---|---|
| Track the 22 "install" campaigns | **Track every campaign.** No allowlist anywhere. |
| Push install replies to Attio | **Push every positive reply to Attio.** |
| Reports live in Google Sheets | **Reports are webpages**, every number drills down to its rows. |
| Reply log in Smartbound's Supabase | **Freelance Supabase** (447 MB free vs 46 MB). |
| Call reporting in scope | **Out of scope.** `apollo_calls` stays in the Smartbound DB, untouched. |

## Why the filters go

The install allowlist is a hand-typed dict of campaign IDs duplicated across four
places. They drifted: the sheet sync knew 22 campaigns, the Attio sync knew 15,
and `INSTALL-CAMPAIGNS.md` -- the file the code calls its "source of truth", which
nothing actually reads -- listed 25.

The cost of that drift, measured on the live log: **14 people who replied
positively between 17 July and 12 August never reached the CRM.** Not flagged,
not logged. `sync_install_replies_attio.py:144` reads

```python
camp = resolve_campaign(r)
if not camp:
    continue          # not on my list, so this reply does not exist
```

and the job then reports success, because from its point of view there was
nothing to do.

Removing the allowlist removes the whole failure class. A new campaign needs no
code edit, and nothing can be silently excluded because there is nothing doing
the excluding.

## Why the freelance Supabase

Smartbound's project (`mgonnoxpaqqcbtrkzmpf`) is at 454 MB of the 500 MB free
tier. 220 MB of that is `apollo_calls`, which must stay: 10 live client trial
portals on reporting.smartbound.ai read it, three of them `active`, the newest
created 11 days ago. Dropping it would blank a client-facing page.

The freelance project (`ddpxbmsiiwtjjpsguege`) is at 53 MB with **447 MB free**,
and it is Devon's own. Reporting data goes there. Call reporting stays where it
is and is not part of this site.

## The cost ceiling

Free tier or it does not ship.

- **GitHub Actions** -- **SETTLED 2026-08-16: this repo is PUBLIC**, which makes
  Actions minutes unlimited and free, permanently. Private would have cost ~1,996
  of 2,000 minutes for a daily schedule -- not a budget, a coin flip, since
  Actions rounds every run up to a whole minute and one retry tips it into paid
  (CampaignArchive and InstallSuppression are 510 each). Railway was priced as
  the alternative and rejected: its free tier is $1/mo of credit, the realistic
  plan is Hobby at **$5/mo minimum**, and our whole workload is ~$2.50/mo of
  compute -- paying a floor for less usage than it covers.

  **What public means in practice.** No credentials are in the repo: every key is
  a GitHub Secret, `.gitignore` blocks `.env*`, `*.csv` and `_state/`, and the
  full history was scanned for token shapes before the switch. Two real prospect
  email addresses had been used as worked examples in a comment and a docstring;
  those were scrubbed from files AND from commit messages with filter-branch, the
  backup refs dropped and the objects gc'd, verified zero across every commit.
  **The standing rule: this repo is world-readable. No key, no lead, no client
  name, no third party's email goes in it -- not in code, not in a commit
  message.** What IS public is the job logic, which is the accepted trade.
- **Supabase** -- 500 MB. Replies at ~1.9 KB/row; even 10x growth is ~85 MB.
- **Cloudflare Pages** -- free, unlimited bandwidth. Already hosting
  dk-morning-brief.

## The reporting site

One site replacing every current sheet. The rule: **every number is a link.**
"23 replies this week" opens the 23 replies, with who, which campaign, and what
they wrote. No number exists that you cannot open.

Pages: campaign stats, KPIs, replies, meetings. Reads Supabase directly, so it
is live rather than a nightly snapshot, and there is no sheet to fall out of
sync with.

## Order of work

1. ~~Settle the Actions cost decision~~ **done -- public repo, unlimited free**
2. Move `campaign_replies` to the freelance Supabase, unfiltered
3. Backfill the 14 missing people into Attio, then delete the allowlists
4. Build the reporting site, replies page first
5. Migrate the jobs, cron on only once their readers are cloud-side
   - **smartlead-sync: DONE 2026-08-19.** Every reader cut over to
     `replies_store`, the CSV write dropped, the dedup set moved to Supabase,
     cron enabled at 05:30 UTC (before reporting-site at 06:10). The job now
     holds nothing on disk. First job fully off the laptop.
   - **Next, in this order, and the order is the argument:**
     1. **Cut over or kill `blue-summit/sync_install_replies.py`** -- not a
        migration, a live job silently reporting success against a frozen file
        (see "The CSV is dead"). Smallest change here, and the only one that is
        currently losing data.
     2. **Delete the three dead `REPLIES_CSV` constants**, so the next person
        auditing this does not have to redo the grep.
     3. **campaign-archive.** Closest to cron-ready: it already dual-writes
        Supabase and already reads replies through the seam, so it needs a
        workflow and nothing else.
     4. **The two stats jobs** (`smartlead-campaign-stats`,
        `smartlead-account-stats`) -- both already on the seam; the question is
        only where their output goes now that sheets are being retired.
     5. **weekly-report** last of the Smartlead set: it needs 27 campaign README
        files mirrored in, which is a real content decision, not a port.
   - **FathomSync** is the other track and does not queue behind these: it needs
     its own seam first (below).

## The trap this hit, 2026-08-19

**A job can be green, on time, and writing to the wrong place.**

`smartlead_sync.py` exists twice. The gtm-jobs copy dual-writes Supabase then
the CSV; the SOS copy only writes the CSV. `SOS\SmartleadSync` ran the SOS
copy, so `campaign_replies` quietly stopped growing on **14 August** -- 4,545
rows against the CSV's 4,668 -- while `reporting-site` kept publishing green
every morning off five-day-old data. Nothing was broken. Nothing alarmed.

Fixed by pointing the laptop wrapper at the gtm-jobs copy, so the machine that
still owns the schedule writes to both. The CSV path is unchanged, so all 27
CSV readers carry on working.

**The rule this gives us:** when a job exists in both trees, the laptop must run
the *cloud* copy from the moment that copy is the better one. Otherwise the
cloud target rots invisibly and the migration's own progress hides it.

Same shape, same day, twice more: `assert_statuses` was added to the vendored
`attio_client.py` and not the original (install sync dead 3 days, 27 people
missing from the CRM), and `build_site.py`'s vendored copy fell a revision
behind -- CI would have deleted the live `/deliverability` page on its next
run. `reporting/check_vendored.py` now covers the pairs that must match, and
the reporting workflow refuses to publish a site missing a page its nav links
to.

## The CSV is dead, and one job has not noticed

`smartlead_sync.py` stopped writing `replies_log.csv` on 2026-08-19. Nothing
writes it now. That was the intended end state, but "20 scripts across three
repos read it" stayed in this file as a live blocker while item 5 above claimed
every reader was cut over. Both could not be true. Audited, by grep, per tree:

| Tree | Live readers of the CSV | Detail |
|---|--:|---|
| `gtm-jobs` | **0** | `weekly_report`, `smartlead_campaign_stats`, `smartlead_deliverability` all call `replies_store.load_all()`. Each still *defines* a `REPLIES_CSV` constant that nothing uses. |
| `devon-kellar-freelance` | **0** | `morning_brief.py`, `sync_install_replies_attio.py` and `reporting/build_site.py` go through the seam; the CSV survives only in comments. |
| `blue-summit` | **1** | `product/outbound-install/scripts/sync_install_replies.py` still reads `C:\Users\Devon\sos\...\replies_log.csv` for real. |
| `sos` | unverified | Its GitHub mirror was last pushed 2026-08-17, two days before the write was dropped, so it cannot answer this. Only the laptop can. |

**The one live reader is the problem.** `sync_install_replies.py` appends *new
positive install replies* to a Google Sheet, deduped against what the sheet
already holds. Pointed at a file that no longer grows, it does not fail -- it
finds nothing new, exits 0, and reports success every time it runs. This is the
same failure the "green, on time, wrong place" section above describes, read
backwards: a job reading from a source that has quietly stopped moving. It
should be cut over to the seam or switched off; leaving it is choosing a job
that lies.

**Delete the three dead `REPLIES_CSV` constants.** They are why this took an
audit to establish: a constant naming the CSV at the top of a file reads as
"this job needs the CSV" long after the code stopped using it, and it is exactly
what kept a dissolved blocker alive in this plan.

## The premise about SOS needs re-checking

The README's stated reason for this repo existing -- that SOS is a local-only
working folder which is not going to GitHub -- **no longer describes reality**,
which means the data split this repo was carved out to create is not the split
that exists.

**The specifics are deliberately not in this file: it is world-readable.** They
are in the freelance repo's session log for 2026-08-19, and they need a decision
from Devon before this README's reasoning is rewritten to match.

## Still true from the original plan

- ~~`replies_log.csv` is written by one job and read by twenty across three
  repos.~~ **Not true any more, and it contradicted item 5 above.** Audited
  2026-08-19 -- see "The CSV is dead" above. The seam
  (`scripts/replies_store.py`) is what everything reads now.
- The laptop stays the system of record until a job's readers are all cloud-side.
- FinanceDaily stays on the laptop deliberately (ADR-0012).
- **FathomSync is no longer a permanent exception.** It was excluded because it
  writes transcripts into the local SOS tree and three things read them from
  disk -- the same file-is-the-interface problem as `replies_log.csv`, not a
  privacy rule. Devon confirmed on 2026-08-19 that the transcripts can live in
  Supabase, so it needs a seam like `replies_store.py` and then it migrates.
  Three readers, not twenty-seven: it is the easier of the two.
