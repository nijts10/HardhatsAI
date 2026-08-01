-- =====================================================================
-- AI Visibility Platform — benchmark runs
--
-- Runs the prompt library against a real consumer-facing AI model
-- (ChatGPT, Gemini) with a neutral prompt — no system-level bias toward
-- the brand, no retrieval/grounding that would artificially inject it —
-- and records whether the brand actually got mentioned in the organic
-- response. This is the actual "AI visibility" measurement; the Phase 2
-- report generator only measured data coverage because this didn't exist
-- yet.
--
-- Reuses extraction_status (running/succeeded/failed) and
-- org_of_brand() from earlier migrations rather than duplicating them.
-- =====================================================================

create type benchmark_model as enum ('chatgpt', 'gemini');

create table benchmark_runs (
  id               uuid primary key default gen_random_uuid(),
  organization_id  uuid not null references organizations (id) on delete cascade,
  brand_id         uuid not null references brands (id) on delete cascade,
  model            benchmark_model not null,
  status           extraction_status not null default 'running',
  triggered_by     uuid references profiles (id) on delete set null,
  started_at       timestamptz not null default now(),
  finished_at      timestamptz,
  stats            jsonb not null default '{}'::jsonb,
  error            text
);

create table benchmark_results (
  id                uuid primary key default gen_random_uuid(),
  benchmark_run_id  uuid not null references benchmark_runs (id) on delete cascade,
  prompt_id         uuid not null references prompt_library (id) on delete cascade,
  response_text     text not null,
  brand_mentioned   boolean not null,
  -- Character offset of the brand's first mention in response_text; null
  -- when not mentioned. A rough, honest proxy for "how prominently" —
  -- not a claim of true ranking, which would need structured citations
  -- the raw chat APIs don't reliably give.
  mention_offset    int,
  created_at        timestamptz not null default now(),
  unique (benchmark_run_id, prompt_id)
);

create index on benchmark_runs (organization_id);
create index on benchmark_runs (brand_id);
create index on benchmark_results (benchmark_run_id);
create index on benchmark_results (prompt_id);

alter table benchmark_runs    enable row level security;
alter table benchmark_results enable row level security;

create policy benchmark_runs_read on benchmark_runs
  for select to authenticated
  using (public.is_org_member(organization_id) or public.is_platform_staff());

create policy benchmark_runs_write on benchmark_runs
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());

create policy benchmark_results_read on benchmark_results
  for select to authenticated
  using (
    exists (
      select 1 from benchmark_runs br
       where br.id = benchmark_run_id
         and (public.is_org_member(br.organization_id) or public.is_platform_staff())
    )
  );

create policy benchmark_results_write on benchmark_results
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());
