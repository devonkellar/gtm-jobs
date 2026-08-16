-- campaign_replies: the Supabase home of replies_log.csv.
-- Run once in the Supabase SQL Editor (project mgonnoxpaqqcbtrkzmpf).
-- Loader: scripts/replies_store.py  (writer: scripts/smartlead_sync.py)
--
-- WHY THIS TABLE EXISTS
-- replies_log.csv (3.5 MB, 4,596 rows) is written by one script and read by
-- twenty across three repos. It is the shared bus of the whole GTM system, and
-- a stateless CI runner cannot hold it. Until it lives here, no migrated job
-- can have a schedule: trigger.
--
-- WHY NOT REUSE campaign_sends
-- campaign_sends is one row per lead per SEQUENCE STEP (keyed on Smartlead's
-- per-send stats_id) and carries the copy we sent. This is one row per REPLY,
-- carrying what they wrote back. Different grain, different lifecycle: a reply
-- can exist for a campaign that has since been deleted from Smartlead. Bolting
-- reply_body onto campaign_sends would have made stats_id nullable and broken
-- its dedup key.
--
-- THE KEY, AND WHY IT IS NOT campaign_lead_map_id
-- Measured on the live CSV: 4,596 rows but only 2,276 distinct map_ids, and 197
-- rows have a BLANK one. A lead can reply more than once, so map_id is not a
-- reply identifier at all.
--
-- The natural key is (campaign_id, lead_email, reply_date). That leaves 4,545
-- distinct of 4,596 -- the 51 collisions are the same reply banked twice: once
-- categorised with a map_id, once as 'Uncategorized' with a blank one. Of the
-- 197 blank-map rows, 39 shadow a categorised row (drop) and 158 are genuinely
-- the only record of that reply (keep). Upserting on the natural key does
-- exactly that: the shadows collapse onto their categorised twin, the 158
-- survive.
--
-- reply_body is deliberately NOT in the key. It is the largest column and the
-- same reply can arrive with trivially different whitespace; including it would
-- resurrect the shadow duplicates it is meant to collapse.

create table if not exists campaign_replies (
    id                    bigserial primary key,

    -- natural key
    campaign_id           bigint      not null,
    lead_email            text        not null,
    reply_date            timestamptz not null,

    campaign_name         text,
    campaign_status       text,          -- ACTIVE / COMPLETED / DRAFTED / ARCHIVED
    lead_first_name       text,
    lead_last_name        text,
    lead_company          text,
    lead_category         text,          -- Interested / Wrong Person / Uncategorized / ...
    reply_body            text,
    reply_from            text,
    reply_to              text,          -- which sending mailbox got it
    smartlead_lead_id     bigint,
    campaign_lead_map_id  bigint,        -- nullable on purpose: 197 rows have none

    source                text default 'smartlead_api',
    first_seen_at         timestamptz default now(),
    updated_at            timestamptz default now(),

    unique (campaign_id, lead_email, reply_date)
);

-- Read patterns these serve, all from real callers:
--   weekly_report / campaign_stats  -> filter by campaign_id
--   install replies + Attio syncs    -> look up a person by email
--   KPI dashboard                    -> "replies this week" over reply_date
--   install KPIs                     -> positive-intent categories only
create index if not exists campaign_replies_campaign_idx on campaign_replies (campaign_id);
create index if not exists campaign_replies_email_idx    on campaign_replies (lead_email);
create index if not exists campaign_replies_date_idx     on campaign_replies (reply_date desc);
create index if not exists campaign_replies_category_idx on campaign_replies (lead_category);
