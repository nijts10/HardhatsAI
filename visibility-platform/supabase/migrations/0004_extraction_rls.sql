-- =====================================================================
-- AI Visibility Platform — RLS for the Phase 2 extraction tables
--
-- Same shape as 0002_rls.sql: org members read their own org's data,
-- platform staff read/write across every org. The extraction worker
-- itself runs as service_role and bypasses RLS entirely, same
-- convention as the HardhatsAI ingestion worker.
-- =====================================================================

create or replace function public.org_of_document_version(p_document_version_id uuid)
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select d.organization_id
    from public.document_versions dv
    join public.documents d on d.id = dv.document_id
   where dv.id = p_document_version_id;
$$;

create or replace function public.org_of_product(p_product_id uuid)
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select b.organization_id
    from public.products p
    join public.brands b on b.id = p.brand_id
   where p.id = p_product_id;
$$;

alter table document_pages    enable row level security;
alter table document_chunks   enable row level security;
alter table extraction_runs   enable row level security;
alter table spec_attributes   enable row level security;
alter table claims            enable row level security;
alter table certifications    enable row level security;

-- Parsed content: read-only for org members/staff; written only by the
-- extraction worker (service_role), never by authenticated.
create policy document_pages_read on document_pages
  for select to authenticated
  using (public.is_org_member(public.org_of_document_version(document_version_id))
         or public.is_platform_staff());

create policy document_chunks_read on document_chunks
  for select to authenticated
  using (public.is_org_member(public.org_of_document_version(document_version_id))
         or public.is_platform_staff());

create policy extraction_runs_read on extraction_runs
  for select to authenticated
  using (public.is_org_member(public.org_of_document_version(document_version_id))
         or public.is_platform_staff());

-- Global controlled vocabulary — same pattern as product_categories/prompt_library.
create policy spec_attributes_read on spec_attributes
  for select to authenticated using (true);
create policy spec_attributes_write on spec_attributes
  for all to authenticated using (public.is_platform_staff()) with check (public.is_platform_staff());

-- Claims: clients read their own org's extracted facts; only staff write
-- (correcting a claim happens via analyst review or a document
-- re-upload + re-extract, not direct client editing — same principle as
-- HardhatsAI's product_specs_admin_write).
create policy claims_read on claims
  for select to authenticated
  using (public.is_org_member(public.org_of_product(product_id)) or public.is_platform_staff());

create policy claims_write on claims
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());

create policy certifications_read on certifications
  for select to authenticated
  using (public.is_org_member(public.org_of_product(product_id)) or public.is_platform_staff());

create policy certifications_write on certifications
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());
