-- =====================================================================
-- HardhatsAI — Row Level Security
--
-- Roles:
--   engineer : reads published products/specs/documents, owns their own
--              projects, queries and selections
--   supplier : full control over their own company's documents and products,
--              no visibility into other suppliers
--   admin    : everything
--
-- The ingestion worker connects with the service_role key and bypasses RLS
-- entirely. Never ship the service_role key to the browser or the Revit
-- plugin — the plugin authenticates as the engineer.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Helper predicates (security definer so they can read membership tables
-- without recursing through the policies that call them)
-- ---------------------------------------------------------------------

create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.profiles
     where id = auth.uid() and role = 'admin'
  );
$$;

create or replace function public.is_supplier_member(p_supplier_company_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.supplier_members
     where supplier_company_id = p_supplier_company_id
       and profile_id = auth.uid()
  );
$$;

create or replace function public.is_project_member(p_project_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.projects
     where id = p_project_id and owner_profile_id = auth.uid()
  ) or exists (
    select 1 from public.project_members
     where project_id = p_project_id and profile_id = auth.uid()
  );
$$;

create or replace function public.can_read_document(p_document_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select public.is_admin()
      or exists (
           select 1 from public.documents d
            where d.id = p_document_id
              and d.supplier_company_id is not null
              and public.is_supplier_member(d.supplier_company_id)
         )
      -- Engineers may open any document backing a published product.
      or exists (
           select 1
             from public.product_documents pd
             join public.products p on p.id = pd.product_id
            where pd.document_id = p_document_id
              and p.status = 'published'
         );
$$;

create or replace function public.can_read_product(p_product_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.products p
     where p.id = p_product_id
       and (
         p.status = 'published'
         or public.is_admin()
         or public.is_supplier_member(p.supplier_company_id)
       )
  );
$$;

-- ---------------------------------------------------------------------
-- Enable RLS everywhere. Tables with no policy are readable/writable only
-- by service_role, which is the intended default for pipeline internals.
-- ---------------------------------------------------------------------

alter table profiles             enable row level security;
alter table supplier_companies   enable row level security;
alter table supplier_members     enable row level security;
alter table product_categories   enable row level security;
alter table spec_definitions     enable row level security;
alter table documents            enable row level security;
alter table document_pages       enable row level security;
alter table document_chunks      enable row level security;
alter table products             enable row level security;
alter table product_documents    enable row level security;
alter table product_specs        enable row level security;
alter table product_certificates enable row level security;
alter table extraction_runs      enable row level security;
alter table projects             enable row level security;
alter table project_members      enable row level security;
alter table revit_elements       enable row level security;
alter table queries              enable row level security;
alter table query_results        enable row level security;
alter table selection_events     enable row level security;
alter table audit_log            enable row level security;
alter table jobs                 enable row level security;

-- ---------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------

create policy profiles_select_self on profiles
  for select to authenticated
  using (id = auth.uid() or public.is_admin());

create policy profiles_update_self on profiles
  for update to authenticated
  using (id = auth.uid() or public.is_admin())
  with check (id = auth.uid() or public.is_admin());

-- Supplier identity is public to signed-in users: engineers must see who
-- makes a product.
create policy supplier_companies_read on supplier_companies
  for select to authenticated using (true);

create policy supplier_companies_write on supplier_companies
  for update to authenticated
  using (public.is_supplier_member(id) or public.is_admin())
  with check (public.is_supplier_member(id) or public.is_admin());

create policy supplier_companies_insert on supplier_companies
  for insert to authenticated
  with check (public.is_admin());

create policy supplier_members_read on supplier_members
  for select to authenticated
  using (profile_id = auth.uid() or public.is_supplier_member(supplier_company_id) or public.is_admin());

create policy supplier_members_write on supplier_members
  for all to authenticated
  using (public.is_admin() or exists (
    select 1 from supplier_members m
     where m.supplier_company_id = supplier_members.supplier_company_id
       and m.profile_id = auth.uid() and m.is_owner
  ))
  with check (public.is_admin() or exists (
    select 1 from supplier_members m
     where m.supplier_company_id = supplier_members.supplier_company_id
       and m.profile_id = auth.uid() and m.is_owner
  ));

-- ---------------------------------------------------------------------
-- Reference data: readable by everyone signed in, writable by admins
-- ---------------------------------------------------------------------

create policy product_categories_read on product_categories
  for select to authenticated using (true);
create policy product_categories_write on product_categories
  for all to authenticated using (public.is_admin()) with check (public.is_admin());

create policy spec_definitions_read on spec_definitions
  for select to authenticated using (true);
create policy spec_definitions_write on spec_definitions
  for all to authenticated using (public.is_admin()) with check (public.is_admin());

-- ---------------------------------------------------------------------
-- Documents
-- ---------------------------------------------------------------------

create policy documents_read on documents
  for select to authenticated using (public.can_read_document(id));

create policy documents_insert on documents
  for insert to authenticated
  with check (
    public.is_admin()
    or (supplier_company_id is not null and public.is_supplier_member(supplier_company_id))
  );

create policy documents_update on documents
  for update to authenticated
  using (public.is_admin() or public.is_supplier_member(supplier_company_id))
  with check (public.is_admin() or public.is_supplier_member(supplier_company_id));

create policy documents_delete on documents
  for delete to authenticated
  using (public.is_admin() or public.is_supplier_member(supplier_company_id));

create policy document_pages_read on document_pages
  for select to authenticated using (public.can_read_document(document_id));

create policy document_chunks_read on document_chunks
  for select to authenticated using (public.can_read_document(document_id));

create policy extraction_runs_read on extraction_runs
  for select to authenticated using (public.can_read_document(document_id));

-- ---------------------------------------------------------------------
-- Products
-- ---------------------------------------------------------------------

create policy products_read on products
  for select to authenticated
  using (status = 'published' or public.is_admin() or public.is_supplier_member(supplier_company_id));

create policy products_write on products
  for all to authenticated
  using (public.is_admin() or public.is_supplier_member(supplier_company_id))
  with check (public.is_admin() or public.is_supplier_member(supplier_company_id));

create policy product_documents_read on product_documents
  for select to authenticated using (public.can_read_product(product_id));

create policy product_specs_read on product_specs
  for select to authenticated using (public.can_read_product(product_id));

create policy product_certificates_read on product_certificates
  for select to authenticated using (public.can_read_product(product_id));

-- Spec writes go through the pipeline (service_role) or an admin override.
-- Suppliers correct data by re-uploading a document, which keeps provenance
-- intact. Do not add a supplier UPDATE policy here without solving that.
create policy product_specs_admin_write on product_specs
  for all to authenticated using (public.is_admin()) with check (public.is_admin());

-- ---------------------------------------------------------------------
-- Projects & Revit
-- ---------------------------------------------------------------------

create policy projects_read on projects
  for select to authenticated using (public.is_project_member(id) or public.is_admin());

create policy projects_insert on projects
  for insert to authenticated with check (owner_profile_id = auth.uid());

create policy projects_update on projects
  for update to authenticated
  using (owner_profile_id = auth.uid() or public.is_admin())
  with check (owner_profile_id = auth.uid() or public.is_admin());

create policy projects_delete on projects
  for delete to authenticated using (owner_profile_id = auth.uid() or public.is_admin());

create policy project_members_read on project_members
  for select to authenticated using (public.is_project_member(project_id) or public.is_admin());

create policy project_members_write on project_members
  for all to authenticated
  using (exists (select 1 from projects p where p.id = project_id and p.owner_profile_id = auth.uid())
         or public.is_admin())
  with check (exists (select 1 from projects p where p.id = project_id and p.owner_profile_id = auth.uid())
              or public.is_admin());

create policy revit_elements_rw on revit_elements
  for all to authenticated
  using (public.is_project_member(project_id) or public.is_admin())
  with check (public.is_project_member(project_id) or public.is_admin());

-- ---------------------------------------------------------------------
-- Queries & feedback
-- ---------------------------------------------------------------------

create policy queries_read on queries
  for select to authenticated
  using (profile_id = auth.uid()
         or (project_id is not null and public.is_project_member(project_id))
         or public.is_admin());

create policy queries_insert on queries
  for insert to authenticated with check (profile_id = auth.uid());

create policy query_results_read on query_results
  for select to authenticated
  using (exists (
    select 1 from queries q
     where q.id = query_id
       and (q.profile_id = auth.uid()
            or (q.project_id is not null and public.is_project_member(q.project_id)))
  ) or public.is_admin());

create policy selection_events_read on selection_events
  for select to authenticated
  using (profile_id = auth.uid()
         or (project_id is not null and public.is_project_member(project_id))
         or public.is_admin());

create policy selection_events_insert on selection_events
  for insert to authenticated with check (profile_id = auth.uid());

-- ---------------------------------------------------------------------
-- Operational tables: admin read, service_role write
-- ---------------------------------------------------------------------

create policy audit_log_read on audit_log
  for select to authenticated using (public.is_admin());

create policy jobs_read on jobs
  for select to authenticated using (public.is_admin());

-- ---------------------------------------------------------------------
-- Function grants
-- ---------------------------------------------------------------------

grant execute on function public.search_products(vector, text, uuid, uuid, jsonb, boolean, int, int) to authenticated;
grant execute on function public.match_chunks(vector, uuid[], int) to authenticated;
grant execute on function public.spec_requirement_status(uuid, jsonb) to authenticated;
revoke execute on function public.claim_job(text, text[]) from authenticated, anon;
