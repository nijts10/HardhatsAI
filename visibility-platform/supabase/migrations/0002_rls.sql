-- =====================================================================
-- AI Visibility Platform — Row Level Security
--
-- Roles:
--   client (org member): sees only their own organization's data, via
--     memberships. 'owner'/'editor' can write; 'viewer' is read-only.
--   analyst / admin (platform staff): cross-org access for review,
--     document intake QA, and report authoring — not scoped to any one
--     organization's membership.
--
-- The ingestion/report pipeline connects with the service_role key and
-- bypasses RLS entirely, same convention as the HardhatsAI project.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Helper predicates
-- ---------------------------------------------------------------------

create or replace function public.is_platform_staff()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.profiles
     where id = auth.uid() and platform_role in ('analyst', 'admin')
  );
$$;

create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.profiles
     where id = auth.uid() and platform_role = 'admin'
  );
$$;

create or replace function public.is_org_member(p_organization_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.memberships
     where organization_id = p_organization_id and profile_id = auth.uid()
  );
$$;

create or replace function public.can_edit_org(p_organization_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select public.is_platform_staff()
      or exists (
           select 1 from public.memberships
            where organization_id = p_organization_id
              and profile_id = auth.uid()
              and role in ('owner', 'editor')
         );
$$;

create or replace function public.org_of_brand(p_brand_id uuid)
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select organization_id from public.brands where id = p_brand_id;
$$;

create or replace function public.org_of_document(p_document_id uuid)
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select organization_id from public.documents where id = p_document_id;
$$;

create or replace function public.org_of_report(p_report_id uuid)
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select organization_id from public.reports where id = p_report_id;
$$;

-- ---------------------------------------------------------------------
-- Enable RLS everywhere. Tables with no policy are readable/writable
-- only by service_role.
-- ---------------------------------------------------------------------

alter table organizations       enable row level security;
alter table profiles            enable row level security;
alter table memberships         enable row level security;
alter table product_categories  enable row level security;
alter table brands              enable row level security;
alter table products            enable row level security;
alter table documents           enable row level security;
alter table document_versions   enable row level security;
alter table prompt_library      enable row level security;
alter table reports             enable row level security;
alter table report_sections     enable row level security;
alter table audit_log           enable row level security;

-- ---------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------

create policy organizations_read on organizations
  for select to authenticated
  using (public.is_org_member(id) or public.is_platform_staff());

-- Org creation is analyst-driven in Phase 1 (outreach -> audit -> client
-- onboarding), not client self-signup.
create policy organizations_write on organizations
  for all to authenticated
  using (public.is_admin() or public.is_platform_staff())
  with check (public.is_admin() or public.is_platform_staff());

create policy profiles_read on profiles
  for select to authenticated
  using (id = auth.uid() or public.is_platform_staff());

create policy profiles_update_self on profiles
  for update to authenticated
  using (id = auth.uid() or public.is_admin())
  with check (id = auth.uid() or public.is_admin());

create policy memberships_read on memberships
  for select to authenticated
  using (public.is_org_member(organization_id) or public.is_platform_staff());

create policy memberships_write on memberships
  for all to authenticated
  using (public.can_edit_org(organization_id))
  with check (public.can_edit_org(organization_id));

-- ---------------------------------------------------------------------
-- Reference data: readable by everyone signed in, writable by staff
-- ---------------------------------------------------------------------

create policy product_categories_read on product_categories
  for select to authenticated using (true);
create policy product_categories_write on product_categories
  for all to authenticated using (public.is_platform_staff()) with check (public.is_platform_staff());

create policy prompt_library_read on prompt_library
  for select to authenticated using (true);
create policy prompt_library_write on prompt_library
  for all to authenticated using (public.is_platform_staff()) with check (public.is_platform_staff());

-- ---------------------------------------------------------------------
-- Commercial objects
-- ---------------------------------------------------------------------

create policy brands_read on brands
  for select to authenticated
  using (public.is_org_member(organization_id) or public.is_platform_staff());

create policy brands_write on brands
  for all to authenticated
  using (public.can_edit_org(organization_id))
  with check (public.can_edit_org(organization_id));

create policy products_read on products
  for select to authenticated
  using (public.is_org_member(public.org_of_brand(brand_id)) or public.is_platform_staff());

create policy products_write on products
  for all to authenticated
  using (public.can_edit_org(public.org_of_brand(brand_id)))
  with check (public.can_edit_org(public.org_of_brand(brand_id)));

-- ---------------------------------------------------------------------
-- Documents
-- ---------------------------------------------------------------------

create policy documents_read on documents
  for select to authenticated
  using (public.is_org_member(organization_id) or public.is_platform_staff());

create policy documents_write on documents
  for all to authenticated
  using (public.can_edit_org(organization_id))
  with check (public.can_edit_org(organization_id));

create policy document_versions_read on document_versions
  for select to authenticated
  using (public.is_org_member(public.org_of_document(document_id)) or public.is_platform_staff());

-- Append-only: insert is allowed for org editors/staff, no update or
-- delete policy at all — the table is immutable by anyone except
-- service_role, matching the "never edit a document in place" invariant.
create policy document_versions_insert on document_versions
  for insert to authenticated
  with check (public.can_edit_org(public.org_of_document(document_id)));

-- ---------------------------------------------------------------------
-- Reports — clients read, staff author (Phase 1: analysts build the
-- audit report; self-serve report creation is a later phase)
-- ---------------------------------------------------------------------

create policy reports_read on reports
  for select to authenticated
  using (public.is_org_member(organization_id) or public.is_platform_staff());

create policy reports_write on reports
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());

create policy report_sections_read on report_sections
  for select to authenticated
  using (public.is_org_member(public.org_of_report(report_id)) or public.is_platform_staff());

create policy report_sections_write on report_sections
  for all to authenticated
  using (public.is_platform_staff())
  with check (public.is_platform_staff());

-- ---------------------------------------------------------------------
-- Operations: staff-only visibility
-- ---------------------------------------------------------------------

create policy audit_log_read on audit_log
  for select to authenticated using (public.is_platform_staff());
