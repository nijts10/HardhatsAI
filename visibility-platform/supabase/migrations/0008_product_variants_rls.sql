-- =====================================================================
-- AI Visibility Platform — RLS for product_variants
--
-- Same shape as products' own policies (0002_rls.sql), reusing
-- org_of_product() from 0004_extraction_rls.sql since a variant's
-- organization is its parent product's.
-- =====================================================================

alter table product_variants enable row level security;

create policy product_variants_read on product_variants
  for select to authenticated
  using (public.is_org_member(public.org_of_product(product_id)) or public.is_platform_staff());

create policy product_variants_write on product_variants
  for all to authenticated
  using (public.can_edit_org(public.org_of_product(product_id)))
  with check (public.can_edit_org(public.org_of_product(product_id)));
