-- =====================================================================
-- AI Visibility Platform — MVP brief Part 3: benchmark engine v2
--
-- Deliberately NEW tables, not a rewrite of benchmark_runs/benchmark_results
-- (0005_benchmarking.sql). That older pair stays exactly as-is and keeps
-- serving the existing `run-benchmark`/`generate-report` commands — this
-- is a structurally different system (real web-search grounding per
-- engine, 3 replicates, resolved-model tracking, cost accounting), not a
-- bigger version of the same query. Names agreed with stijn 2026-08-10:
-- visibility_runs / visibility_answers (not benchmark_runs/benchmark_answers
-- as the brief's own prose suggests, since that name is already taken).
--
-- Reads from `prompts` (MVP brief Part 2, 0014/0015), not prompt_library.
-- =====================================================================

create type visibility_engine as enum ('openai', 'anthropic', 'gemini');

create table visibility_runs (
  id                  uuid primary key default gen_random_uuid(),
  organization_id     uuid not null references organizations (id) on delete cascade,
  brand_id            uuid not null references brands (id) on delete cascade,
  category_id         uuid not null references product_categories (id) on delete restrict,
  engine              visibility_engine not null,
  requested_model     text not null,
  replicates          int not null default 3 check (replicates >= 1),
  status              extraction_status not null default 'running',
  triggered_by        uuid references profiles (id) on delete set null,
  started_at          timestamptz not null default now(),
  finished_at         timestamptz,
  -- Running total across every answer inserted under this run so far —
  -- updated as answers land, not just once at the end, so a crash mid-run
  -- leaves an accurate number behind for the cost-ceiling check on resume.
  cost_usd            numeric(10, 4) not null default 0,
  stats               jsonb not null default '{}'::jsonb,
  error               text
);

create table visibility_answers (
  id                     uuid primary key default gen_random_uuid(),
  run_id                 uuid not null references visibility_runs (id) on delete cascade,
  prompt_id              uuid not null references prompts (id) on delete restrict,
  replicate_index        int not null check (replicate_index >= 0),
  response_text          text not null,
  -- What the API actually reported back for this specific call, not what
  -- was requested (visibility_runs.requested_model) -- brief's explicit
  -- point: without this, month-over-month drift can't be attributed to
  -- remediation vs. the provider silently swapping models underneath a
  -- pinned model string. Nullable only because a provider occasionally
  -- doesn't echo one back; never populated from requested_model as a
  -- fallback -- an absent value must stay visibly absent, not silently
  -- become a guess.
  resolved_model_version text,
  -- URLs the web-search/grounding tool actually cited, in the order
  -- returned. Feeds Part 7's "browsed source URL" report field and Part
  -- 5's page-visibility cross-check -- worth capturing now rather than a
  -- second migration later.
  citations              jsonb not null default '[]'::jsonb,
  input_tokens           int,
  output_tokens          int,
  cost_usd               numeric(10, 4),
  created_at             timestamptz not null default now(),
  -- Resumability: a crashed/aborted run is continued by re-running the
  -- same run_id and skipping any (prompt_id, replicate_index) that
  -- already succeeded. Only successful calls are ever inserted here (see
  -- visibility.py) -- a failed API call is not represented as a row, it's
  -- just an absence that the next resume attempt will retry.
  unique (run_id, prompt_id, replicate_index)
);

create index on visibility_runs (organization_id);
create index on visibility_runs (brand_id);
create index on visibility_runs (status);
create index on visibility_answers (run_id);
create index on visibility_answers (prompt_id);

alter table visibility_runs    enable row level security;
alter table visibility_answers enable row level security;

-- Same shape as benchmark_runs/benchmark_results' own policies
-- (0005_benchmarking.sql): org members read their own org's runs, only
-- platform staff (the worker, running as service_role, bypasses RLS
-- entirely per CLAUDE.md anyway) can write.
create policy visibility_runs_read on visibility_runs
  for select to authenticated
  using (public.is_org_member(organization_id) or public.is_platform_staff());

create policy visibility_runs_write on visibility_runs
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());

create policy visibility_answers_read on visibility_answers
  for select to authenticated
  using (
    exists (
      select 1 from visibility_runs vr
       where vr.id = run_id
         and (public.is_org_member(vr.organization_id) or public.is_platform_staff())
    )
  );

create policy visibility_answers_write on visibility_answers
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());
