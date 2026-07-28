-- =====================================================================
-- HardhatsAI — core schema
-- Target: Supabase (PostgreSQL 15+) with pgvector >= 0.8
--
-- Design invariants (do not violate these when extending):
--   1. Every extracted technical fact carries provenance: document, page,
--      and the literal source snippet it was read from. See product_specs.
--   2. "Absent" has three distinct meanings and they are NOT interchangeable:
--        - no product_specs row          -> never evaluated
--        - row with presence='not_stated'-> evaluated, the document is silent
--        - row with a NULL value column  -> illegal, blocked by check constraint
--   3. Structured requirements (EI60, Rw >= 45 dB) are filtered in SQL against
--      typed spec values. Embeddings are only used for the fuzzy remainder.
--   4. Specs are append-only: superseding writes a new row and flips is_current.
-- =====================================================================

create extension if not exists vector;
create extension if not exists pg_trgm;
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------

create type user_role as enum ('engineer', 'supplier', 'admin');

create type document_source as enum (
  'supplier_upload',   -- pushed through the supplier portal
  'crawled',           -- harvested from a public manufacturer site (MVP path)
  'manual'             -- entered by an admin
);

create type document_kind as enum (
  'datasheet', 'dop', 'epd', 'certificate', 'test_report',
  'installation_manual', 'bim_asset', 'brochure', 'other'
);

create type document_status as enum (
  'uploaded', 'queued', 'parsing', 'parsed', 'extracting',
  'indexed', 'failed', 'superseded'
);

create type chunk_type as enum ('prose', 'table', 'spec_table', 'list', 'caption');

create type spec_data_type as enum ('numeric', 'numeric_range', 'text', 'boolean', 'enum');

-- How a spec value came to exist. Drives the anti-hallucination constraints.
create type spec_presence as enum (
  'stated',      -- literally present in the source document
  'derived',     -- computed from stated values (e.g. Rd from lambda + thickness)
  'not_stated',  -- we looked for it in this document and it is not there
  'manual'       -- entered/overridden by a human
);

create type product_status as enum ('draft', 'published', 'archived');

create type selection_action as enum (
  'viewed', 'opened_detail', 'saved_to_project', 'written_to_revit', 'rejected'
);

create type job_status as enum ('pending', 'running', 'succeeded', 'failed', 'dead');

-- ---------------------------------------------------------------------
-- Identity & tenancy
-- ---------------------------------------------------------------------

create table profiles (
  id            uuid primary key references auth.users (id) on delete cascade,
  full_name     text,
  role          user_role not null default 'engineer',
  company_name  text,
  country       text default 'NL',
  locale        text default 'nl',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table supplier_companies (
  id                 uuid primary key default gen_random_uuid(),
  name               text not null,
  slug               text not null unique,
  country            text default 'NL',
  website            text,
  vat_number         text,
  logo_url           text,
  -- Unclaimed companies exist from day one: we ingest public documentation
  -- before any supplier signs up. claimed_at flips when they take ownership.
  claimed_by         uuid references profiles (id) on delete set null,
  claimed_at         timestamptz,
  -- 0..1, recomputed by the pipeline. Feeds ranking (see search_products).
  data_completeness  numeric(4, 3) not null default 0
                     check (data_completeness between 0 and 1),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create table supplier_members (
  supplier_company_id uuid not null references supplier_companies (id) on delete cascade,
  profile_id          uuid not null references profiles (id) on delete cascade,
  is_owner            boolean not null default false,
  created_at          timestamptz not null default now(),
  primary key (supplier_company_id, profile_id)
);

-- ---------------------------------------------------------------------
-- Taxonomy & controlled spec vocabulary
-- ---------------------------------------------------------------------

create table product_categories (
  id         uuid primary key default gen_random_uuid(),
  parent_id  uuid references product_categories (id) on delete restrict,
  code       text not null unique,        -- e.g. 'systeemwand', 'isolatie_plaat'
  name_nl    text not null,
  name_en    text not null,
  created_at timestamptz not null default now()
);

-- The controlled vocabulary. This is what makes hard filtering possible
-- instead of free-form key/value soup. Do not let the extractor invent keys:
-- it must map to a row here or emit nothing.
create table spec_definitions (
  id             uuid primary key default gen_random_uuid(),
  key            text not null unique,     -- snake_case, stable, machine-facing
  name_nl        text not null,
  name_en        text not null,
  data_type      spec_data_type not null,
  unit           text,                     -- canonical SI-ish unit, e.g. 'mm', 'W/mK', 'dB'
  is_filterable  boolean not null default true,
  -- For enum types: allowed values plus their rank. Rank is ordered
  -- BEST = HIGHEST so that "at least Euroclass B" becomes ordinal >= rank('B').
  enum_values    text[],
  enum_ordinals  jsonb,
  norm_reference text,                     -- e.g. 'EN 13501-2', 'NEN 1068'
  description    text,
  -- Categories this spec is relevant to. Empty = applies to everything.
  category_codes text[] not null default '{}',
  created_at     timestamptz not null default now(),
  constraint spec_definitions_enum_ck check (
    data_type <> 'enum' or (enum_values is not null and enum_ordinals is not null)
  )
);

-- ---------------------------------------------------------------------
-- Documents & parsed content
-- ---------------------------------------------------------------------

create table documents (
  id                     uuid primary key default gen_random_uuid(),
  supplier_company_id    uuid references supplier_companies (id) on delete set null,
  uploaded_by            uuid references profiles (id) on delete set null,
  source                 document_source not null default 'supplier_upload',
  source_url             text,
  storage_bucket         text not null default 'supplier-documents',
  storage_path           text not null,
  original_filename      text,
  mime_type              text,
  byte_size              bigint,
  -- Idempotency key for the whole pipeline. Re-uploading the same bytes
  -- must never produce a second parse run.
  sha256                 text not null unique,
  language               text,
  kind                   document_kind not null default 'other',
  title                  text,
  publication_date       date,
  revision_label         text,
  supersedes_document_id uuid references documents (id) on delete set null,
  status                 document_status not null default 'uploaded',
  page_count             int,
  parse_error            text,
  parsed_at              timestamptz,
  indexed_at             timestamptz,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

-- Page-level text is kept so that snippet verification and PDF highlighting
-- stay cheap. Chunks reference pages, not the other way round.
create table document_pages (
  id            uuid primary key default gen_random_uuid(),
  document_id   uuid not null references documents (id) on delete cascade,
  page_number   int not null check (page_number >= 1),
  text          text not null default '',
  -- Raw layout output from the parser (words + bboxes), used for highlighting.
  layout        jsonb,
  unique (document_id, page_number)
);

create table document_chunks (
  id            uuid primary key default gen_random_uuid(),
  document_id   uuid not null references documents (id) on delete cascade,
  chunk_index   int not null,
  page_start    int not null,
  page_end      int not null,
  chunk_type    chunk_type not null default 'prose',
  -- heading_path preserves document structure, e.g.
  -- {'Technische gegevens','Brandwerendheid'}. Prepended to the embedded text.
  heading_path  text[] not null default '{}',
  content       text not null,
  token_count   int,
  embedding     vector(1536),
  -- 'simple' config on purpose: Dutch stemming mangles tokens like EI60/S200,
  -- and exact-token recall matters more here than morphological recall.
  tsv           tsvector generated always as (to_tsvector('simple', content)) stored,
  created_at    timestamptz not null default now(),
  unique (document_id, chunk_index)
);

-- ---------------------------------------------------------------------
-- Products
-- ---------------------------------------------------------------------

create table products (
  id                  uuid primary key default gen_random_uuid(),
  supplier_company_id uuid not null references supplier_companies (id) on delete cascade,
  category_id         uuid references product_categories (id) on delete set null,
  canonical_name      text not null,
  slug                text not null unique,
  manufacturer_ref    text,               -- supplier's own article/system code
  description         text,
  status              product_status not null default 'draft',
  primary_document_id uuid references documents (id) on delete set null,
  image_url           text,
  -- Bumped whenever a superseding document changes any current spec.
  version             int not null default 1,
  search_tsv          tsvector generated always as (
                        to_tsvector('simple',
                          coalesce(canonical_name, '') || ' ' ||
                          coalesce(manufacturer_ref, '') || ' ' ||
                          coalesce(description, ''))
                      ) stored,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create table product_documents (
  product_id  uuid not null references products (id) on delete cascade,
  document_id uuid not null references documents (id) on delete cascade,
  relation    document_kind not null default 'datasheet',
  primary key (product_id, document_id)
);

-- ---------------------------------------------------------------------
-- Extraction provenance
-- ---------------------------------------------------------------------

-- One row per (document, extractor version). Lets you re-extract everything
-- after a prompt change and diff the results before promoting them.
create table extraction_runs (
  id              uuid primary key default gen_random_uuid(),
  document_id     uuid not null references documents (id) on delete cascade,
  parser_version  text not null,
  prompt_version  text not null,
  model           text not null,
  embedding_model text,
  status          job_status not null default 'running',
  started_at      timestamptz not null default now(),
  finished_at     timestamptz,
  stats           jsonb not null default '{}'::jsonb,
  cost_usd        numeric(10, 4),
  error           text
);

create table product_specs (
  id                 uuid primary key default gen_random_uuid(),
  product_id         uuid not null references products (id) on delete cascade,
  spec_definition_id uuid not null references spec_definitions (id) on delete restrict,

  -- Typed value columns. Exactly one shape is populated, per data_type.
  -- numeric_range uses value_numeric as the lower bound and value_numeric_max
  -- as the upper bound (e.g. lambda 0.035–0.040 W/mK).
  value_numeric      numeric,
  value_numeric_max  numeric,
  value_text         text,
  value_bool         boolean,
  value_enum         text,
  unit               text,

  presence           spec_presence not null,
  confidence         numeric(4, 3) check (confidence between 0 and 1),

  -- Provenance. Mandatory for presence='stated'.
  document_id        uuid references documents (id) on delete set null,
  chunk_id           uuid references document_chunks (id) on delete set null,
  page_number        int,
  source_snippet     text,
  source_bbox        jsonb,
  extraction_run_id  uuid references extraction_runs (id) on delete set null,

  -- Append-only history.
  is_current         boolean not null default true,
  superseded_by      uuid references product_specs (id) on delete set null,
  created_at         timestamptz not null default now(),

  -- A stated fact without a citation is not a fact.
  constraint product_specs_provenance_ck check (
    presence <> 'stated'
    or (document_id is not null and source_snippet is not null and page_number is not null)
  ),
  -- not_stated carries no value; everything else carries exactly one.
  constraint product_specs_value_ck check (
    case
      when presence = 'not_stated' then
        value_numeric is null and value_numeric_max is null and value_text is null
        and value_bool is null and value_enum is null
      else
        (value_numeric is not null)::int + (value_text is not null)::int
        + (value_bool is not null)::int + (value_enum is not null)::int = 1
    end
  ),
  constraint product_specs_range_ck check (
    value_numeric_max is null
    or (value_numeric is not null and value_numeric_max >= value_numeric)
  )
);

create table product_certificates (
  id             uuid primary key default gen_random_uuid(),
  product_id     uuid not null references products (id) on delete cascade,
  document_id    uuid references documents (id) on delete set null,
  scheme         text not null,            -- 'CE', 'KOMO', 'DoP', 'EPD', 'FSC', ...
  number         text,
  issuer         text,
  issued_on      date,
  valid_until    date,
  scope_text     text,
  page_number    int,
  source_snippet text,
  created_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Projects & Revit context
-- ---------------------------------------------------------------------

create table projects (
  id                 uuid primary key default gen_random_uuid(),
  owner_profile_id   uuid not null references profiles (id) on delete cascade,
  name               text not null,
  revit_project_name text,
  phase              text,
  country            text default 'NL',
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create table project_members (
  project_id uuid not null references projects (id) on delete cascade,
  profile_id uuid not null references profiles (id) on delete cascade,
  can_edit   boolean not null default true,
  primary key (project_id, profile_id)
);

-- Snapshot of a Revit element at the moment a question was asked. Never
-- treated as a live mirror of the model — it is query context, not truth.
create table revit_elements (
  id             uuid primary key default gen_random_uuid(),
  project_id     uuid not null references projects (id) on delete cascade,
  unique_id      text not null,            -- Revit UniqueId, stable across sessions
  element_id     bigint,                   -- Revit ElementId, session-scoped
  category       text,                     -- 'Walls', 'Floors', 'Windows', ...
  family_name    text,
  type_name      text,
  level_name     text,
  parameters     jsonb not null default '{}'::jsonb,
  bounding_box   jsonb,
  captured_by    uuid references profiles (id) on delete set null,
  captured_at    timestamptz not null default now(),
  unique (project_id, unique_id)
);

-- ---------------------------------------------------------------------
-- Queries, answers, feedback
-- ---------------------------------------------------------------------

create table queries (
  id                  uuid primary key default gen_random_uuid(),
  profile_id          uuid not null references profiles (id) on delete cascade,
  project_id          uuid references projects (id) on delete set null,
  revit_element_id    uuid references revit_elements (id) on delete set null,
  prompt_text         text not null,
  -- Structured requirements parsed out of prompt + element context, in the
  -- exact shape search_products() consumes:
  -- [{"key":"fire_resistance_ei","op":">=","value":60}, ...]
  parsed_requirements jsonb not null default '[]'::jsonb,
  context             jsonb not null default '{}'::jsonb,
  embedding           vector(1536),
  model               text,
  latency_ms          int,
  created_at          timestamptz not null default now()
);

create table query_results (
  id                 uuid primary key default gen_random_uuid(),
  query_id           uuid not null references queries (id) on delete cascade,
  product_id         uuid not null references products (id) on delete cascade,
  rank               int not null,
  score              double precision,
  vector_score       double precision,
  keyword_score      double precision,
  -- Explicit requirement accounting, surfaced verbatim in the Revit panel.
  requirements_met   jsonb not null default '[]'::jsonb,
  requirements_unmet jsonb not null default '[]'::jsonb,
  requirements_unknown jsonb not null default '[]'::jsonb,
  reasoning          text,
  cited_chunk_ids    uuid[] not null default '{}',
  created_at         timestamptz not null default now(),
  unique (query_id, product_id)
);

create table selection_events (
  id                 uuid primary key default gen_random_uuid(),
  query_id           uuid references queries (id) on delete set null,
  profile_id         uuid not null references profiles (id) on delete cascade,
  project_id         uuid references projects (id) on delete set null,
  revit_element_id   uuid references revit_elements (id) on delete set null,
  product_id         uuid references products (id) on delete set null,
  action             selection_action not null,
  written_parameters jsonb,
  note               text,
  created_at         timestamptz not null default now()
);

create table audit_log (
  id               uuid primary key default gen_random_uuid(),
  actor_profile_id uuid references profiles (id) on delete set null,
  actor_role       user_role,
  action           text not null,
  entity_type      text not null,
  entity_id        uuid,
  metadata         jsonb not null default '{}'::jsonb,
  created_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Background job queue
--
-- Deliberately a plain table, not pgmq/Redis: parsing jobs are low-volume,
-- long-running and need to be inspectable. Swap it out when that stops
-- being true.
-- ---------------------------------------------------------------------

create table jobs (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null,               -- 'parse_document', 'extract_specs', 'embed_chunks'
  payload     jsonb not null default '{}'::jsonb,
  status      job_status not null default 'pending',
  priority    int not null default 100,
  attempts    int not null default 0,
  max_attempts int not null default 3,
  run_after   timestamptz not null default now(),
  locked_at   timestamptz,
  locked_by   text,
  last_error  text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------

create index on supplier_members (profile_id);
create index on documents (supplier_company_id);
create index on documents (status) where status <> 'indexed';
create index on documents (supersedes_document_id);
create index on document_pages (document_id);
create index on document_chunks (document_id);

-- Vector index. m/ef_construction tuned for a corpus in the 10^5–10^6 chunk
-- range; revisit if the corpus grows past that.
create index document_chunks_embedding_idx on document_chunks
  using hnsw (embedding vector_cosine_ops) with (m = 16, ef_construction = 64);
create index document_chunks_tsv_idx on document_chunks using gin (tsv);

create index on products (supplier_company_id);
create index on products (category_id);
create index on products (status);
create index products_search_tsv_idx on products using gin (search_tsv);
create index products_name_trgm_idx on products using gin (canonical_name gin_trgm_ops);

create index on product_documents (document_id);

-- The workhorse index for hard spec filtering.
create index product_specs_lookup_idx on product_specs
  (spec_definition_id, value_numeric) where is_current;
create index product_specs_product_idx on product_specs (product_id) where is_current;
create unique index product_specs_current_uniq on product_specs
  (product_id, spec_definition_id) where is_current;
create index on product_specs (document_id);

create index on product_certificates (product_id);
create index on revit_elements (project_id);
create index on queries (profile_id, created_at desc);
create index on queries (project_id);
create index on query_results (query_id);
create index on query_results (product_id);
create index on selection_events (product_id, action);
create index on selection_events (profile_id, created_at desc);
create index on audit_log (entity_type, entity_id);
create index on audit_log (created_at desc);
create index jobs_claim_idx on jobs (status, priority, run_after) where status = 'pending';

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

create trigger t_profiles_updated before update on profiles
  for each row execute function set_updated_at();
create trigger t_supplier_companies_updated before update on supplier_companies
  for each row execute function set_updated_at();
create trigger t_documents_updated before update on documents
  for each row execute function set_updated_at();
create trigger t_products_updated before update on products
  for each row execute function set_updated_at();
create trigger t_projects_updated before update on projects
  for each row execute function set_updated_at();
create trigger t_jobs_updated before update on jobs
  for each row execute function set_updated_at();

-- Mirror auth.users into profiles on signup.
create or replace function handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, full_name, role)
  values (
    new.id,
    new.raw_user_meta_data ->> 'full_name',
    coalesce((new.raw_user_meta_data ->> 'role')::public.user_role, 'engineer')
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger t_on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();
