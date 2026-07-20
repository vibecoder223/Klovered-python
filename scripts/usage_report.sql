-- Read-only usage report against the app's own Postgres data — "who used it"
-- without any new tooling. Run on the Droplet:
--   psql "$ADMIN_DATABASE_URL" -f scripts/usage_report.sql
-- (ADMIN_DATABASE_URL, not DATABASE_URL — RLS on these tables would otherwise
-- scope every query to a single org, since there's no logged-in caller here.)

-- 1) Signups over time: real accounts only (guests share email = '').
select date_trunc('day', created_at)::date as day, count(*) as signups
from users
where email <> ''
group by 1
order by 1 desc
limit 30;

-- 2) Guest vs signed-in accounts, all time.
select is_anonymous, count(*) as users
from users
group by 1;

-- 3) Organizations created per day (one per signup/guest session — this is
--    close to "distinct sessions/teams that touched the tool").
select date_trunc('day', created_at)::date as day, count(*) as orgs_created
from organizations
group by 1
order by 1 desc
limit 30;

-- 4) Deals (RFPs) created per day, with how many ever got an export-worthy
--    answer set (status != 'in_progress' is whatever your pipeline sets on
--    completion — check app/routers for the actual status values in use).
select date_trunc('day', d.created_at)::date as day, count(*) as deals_created
from deals d
group by 1
order by 1 desc
limit 30;

-- 5) Uploads by kind (knowledge doc vs RFP) per day — the clearest "did they
--    actually use it" signal, since browsing alone leaves no upload_events row.
select date_trunc('day', created_at)::date as day, kind, count(*) as uploads
from upload_events
group by 1, 2
order by 1 desc, 2;

-- 6) Most active organizations (by upload volume), with their owner's email
--    where known (guests show blank).
select o.name, o.slug, count(ue.id) as uploads, min(ue.created_at) as first_seen, max(ue.created_at) as last_seen
from organizations o
join upload_events ue on ue.org_id = o.id
group by o.id, o.name, o.slug
order by uploads desc
limit 20;

-- 7) Funnel snapshot: total orgs vs orgs that ever uploaded knowledge vs orgs
--    that ever uploaded an RFP vs orgs whose questions got at least one answer.
select
  (select count(*) from organizations) as total_orgs,
  (select count(distinct org_id) from upload_events where kind = 'knowledge') as orgs_with_knowledge,
  (select count(distinct org_id) from upload_events where kind = 'rfp') as orgs_with_rfp,
  (select count(distinct d.org_id)
     from deals d
     join documents doc on doc.deal_id = d.id
     join questions q on q.document_id = doc.id
     join responses r on r.question_id = q.id) as orgs_with_answers;

-- 8) Feedback left from the "how did we do?" card: average rating, count, and
--    the most recent comments (blank comments excluded).
select round(avg(rating), 2) as avg_rating, count(*) as responses
from feedback;

select created_at, rating, comment, nullif(email, '') as email
from feedback
where comment <> ''
order by created_at desc
limit 25;

-- Note: nothing currently logs when someone clicks "Download as Word" — the
-- export happens entirely client-side (Blob download, no server round-trip),
-- so it leaves no row here. If export volume matters, that needs a small
-- POST /api/export-events call added to AnswersList.exportDocx() in
-- klovered-free — ask if you want that wired up.
