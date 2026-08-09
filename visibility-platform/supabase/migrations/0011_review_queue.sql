-- =====================================================================
-- AI Visibility Platform — review queue for found-but-undefined specs
--
-- Replaces the ad-hoc "[UNMAPPED: ...]" convention that used to get
-- stuffed into application_area claims (see extraction.py history): a
-- fact the extractor finds that doesn't map to any spec_attributes key
-- gets its own row here instead, with its own real citation. These rows
-- are never claims -- they live in their own table, so they structurally
-- can never be counted by report.py's coverage query or any other
-- measurement, not just by convention.
--
-- A human resolves each row via the `review` CLI: promote (a matching
-- spec_attributes.key now exists -- an analyst added it to seed.sql,
-- same controlled-vocabulary-growth rule as everywhere else in this
-- project) or reject (not worth a dedicated spec, or not a real fact).
-- =====================================================================

create type review_queue_status as enum ('new', 'promoted', 'rejected');

create table review_queue (
  id                   uuid primary key default gen_random_uuid(),
  found_term           text not null,
  source_snippet       text not null,
  document_version_id  uuid not null references document_versions (id) on delete cascade,
  page_number          int not null,
  status               review_queue_status not null default 'new',
  -- Not in the original field list, but "promoted" is meaningless without
  -- recording promoted to WHAT -- `review promote <id> --key <spec_key>`
  -- needs somewhere to put spec_key, and this is the natural place.
  promoted_to_key      text references spec_attributes (key),
  created_at           timestamptz not null default now(),

  constraint review_queue_promoted_key_ck check (
    (status = 'promoted') = (promoted_to_key is not null)
  )
);

create index on review_queue (document_version_id);
create index on review_queue (status);

alter table review_queue enable row level security;

-- Same shape as claims/certifications: org members read their own org's
-- findings, staff read/write everywhere, the extraction worker itself
-- runs as service_role and bypasses RLS.
create policy review_queue_read on review_queue
  for select to authenticated
  using (public.is_org_member(public.org_of_document_version(document_version_id))
         or public.is_platform_staff());

create policy review_queue_write on review_queue
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());
