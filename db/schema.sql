-- Klovered — self-hosted Postgres schema (no Supabase).
-- Runs on first init via docker-entrypoint-initdb.d, or apply with
-- scripts/apply_schema.py. Executed as the superuser (POSTGRES_USER).
--
-- Isolation model: the request path connects as app_user (NOBYPASSRLS) and sets
-- `app.user_id` per transaction; RLS policies scope every row to the caller's
-- org. Provisioning + workers connect as the superuser (BYPASSRLS).

create extension if not exists vector;

-- Request-path role: cannot bypass RLS. Password must match database_url.
do $$
begin
  if not exists (select from pg_roles where rolname = 'app_user') then
    create role app_user login password 'app_pw' nosuperuser nobypassrls;
  end if;
end $$;

-- ---------- tables ----------
-- A guest and a real account are both rows here; `is_anonymous` is the only
-- difference. Upgrading a guest in place (set email/password_hash, flip the
-- flag) keeps their id — so their org and every uploaded doc carry over.
create table if not exists users (
  id            uuid primary key default gen_random_uuid(),
  email         text not null default '',
  password_hash text,
  is_anonymous  boolean not null default true,
  created_at    timestamptz not null default now()
);
-- Real accounts need a unique email; guests all share email = '' so they're
-- excluded from the constraint. lower() makes it case-insensitive.
create unique index if not exists idx_users_email_unique
  on users (lower(email)) where email <> '';

create table if not exists organizations (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  slug       text unique not null,
  created_at timestamptz not null default now()
);

create table if not exists team_members (
  id         uuid primary key default gen_random_uuid(),
  org_id     uuid not null references organizations(id) on delete cascade,
  user_id    uuid not null references users(id) on delete cascade,
  role       text not null default 'owner',
  email      text not null default '',
  name       text not null default '',
  created_at timestamptz not null default now(),
  unique (org_id, user_id)
);

create table if not exists org_settings (
  org_id uuid primary key references organizations(id) on delete cascade
);

create table if not exists deals (
  id         uuid primary key default gen_random_uuid(),
  org_id     uuid not null references organizations(id) on delete cascade,
  name       text not null,
  status     text not null default 'in_progress',
  owner_id   uuid references users(id),
  created_at timestamptz not null default now()
);

create table if not exists documents (
  id                uuid primary key default gen_random_uuid(),
  deal_id           uuid not null references deals(id) on delete cascade,
  filename          text not null,
  file_path         text not null,
  file_size         bigint not null default 0,
  mime_type         text,
  processing_status text not null default 'uploaded',
  extracted_text    text,
  error_message     text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

-- The knowledge base: past proposals, security docs and policies the org
-- uploads. Retrieval draws answers from THESE — `documents` above is the RFP
-- being answered, which is a different thing entirely.
create table if not exists knowledge_documents (
  id                uuid primary key default gen_random_uuid(),
  org_id            uuid not null references organizations(id) on delete cascade,
  filename          text not null,
  file_path         text not null,
  file_size         bigint not null default 0,
  mime_type         text,
  doc_type          text not null default 'other',
  ingestion_status  text not null default 'pending',
  error_message     text,
  page_count        int,
  text_hash         text,
  uploaded_by       uuid references users(id),
  created_at        timestamptz not null default now()
);
-- Dedup guard: the same text ingested twice in one org is skipped rather than
-- re-chunked (see pipeline/ingest.py).
create unique index if not exists idx_kdocs_org_texthash
  on knowledge_documents (org_id, text_hash) where text_hash is not null;

create table if not exists document_chunks (
  id                    uuid primary key default gen_random_uuid(),
  document_id           uuid references documents(id) on delete cascade,
  -- Set for knowledge-base chunks; null for the RFP's own chunks. Retrieval
  -- searches ONLY rows where this is set.
  knowledge_document_id uuid references knowledge_documents(id) on delete cascade,
  org_id                uuid not null references organizations(id) on delete cascade,
  chunk_index           int not null default 0,
  section_title         text,
  section_path          text,
  page_start            int,
  page_end              int,
  raw_text              text,
  cleaned_text          text,
  text_for_embedding    text,
  embedding             vector(1024),
  sparse_terms          text[],
  created_at            timestamptz not null default now()
);
create index if not exists idx_chunks_embedding on document_chunks
  using hnsw (embedding vector_cosine_ops);
create index if not exists idx_chunks_sparse_terms on document_chunks using gin (sparse_terms);

create table if not exists extracted_requirements (
  id              uuid primary key default gen_random_uuid(),
  document_id     uuid not null references documents(id) on delete cascade,
  requirement_id  text not null,
  title           text,
  description     text,
  category        text,
  priority        text,
  is_mandatory    boolean default false,
  section         text,
  source_page     int,
  classification  text not null default 'must',
  topic           text not null default 'technical',
  created_at      timestamptz not null default now()
);

create table if not exists compliance_matrix (
  id                 uuid primary key default gen_random_uuid(),
  document_id        uuid not null references documents(id) on delete cascade,
  requirement_id     text not null,
  our_capability     text,
  compliance_status  text not null default 'pending'
);

create table if not exists questions (
  id              uuid primary key default gen_random_uuid(),
  document_id     uuid not null references documents(id) on delete cascade,
  requirement_id  text,
  question_text   text not null,
  category        text,
  priority        text not null default 'medium',
  status          text not null default 'todo',
  created_at      timestamptz not null default now()
);

create table if not exists responses (
  id                          uuid primary key default gen_random_uuid(),
  question_id                 uuid not null unique references questions(id) on delete cascade,
  ai_generated_draft          text,
  draft_text                  text,
  final_text                  text,
  answer_text_with_markers    text,
  tone                        text default 'technical',
  confidence                  numeric,
  gap_flag                    text,
  status                      text not null default 'requires_review',
  generated_by                text default 'ai',
  created_at                  timestamptz not null default now(),
  updated_at                  timestamptz not null default now()
);

create table if not exists citations (
  id                  uuid primary key default gen_random_uuid(),
  response_id         uuid not null references responses(id) on delete cascade,
  chunk_id            uuid,
  document_filename   text,
  section_path        text,
  page                int,
  quote               text
);

create table if not exists agent_runs (
  id             uuid primary key default gen_random_uuid(),
  document_id    uuid not null references documents(id) on delete cascade,
  agent_type     text not null,
  status         text not null,
  input_tokens   int,
  output_tokens  int,
  cost           numeric,
  error_message  text,
  result         jsonb,
  started_at     timestamptz,
  completed_at   timestamptz
);

create table if not exists jobs (
  id             uuid primary key default gen_random_uuid(),
  document_id    uuid not null references documents(id) on delete cascade,
  org_id         uuid not null references organizations(id) on delete cascade,
  stage          text not null,
  target_id      uuid,
  status         text not null default 'pending',
  attempts       int not null default 0,
  max_attempts   int not null default 3,
  error          text,
  run_after      timestamptz not null default now(),
  lease_until    timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
-- One live (non-done/dead) job per (document, stage, target) — enqueue is a
-- no-op insert-ignore against this when a job is already pending/claimed.
create unique index if not exists idx_jobs_live_unique on jobs (document_id, stage, coalesce(target_id, '00000000-0000-0000-0000-000000000000'))
  where status in ('pending', 'claimed');

-- Single-use share invites. A signed-in owner mints one to add exactly ONE
-- collaborator to their org (the free tool's cap of 2 members total). Handled
-- entirely on the admin connection (create + accept both cross org boundaries),
-- so RLS is enabled with NO request-path policy — app_user can never read a
-- token, and there is no per-tenant read path to leak them through.
create table if not exists invites (
  id           uuid primary key default gen_random_uuid(),
  token        text not null unique,
  org_id       uuid not null references organizations(id) on delete cascade,
  deal_id      uuid references deals(id) on delete set null,
  created_by   uuid references users(id),
  expires_at   timestamptz not null,
  accepted_by  uuid references users(id),
  accepted_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index if not exists idx_invites_org on invites (org_id);

-- claim_jobs: atomically lease up to p_limit pending, due jobs.
create or replace function claim_jobs(p_limit int) returns setof jobs
  language plpgsql security definer set search_path = public as
$$
begin
  return query
  update jobs set status = 'claimed', attempts = attempts + 1,
         lease_until = now() + interval '5 minutes', updated_at = now()
  where id in (
    select id from jobs
    where status = 'pending' and run_after <= now()
    order by created_at
    limit p_limit
    for update skip locked
  )
  returning *;
end;
$$;

-- recover_stuck_jobs: a claimed job whose lease expired goes back to pending.
create or replace function recover_stuck_jobs() returns void
  language sql security definer set search_path = public as
$$
  update jobs set status = 'pending', lease_until = null, updated_at = now()
  where status = 'claimed' and lease_until < now()
$$;

-- match_chunks: cosine-similarity search scoped to an org (RLS is bypassed by
-- SECURITY DEFINER, so the org filter here IS the isolation boundary for this
-- function — every caller must pass p_org_id and it must be their own).
create or replace function match_chunks(p_org_id uuid, p_embedding vector(1024), p_match_count int default 20)
returns table (
  chunk_id uuid, text text, section_path text, page_start int, page_end int,
  document_filename text, similarity float
)
language sql stable security definer set search_path = public as
-- Only knowledge-base chunks are searched (knowledge_document_id is not null):
-- answers are grounded in the org's own past documents, never in the RFP being
-- answered. Mirrors the sparse/BM25 filter in pipeline/retrieval.py — the two
-- halves of hybrid retrieval must draw from the same pool.
$$
  select
    c.id as chunk_id,
    coalesce(c.cleaned_text, c.raw_text, '') as text,
    c.section_path, c.page_start, c.page_end,
    coalesce(k.filename, '(unknown)') as document_filename,
    1 - (c.embedding <=> p_embedding) as similarity
  from document_chunks c
  join knowledge_documents k on k.id = c.knowledge_document_id
  where c.org_id = p_org_id
    and c.knowledge_document_id is not null
    and c.embedding is not null
  order by c.embedding <=> p_embedding
  limit p_match_count
$$;

-- ---------- helpers ----------
-- The verified caller, read from the per-transaction GUC set by the app.
create or replace function current_user_id() returns uuid
  language sql stable as
$$ select nullif(current_setting('app.user_id', true), '')::uuid $$;

-- SECURITY DEFINER so the policy subquery reads team_members without recursing
-- into its own RLS policy (owner is the superuser, which bypasses RLS).
create or replace function current_user_org_ids() returns setof uuid
  language sql stable security definer set search_path = public as
$$ select org_id from team_members where user_id = current_user_id() $$;

-- ---------- RLS ----------
alter table users         enable row level security;
alter table organizations enable row level security;
alter table team_members  enable row level security;
alter table org_settings  enable row level security;
alter table deals         enable row level security;
alter table documents     enable row level security;
alter table knowledge_documents     enable row level security;
alter table document_chunks         enable row level security;
alter table extracted_requirements  enable row level security;
alter table compliance_matrix       enable row level security;
alter table questions               enable row level security;
alter table responses               enable row level security;
alter table citations                enable row level security;
alter table agent_runs              enable row level security;
alter table jobs                    enable row level security;
-- invites: RLS on, NO policy — only the admin (BYPASSRLS) connection touches it.
alter table invites                 enable row level security;

-- users holds password_hash, and app_user has table-level SELECT on everything
-- in this schema — so without RLS any signed-in caller could read every
-- account's hash. SELECT-only, own-row-only: writes to users (guest creation,
-- signup upgrade) all run on the admin connection, which bypasses RLS anyway,
-- so the request path never needs insert/update/delete here.
drop policy if exists users_self on users;
create policy users_self on users for select
  using (id = current_user_id());

drop policy if exists org_member on organizations;
create policy org_member on organizations for select
  using (id in (select current_user_org_ids()));

drop policy if exists tm_self on team_members;
create policy tm_self on team_members for select
  using (user_id = current_user_id() or org_id in (select current_user_org_ids()));

drop policy if exists org_settings_ro on org_settings;
create policy org_settings_ro on org_settings for select
  using (org_id in (select current_user_org_ids()));

drop policy if exists deals_rw on deals;
create policy deals_rw on deals for all
  using (org_id in (select current_user_org_ids()))
  with check (org_id in (select current_user_org_ids()));

drop policy if exists documents_rw on documents;
create policy documents_rw on documents for all
  using (deal_id in (select id from deals where org_id in (select current_user_org_ids())))
  with check (deal_id in (select id from deals where org_id in (select current_user_org_ids())));

drop policy if exists kdocs_rw on knowledge_documents;
create policy kdocs_rw on knowledge_documents for all
  using (org_id in (select current_user_org_ids()))
  with check (org_id in (select current_user_org_ids()));

drop policy if exists chunks_rw on document_chunks;
create policy chunks_rw on document_chunks for all
  using (org_id in (select current_user_org_ids()))
  with check (org_id in (select current_user_org_ids()));

drop policy if exists reqs_rw on extracted_requirements;
create policy reqs_rw on extracted_requirements for all
  using (document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))))
  with check (document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))));

drop policy if exists cm_rw on compliance_matrix;
create policy cm_rw on compliance_matrix for all
  using (document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))))
  with check (document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))));

drop policy if exists questions_rw on questions;
create policy questions_rw on questions for all
  using (document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))))
  with check (document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))));

drop policy if exists responses_rw on responses;
create policy responses_rw on responses for all
  using (question_id in (select id from questions where document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids())))))
  with check (question_id in (select id from questions where document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids())))));

drop policy if exists citations_rw on citations;
create policy citations_rw on citations for all
  using (response_id in (select id from responses where question_id in (select id from questions where document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))))))
  with check (response_id in (select id from responses where question_id in (select id from questions where document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))))));

drop policy if exists agent_runs_ro on agent_runs;
create policy agent_runs_ro on agent_runs for select
  using (document_id in (select id from documents where deal_id in (select id from deals where org_id in (select current_user_org_ids()))));

drop policy if exists jobs_ro on jobs;
create policy jobs_ro on jobs for select
  using (org_id in (select current_user_org_ids()));

-- ---------- grants ----------
grant usage on schema public to app_user;
grant select, insert, update, delete on all tables in schema public to app_user;
alter default privileges in schema public
  grant select, insert, update, delete on tables to app_user;
grant execute on all functions in schema public to app_user;
