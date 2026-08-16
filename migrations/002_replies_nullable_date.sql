-- Fix 001: reply_date is not always present, and it is not part of the identity.
--
-- WHAT 001 GOT WRONG
-- It made reply_date NOT NULL and part of the unique key. The backfill died on
-- row 2,501 with a 23502. Measured on the live CSV: 1,969 of 4,596 rows (43%)
-- have a BLANK reply_date -- including 119 'Interested' and 187 'Do Not Contact'.
-- Dropping them was never an option.
--
-- WHAT THE DATA ACTUALLY IS
-- The file holds two different kinds of record under one header:
--
--   1. Reply EVENTS (2,627 rows, all dated) -- a real inbound message. One lead
--      can send many: one lead has 12 in campaign 3509953, same
--      campaign_lead_map_id, different timestamps and bodies. A genuine thread.
--      So map_id is a CONVERSATION id, never a reply id.
--
--   2. Lead STATUS records (1,969 rows, never dated, 106 with any body) -- the
--      lead's current category: Out Of Office (976), Bounce (311), Not
--      Interested (191). One per campaign+lead: 1,968 distinct of 1,969.
--      1,858 of them belong to a lead who ALSO has dated replies.
--
-- THE KEY
-- Postgres treats every NULL as distinct in a unique index, so a plain unique
-- (campaign_id, lead_email, reply_date) would let the 1,969 undated rows insert
-- without limit -- one duplicate per sync, forever. Instead the key coalesces:
--
--   unique (campaign_id, lead_email, coalesce(reply_date, '-infinity'))
--
-- Dated replies stay distinct per timestamp (the 12-message thread survives as
-- 12 rows). Undated status rows collapse to exactly one per campaign+lead, and
-- a re-sync updates it in place rather than piling up.
--
-- Expected result: 2,577 dated + 1,968 undated = 4,545 rows from 4,596 CSV
-- rows. The 51 collapsed are same-reply-banked-twice (once categorised with a
-- map_id, once Uncategorized without).

-- The partial backfill from 001 stopped mid-file, so it is not a trustworthy
-- subset. Clear it and reload cleanly -- nothing else reads this table yet.
truncate table campaign_replies;

alter table campaign_replies alter column reply_date drop not null;

alter table campaign_replies
    drop constraint if exists campaign_replies_campaign_id_lead_email_reply_date_key;

-- Expression-based, so it must be a unique INDEX, not a table constraint.
create unique index if not exists campaign_replies_natural_key
    on campaign_replies (campaign_id, lead_email, (coalesce(reply_date, '-infinity'::timestamptz)));
