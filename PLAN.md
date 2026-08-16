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

- **GitHub Actions** -- 2,000 min/mo on private repos. Running everything daily
  costs ~1,996, which is not a budget, it is a coin flip: Actions rounds every
  run up to a whole minute, so one retry tips it into paid. Two jobs are over
  half the bill (CampaignArchive 510, InstallSuppression 510). **Open decision:**
  make this repo public (unlimited minutes, code becomes readable) or drop those
  two to weekly (~1,163 min, 837 headroom). Nothing ships until this is settled.
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

1. Settle the Actions cost decision (blocks everything)
2. Move `campaign_replies` to the freelance Supabase, unfiltered
3. Backfill the 14 missing people into Attio, then delete the allowlists
4. Build the reporting site, replies page first
5. Migrate the jobs, cron on only once their readers are cloud-side

## Still true from the original plan

- `replies_log.csv` is written by one job and read by twenty across three repos.
  The seam (`scripts/replies_store.py`) exists and is verified; the remaining
  readers still point at the CSV and are being left alone for now.
- The laptop stays the system of record until a job's readers are all cloud-side.
- FathomSync and FinanceDaily stay on the laptop deliberately.
