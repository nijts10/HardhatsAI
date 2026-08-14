-- =====================================================================
-- product_documents — many-to-many between documents and products.
--
-- Why: documents.product_id is a single FK, but a real Hunter Douglas
-- document commonly covers a whole product family, not one SKU (e.g. the
-- Indoor Air Comfort Gold certificate explicitly lists HeartFelt Baffles,
-- Groove, Islands, Linear, MultiPanel and Origami as "products included").
-- Found while backfilling the full HeartFelt line: all 8 documents
-- ingested so far were attached to a single product (HeartFelt Linear
-- PareauLux) even though several are family-wide certificates, leaving
-- the other 18 HeartFelt-line products with zero documents and zero
-- claims. Mirrors HardhatsAI's product_documents (20260728120000_init.sql)
-- for exactly the same reason.
--
-- documents.product_id is kept as-is (the "primary" product a document
-- was uploaded against) — this table is the additional, explicit
-- association used for search/citation across every product a document
-- actually covers. It does not by itself create claims for the newly
-- associated products; claims still require a per-product extraction
-- (or backfill) pass, since claims.product_id is required and singular
-- (claims_current_uniq is one row per (product_id, spec_attribute_id)).
-- =====================================================================

create table product_documents (
  product_id  uuid not null references products (id) on delete cascade,
  document_id uuid not null references documents (id) on delete cascade,
  relation    document_kind not null default 'datasheet',
  created_at  timestamptz not null default now(),
  primary key (product_id, document_id)
);

create index on product_documents (document_id);

alter table product_documents enable row level security;

create policy product_documents_read on product_documents
  for select to authenticated
  using (public.is_org_member(public.org_of_document(document_id)) or public.is_platform_staff());

create policy product_documents_write on product_documents
  for all to authenticated
  using (public.can_edit_org(public.org_of_document(document_id)))
  with check (public.can_edit_org(public.org_of_document(document_id)));

-- Backfill: every document's existing primary product_id is also a
-- product_documents row, so nothing that already worked off
-- documents.product_id regresses.
insert into product_documents (product_id, document_id)
select product_id, id from documents where product_id is not null
on conflict do nothing;
