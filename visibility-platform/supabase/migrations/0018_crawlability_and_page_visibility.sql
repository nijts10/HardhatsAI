-- =====================================================================
-- AI Visibility Platform — MVP brief Part 5: crawlability + page-visibility
--
-- Two independent checks, deliberately separate concerns:
--   crawlability_checks/crawlability_agents -- can a given AI crawler even
--     FETCH the brand's site at all, per its own declared robots.txt
--     policy (agent-level, brand-scoped, one fetch of one file).
--   page_spec_visibility -- for a product WITH ground truth, once a page
--     IS fetched, does the verified spec value actually appear as literal
--     text on it (product-level, per-claim, the brief's "highest-value
--     diagnostic in the whole product" -- e.g. "aw 0,85 exists only in a
--     PDF/table-image, never as page text" directly explains why an AI
--     doesn't know it even when nothing is blocking the crawl).
--
-- products.product_url is new -- nothing before Part 5 needed a
-- per-product public URL. Nullable: a product with no URL set is simply
-- not yet checkable, not an error (same "no row = not yet evaluated"
-- philosophy as claims/spec_attributes elsewhere in this schema).
--
-- crawlability_checks/crawlability_agents split mirrors visibility_runs/
-- visibility_answers: one fetch of robots.txt, many per-agent verdicts
-- against that same fetch -- an agent's outcome is never re-fetched
-- individually, robots.txt itself is agent-agnostic, only its RULES are
-- agent-specific (real precedence logic lives in ingest/robots.py, not
-- SQL -- brief explicitly warns against a naive substring check here,
-- "a wrong finding here destroys trust").
--
-- page_spec_visibility.value_found_as_text/schema_org_product_present are
-- nullable tri-state, not boolean-with-a-default: null means the fetch
-- itself failed (see `error`) so nothing could be determined, true/false
-- means it succeeded and got a real answer either way -- same "three
-- states, not two" rule CLAUDE.md requires everywhere else in this
-- schema, applied here even though this table isn't `claims` itself.
-- =====================================================================

alter table products add column product_url text;

create table crawlability_checks (
  id              uuid primary key default gen_random_uuid(),
  brand_id        uuid not null references brands (id) on delete cascade,
  website_url     text not null,
  robots_txt_url  text not null,
  http_status     int,
  robots_txt_body text,
  fetched_at      timestamptz not null default now(),
  error           text,
  created_at      timestamptz not null default now()
);

create table crawlability_agents (
  id             uuid primary key default gen_random_uuid(),
  check_id       uuid not null references crawlability_checks (id) on delete cascade,
  agent_name     text not null,
  -- null only when check_id's own fetch errored -- nothing to evaluate.
  allowed        boolean,
  -- The literal "Allow: /x" / "Disallow: /y" line that decided this
  -- outcome, for audit -- null when no rule matched (default-allow).
  matched_rule   text,
  -- Which User-agent: group actually applied (brief's "most-specific
  -- group wins" -- this is the evidence that it did), e.g. "GPTBot" or
  -- "*" when only the wildcard group existed. Null alongside allowed
  -- when the fetch itself errored.
  matched_group  text,
  created_at     timestamptz not null default now(),
  unique (check_id, agent_name)
);

create table page_spec_visibility (
  id                          uuid primary key default gen_random_uuid(),
  product_id                  uuid not null references products (id) on delete cascade,
  claim_id                    uuid not null references claims (id) on delete cascade,
  product_url                 text not null,
  http_status                 int,
  value_found_as_text         boolean,
  schema_org_product_present  boolean,
  fetched_at                  timestamptz not null default now(),
  error                       text,
  created_at                  timestamptz not null default now()
);

create index on crawlability_checks (brand_id);
create index on crawlability_agents (check_id);
create index on page_spec_visibility (product_id);
create index on page_spec_visibility (claim_id);

alter table crawlability_checks   enable row level security;
alter table crawlability_agents   enable row level security;
alter table page_spec_visibility  enable row level security;

-- Same shape as visibility_runs/visibility_answers: org members read
-- their own org's checks, only platform staff/the worker (service_role,
-- bypasses RLS per CLAUDE.md) write.
create policy crawlability_checks_read on crawlability_checks
  for select to authenticated
  using (
    exists (
      select 1 from brands b where b.id = brand_id
        and (public.is_org_member(b.organization_id) or public.is_platform_staff())
    )
  );

create policy crawlability_checks_write on crawlability_checks
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());

create policy crawlability_agents_read on crawlability_agents
  for select to authenticated
  using (
    exists (
      select 1 from crawlability_checks cc
        join brands b on b.id = cc.brand_id
       where cc.id = check_id
         and (public.is_org_member(b.organization_id) or public.is_platform_staff())
    )
  );

create policy crawlability_agents_write on crawlability_agents
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());

create policy page_spec_visibility_read on page_spec_visibility
  for select to authenticated
  using (
    exists (
      select 1 from products p
        join brands b on b.id = p.brand_id
       where p.id = product_id
         and (public.is_org_member(b.organization_id) or public.is_platform_staff())
    )
  );

create policy page_spec_visibility_write on page_spec_visibility
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());
