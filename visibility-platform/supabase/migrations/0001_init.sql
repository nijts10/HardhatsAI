-- =====================================================================
-- AI Visibility Platform — Phase 1 schema
-- Target: Supabase (PostgreSQL 15+)
--
-- Scope (per implementation brief, Phase 1 only):
--   multi-tenant orgs/auth, manual brand/product entry, document upload
--   with version immutability, a prompt library, and a single audit report.
-- Parsing/extraction, claims, benchmark runs and monthly reporting are
-- Phase 2+ and deliberately not modeled yet.
--
-- Design invariants (do not violate these when extending):
--   1. Multi-tenant by organization; every client-owned row needs RLS.
--   2. document_versions is append-only. A document is never edited in
--      place — a re-upload creates a new version and documents.
--      current_version_id is repointed by a trigger.
--   3. Provenance: every document_versions row records who uploaded it,
--      when, and its sha256. Phase 2 extraction will extend this down to
--      page/chunk once parsing exists.
-- =====================================================================

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------

create type platform_role as enum ('client', 'analyst', 'admin');
create type membership_role as enum ('owner', 'editor', 'viewer');

create type document_kind as enum (
  'datasheet', 'dop', 'epd', 'certificate', 'test_report',
  'installation_manual', 'brochure', 'bestektekst', 'bim_asset', 'other'
);

create type report_kind as enum ('audit', 'monthly');
create type report_status as enum ('draft', 'published');

-- ---------------------------------------------------------------------
-- Tenancy & identity
-- ---------------------------------------------------------------------

create table organizations (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  slug       text not null unique,
  country    text default 'NL',
  website    text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- 1:1 with auth.users. platform_role is orthogonal to org membership: an
-- analyst/admin is our own staff and sees across every client org, not
-- scoped to any one organization's data.
create table profiles (
  id            uuid primary key references auth.users (id) on delete cascade,
  full_name     text,
  platform_role platform_role not null default 'client',
  locale        text default 'nl',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table memberships (
  organization_id uuid not null references organizations (id) on delete cascade,
  profile_id      uuid not null references profiles (id) on delete cascade,
  role            membership_role not null default 'viewer',
  created_at      timestamptz not null default now(),
  primary key (organization_id, profile_id)
);

-- ---------------------------------------------------------------------
-- Taxonomy & commercial objects
-- ---------------------------------------------------------------------

-- Global controlled vocabulary, admin-managed — mirrors the pattern used
-- for product_categories in the HardhatsAI schema.
create table product_categories (
  id         uuid primary key default gen_random_uuid(),
  code       text not null unique,
  name_nl    text not null,
  name_en    text not null,
  created_at timestamptz not null default now()
);

create table brands (
  id              uuid primary key default gen_random_uuid(),
  organization_id uuid not null references organizations (id) on delete cascade,
  name            text not null,
  slug            text not null unique,
  website         text,
  logo_url        text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- Manual entry only in Phase 1 — no variants/specs/claims yet, those
-- arrive with the Phase 2 extraction pipeline.
create table products (
  id               uuid primary key default gen_random_uuid(),
  brand_id         uuid not null references brands (id) on delete cascade,
  category_id      uuid references product_categories (id) on delete set null,
  name             text not null,
  slug             text not null unique,
  manufacturer_ref text,
  description      text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Documents (upload only — parsing is Phase 2)
-- ---------------------------------------------------------------------

-- The stable, logical document identity. current_version_id is maintained
-- by a trigger on document_versions, never written directly.
create table documents (
  id                  uuid primary key default gen_random_uuid(),
  organization_id     uuid not null references organizations (id) on delete cascade,
  product_id          uuid references products (id) on delete set null,
  title               text,
  kind                document_kind not null default 'other',
  current_version_id  uuid,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

-- Append-only. A re-upload is a new row here, never an update to an
-- existing one — see set_updated_at-style trigger below that repoints
-- documents.current_version_id instead of mutating this table.
create table document_versions (
  id                uuid primary key default gen_random_uuid(),
  document_id       uuid not null references documents (id) on delete cascade,
  version_number    int not null,
  storage_bucket    text not null default 'documents',
  storage_path      text not null,
  original_filename text,
  mime_type         text,
  byte_size         bigint,
  sha256            text not null unique,
  uploaded_by       uuid references profiles (id) on delete set null,
  created_at        timestamptz not null default now(),
  unique (document_id, version_number)
);

alter table documents
  add constraint documents_current_version_fkey
  foreign key (current_version_id) references document_versions (id) on delete set null;

-- ---------------------------------------------------------------------
-- Prompt library (global, use-case organized — see brief)
-- ---------------------------------------------------------------------

create table prompt_library (
  id          uuid primary key default gen_random_uuid(),
  use_case    text not null,
  prompt_text text not null,
  category_id uuid references product_categories (id) on delete set null,
  language    text not null default 'nl',
  is_active   boolean not null default true,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Reports (one audit report per brand in Phase 1; 'monthly' kind exists
-- so Phase 2's recurring reporting doesn't need a schema change)
-- ---------------------------------------------------------------------

create table reports (
  id              uuid primary key default gen_random_uuid(),
  organization_id uuid not null references organizations (id) on delete cascade,
  brand_id        uuid references brands (id) on delete set null,
  kind            report_kind not null default 'audit',
  title           text not null,
  status          report_status not null default 'draft',
  summary         text,
  created_by      uuid references profiles (id) on delete set null,
  published_at    timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table report_sections (
  id             uuid primary key default gen_random_uuid(),
  report_id      uuid not null references reports (id) on delete cascade,
  section_order  int not null,
  heading        text not null,
  body           text,
  created_at     timestamptz not null default now(),
  unique (report_id, section_order)
);

-- ---------------------------------------------------------------------
-- Operations
-- ---------------------------------------------------------------------

create table audit_log (
  id               uuid primary key default gen_random_uuid(),
  organization_id  uuid references organizations (id) on delete set null,
  actor_profile_id uuid references profiles (id) on delete set null,
  action           text not null,
  entity_type      text not null,
  entity_id        uuid,
  metadata         jsonb not null default '{}'::jsonb,
  created_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------

create index on memberships (profile_id);
create index on brands (organization_id);
create index on products (brand_id);
create index on products (category_id);
create index on documents (organization_id);
create index on documents (product_id);
create index on document_versions (document_id);
create index on prompt_library (category_id);
create index on reports (organization_id);
create index on reports (brand_id);
create index on report_sections (report_id);
create index on audit_log (organization_id);
create index on audit_log (entity_type, entity_id);

-- ---------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------

create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger t_organizations_updated before update on organizations
  for each row execute function set_updated_at();
create trigger t_profiles_updated before update on profiles
  for each row execute function set_updated_at();
create trigger t_brands_updated before update on brands
  for each row execute function set_updated_at();
create trigger t_products_updated before update on products
  for each row execute function set_updated_at();
create trigger t_documents_updated before update on documents
  for each row execute function set_updated_at();
create trigger t_reports_updated before update on reports
  for each row execute function set_updated_at();

-- Auto-number versions per document so callers don't need to compute it
-- (and can't accidentally collide with an existing version).
create or replace function assign_document_version_number()
returns trigger
language plpgsql
as $$
begin
  if new.version_number is null then
    select coalesce(max(version_number), 0) + 1
      into new.version_number
      from document_versions
     where document_id = new.document_id;
  end if;
  return new;
end;
$$;

create trigger t_document_versions_number
  before insert on document_versions
  for each row execute function assign_document_version_number();

-- A new version becomes current automatically — documents is never
-- written to directly for this.
create or replace function set_current_document_version()
returns trigger
language plpgsql
as $$
begin
  update documents
     set current_version_id = new.id,
         updated_at = now()
   where id = new.document_id;
  return new;
end;
$$;

create trigger t_document_versions_set_current
  after insert on document_versions
  for each row execute function set_current_document_version();

-- Mirror auth.users into profiles on signup.
create or replace function handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, full_name, platform_role)
  values (
    new.id,
    new.raw_user_meta_data ->> 'full_name',
    coalesce((new.raw_user_meta_data ->> 'platform_role')::public.platform_role, 'client')
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger t_on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();
