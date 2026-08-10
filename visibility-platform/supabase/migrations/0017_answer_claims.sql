-- =====================================================================
-- AI Visibility Platform — MVP brief Part 4: spec-diff (answer_claims)
--
-- The core differentiator: extract every technical claim an AI answer
-- makes about a named product's specs, resolve which real product it's
-- talking about, and compare the claimed value against verified ground
-- truth (claims). An AI's answer is "content"; a demonstrated wrong
-- number next to the right one, both cited, is evidence.
--
-- spec_attribute_id is NOT NULL -- a claim whose spec_key doesn't map to
-- the controlled vocabulary is discarded before insert, same rule as
-- claims (0003_extraction.sql: "the extractor cannot invent spec keys").
--
-- resolved_product_id is nullable by design: pg_trgm fuzzy-matches
-- product_name against `products` (global, not scoped to the audited
-- brand -- a competitor's product lives under a different brand_id and
-- resolving it is the point of extracting competitor claims at all).
-- Below the acceptance threshold, resolved_product_id stays null and
-- verdict is forced to 'unverifiable_product' -- see
-- answer_claims_verdict_product_ck below. Never guess silently.
--
-- verdict is computed at insert time (this is a derived/analytical
-- table, not an append-only fact table like claims -- there is no
-- "supersede", a re-run of claims extraction for the same run just
-- skips answers that already have rows; see claims_diff.py).
-- =====================================================================

create extension if not exists pg_trgm;

create type answer_claim_verdict as enum (
  'correct', 'incorrect', 'likely_confusion', 'unverifiable_product', 'unverifiable_spec'
);

create table answer_claims (
  id                   uuid primary key default gen_random_uuid(),
  answer_id            uuid not null references visibility_answers (id) on delete cascade,

  brand_name           text not null,
  product_name         text not null,
  -- Whether brand_name matches the visibility_runs.brand_id being
  -- audited (fuzzy, see claims_diff._is_own_brand) -- false means this
  -- claim is about a competitor's product, needed for share-of-voice
  -- and the "competitor cited correctly, customer cited wrong" report
  -- narrative (brief, Part 4).
  is_competitor        boolean not null default false,

  resolved_product_id  uuid references products (id) on delete set null,
  resolution_score     numeric(5, 4),

  spec_attribute_id    uuid not null references spec_attributes (id) on delete restrict,
  value_numeric        numeric,
  value_numeric_max    numeric,
  value_text           text,
  value_bool           boolean,
  value_enum           text,
  unit                 text,
  source_quote         text not null,

  verdict              answer_claim_verdict not null,
  -- The ground-truth claims row this was actually compared against.
  -- Null for 'unverifiable_product' (nothing to compare against) and for
  -- 'unverifiable_spec' when no ground-truth row exists at all (as
  -- opposed to one that exists but is 'not_stated', which IS recorded
  -- here so the report can show exactly what was checked).
  compared_claim_id    uuid references claims (id) on delete set null,
  -- Which spec_attributes.key the claim was actually verified against
  -- when verdict = 'likely_confusion' (one of spec_attribute_id's own
  -- confusable_with entries) -- e.g. an AI answer's "alpha_w" value that
  -- doesn't match the product's real alpha_w but DOES match its NRC.
  confusable_key        text,

  created_at            timestamptz not null default now(),

  constraint answer_claims_value_ck check (
    (value_numeric is not null)::int + (value_text is not null)::int
    + (value_bool is not null)::int + (value_enum is not null)::int = 1
  ),
  constraint answer_claims_range_ck check (
    value_numeric_max is null
    or (value_numeric is not null and value_numeric_max >= value_numeric)
  ),
  constraint answer_claims_resolution_ck check (
    resolved_product_id is null or resolution_score is not null
  ),
  constraint answer_claims_verdict_product_ck check (
    (verdict = 'unverifiable_product') = (resolved_product_id is null)
  ),
  constraint answer_claims_confusable_ck check (
    (confusable_key is not null) = (verdict = 'likely_confusion')
  )
);

create index on answer_claims (answer_id);
create index on answer_claims (resolved_product_id);
create index on answer_claims (spec_attribute_id);
create index on answer_claims (verdict);

-- Backs claims_diff.py's pg_trgm fuzzy product-name resolution.
create index products_name_trgm_idx on products using gin (name gin_trgm_ops);

alter table answer_claims enable row level security;

-- Same shape as visibility_answers' own policies: org members read their
-- own org's audit results (reached through visibility_answers ->
-- visibility_runs -> organization_id), only platform staff/the worker
-- (service_role, bypasses RLS per CLAUDE.md) can write.
create policy answer_claims_read on answer_claims
  for select to authenticated
  using (
    exists (
      select 1 from visibility_answers va
        join visibility_runs vr on vr.id = va.run_id
       where va.id = answer_id
         and (public.is_org_member(vr.organization_id) or public.is_platform_staff())
    )
  );

create policy answer_claims_write on answer_claims
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());
