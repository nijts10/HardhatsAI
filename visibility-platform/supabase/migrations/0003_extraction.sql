-- =====================================================================
-- AI Visibility Platform — Phase 2: extraction pipeline schema
--
-- Adds: parsed page/chunk storage with embeddings, extraction run
-- provenance, a controlled claim vocabulary, and the claims fact table
-- itself. Design deliberately mirrors the HardhatsAI schema's proven
-- pattern (spec_definitions/product_specs) rather than the brief's fuller
-- suggested table list (claims/spec_attributes/certifications/standards/
-- applications/constraints/evidence_links) — standards fold into
-- spec_attributes.norm_reference, applications/constraints are just
-- claims against a text/numeric attribute, and evidence lives as columns
-- on the claim itself rather than a separate join table. One proven
-- pattern, not seven half-used tables.
--
-- Key difference from HardhatsAI: pages/chunks attach to a specific
-- document_version, not the logical document — re-uploading a document
-- produces genuinely different parsed content, and old versions' parsed
-- content must stay intact for history.
-- =====================================================================

create extension if not exists vector;

-- ---------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------

create type chunk_type as enum ('prose', 'table', 'spec_table', 'list', 'caption');
create type extraction_status as enum ('running', 'succeeded', 'failed');
create type claim_data_type as enum ('numeric', 'numeric_range', 'text', 'boolean', 'enum');

-- Mirrors spec_presence in the HardhatsAI schema — the same three-state
-- (not two) anti-hallucination mechanism: no row = never evaluated,
-- 'not_stated' = evaluated and the document is silent, NULL value on a
-- stated row is illegal (enforced by claims_value_ck below).
create type claim_presence as enum ('stated', 'derived', 'not_stated', 'manual');

-- ---------------------------------------------------------------------
-- Parsed content
-- ---------------------------------------------------------------------

create table document_pages (
  id                   uuid primary key default gen_random_uuid(),
  document_version_id  uuid not null references document_versions (id) on delete cascade,
  page_number          int not null check (page_number >= 1),
  text                 text not null default '',
  layout               jsonb,
  unique (document_version_id, page_number)
);

create table document_chunks (
  id                   uuid primary key default gen_random_uuid(),
  document_version_id  uuid not null references document_versions (id) on delete cascade,
  chunk_index          int not null,
  page_start           int not null,
  page_end             int not null,
  chunk_type           chunk_type not null default 'prose',
  heading_path         text[] not null default '{}',
  content              text not null,
  token_count          int,
  embedding            vector(1536),
  tsv                  tsvector generated always as (to_tsvector('simple', content)) stored,
  created_at           timestamptz not null default now(),
  unique (document_version_id, chunk_index)
);

create table extraction_runs (
  id                   uuid primary key default gen_random_uuid(),
  document_version_id  uuid not null references document_versions (id) on delete cascade,
  parser_version       text not null,
  prompt_version       text not null,
  model                text not null,
  embedding_model      text,
  status               extraction_status not null default 'running',
  started_at           timestamptz not null default now(),
  finished_at          timestamptz,
  stats                jsonb not null default '{}'::jsonb,
  cost_usd             numeric(10, 4),
  error                text
);

-- ---------------------------------------------------------------------
-- Controlled claim vocabulary — the extractor cannot invent keys outside
-- this table, same invariant as HardhatsAI's spec_definitions.
-- ---------------------------------------------------------------------

create table spec_attributes (
  id             uuid primary key default gen_random_uuid(),
  key            text not null unique,
  name_nl        text not null,
  name_en        text not null,
  data_type      claim_data_type not null,
  unit           text,
  is_filterable  boolean not null default true,
  enum_values    text[],
  enum_ordinals  jsonb,
  norm_reference text,
  description    text,
  category_codes text[] not null default '{}',
  created_at     timestamptz not null default now(),
  constraint spec_attributes_enum_ck check (
    data_type <> 'enum' or (enum_values is not null and enum_ordinals is not null)
  )
);

-- ---------------------------------------------------------------------
-- Claims — the typed, cited, append-only fact table.
-- ---------------------------------------------------------------------

create table claims (
  id                   uuid primary key default gen_random_uuid(),
  product_id           uuid not null references products (id) on delete cascade,
  spec_attribute_id    uuid not null references spec_attributes (id) on delete restrict,

  value_numeric        numeric,
  value_numeric_max    numeric,
  value_text           text,
  value_bool           boolean,
  value_enum           text,
  unit                 text,

  presence             claim_presence not null,
  confidence           numeric(4, 3) check (confidence between 0 and 1),

  document_version_id  uuid references document_versions (id) on delete set null,
  chunk_id             uuid references document_chunks (id) on delete set null,
  page_number          int,
  source_snippet       text,
  extraction_run_id    uuid references extraction_runs (id) on delete set null,

  is_current           boolean not null default true,
  superseded_by        uuid references claims (id) on delete set null,
  created_at           timestamptz not null default now(),

  -- A stated claim without a citation is not a claim.
  constraint claims_provenance_ck check (
    presence <> 'stated'
    or (document_version_id is not null and source_snippet is not null and page_number is not null)
  ),
  constraint claims_value_ck check (
    case
      when presence = 'not_stated' then
        value_numeric is null and value_numeric_max is null and value_text is null
        and value_bool is null and value_enum is null
      else
        (value_numeric is not null)::int + (value_text is not null)::int
        + (value_bool is not null)::int + (value_enum is not null)::int = 1
    end
  ),
  constraint claims_range_ck check (
    value_numeric_max is null
    or (value_numeric is not null and value_numeric_max >= value_numeric)
  )
);

create table certifications (
  id                   uuid primary key default gen_random_uuid(),
  product_id           uuid not null references products (id) on delete cascade,
  document_version_id  uuid references document_versions (id) on delete set null,
  scheme               text not null,
  number               text,
  issuer               text,
  issued_on            date,
  valid_until          date,
  scope_text           text,
  page_number          int,
  source_snippet       text,
  created_at           timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------

create index on document_pages (document_version_id);
create index on document_chunks (document_version_id);
create index document_chunks_embedding_idx on document_chunks
  using hnsw (embedding vector_cosine_ops) with (m = 16, ef_construction = 64);
create index document_chunks_tsv_idx on document_chunks using gin (tsv);

create index on extraction_runs (document_version_id);

create index claims_lookup_idx on claims (spec_attribute_id, value_numeric) where is_current;
create index claims_product_idx on claims (product_id) where is_current;
create unique index claims_current_uniq on claims (product_id, spec_attribute_id) where is_current;
create index on claims (document_version_id);

create index on certifications (product_id);

-- ---------------------------------------------------------------------
-- Supersede helper — mirrors supersede_spec in the HardhatsAI schema.
-- ---------------------------------------------------------------------

create or replace function public.supersede_claim(
  p_old_claim_id uuid,
  p_new_claim_id uuid
)
returns void
language sql
set search_path = public
as $$
  update claims
     set is_current    = false,
         superseded_by = p_new_claim_id
   where id = p_old_claim_id;
$$;
