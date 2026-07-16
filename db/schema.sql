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
create table if not exists users (
  id           uuid primary key default gen_random_uuid(),
  email        text not null default '',
  is_anonymous boolean not null default true,
  created_at   timestamptz not null default now()
);

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
  created_at        timestamptz not null default now()
);

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
alter table organizations enable row level security;
alter table team_members  enable row level security;
alter table org_settings  enable row level security;
alter table deals         enable row level security;
alter table documents     enable row level security;

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

-- ---------- grants ----------
grant usage on schema public to app_user;
grant select, insert, update, delete on all tables in schema public to app_user;
alter default privileges in schema public
  grant select, insert, update, delete on tables to app_user;
grant execute on all functions in schema public to app_user;
