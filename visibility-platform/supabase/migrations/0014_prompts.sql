-- =====================================================================
-- AI Visibility Platform — prompts table (MVP brief Part 2)
--
-- Deliberately a new table, not a rename/migration of prompt_library:
-- prompt_library stays exactly as-is and keeps serving the existing
-- run-benchmark/generate-report commands. prompts is the table Part 3's
-- new benchmark engine will read from going forward (decided with stijn
-- 2026-08-09 -- prompt_library structurally can't support per-intent
-- scoring (Part 6) or prompt-set versioning (Part 3's
-- benchmark_runs.prompt_set_version) without becoming this same shape
-- anyway, so no reason to maintain both as parallel designs).
-- =====================================================================

create type prompt_intent as enum (
  'spec_constrained', 'application_driven', 'comparative', 'compliance',
  'sustainability', 'brand_direct', 'problem_driven'
);

create table prompts (
  id          uuid primary key default gen_random_uuid(),
  category_id uuid references product_categories (id) on delete set null,
  locale      text not null default 'nl',
  intent      prompt_intent not null,
  text        text not null,
  is_active   boolean not null default true,
  created_at  timestamptz not null default now(),
  version     int not null default 1
);

create index on prompts (category_id);
create index on prompts (intent);

alter table prompts enable row level security;

-- Same shape as prompt_library's own policies (0002_rls.sql): global
-- reference data, readable by anyone signed in, writable by staff only.
create policy prompts_read on prompts
  for select to authenticated using (true);
create policy prompts_write on prompts
  for all to authenticated using (public.is_platform_staff()) with check (public.is_platform_staff());
