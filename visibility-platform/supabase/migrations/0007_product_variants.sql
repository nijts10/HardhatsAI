-- =====================================================================
-- AI Visibility Platform — product variants
--
-- A single product can have genuinely different performance values at
-- different thicknesses/SKUs (e.g. sound reduction differs by
-- thickness). claims previously allowed only one current value per
-- (product, spec_attribute) -- no way to store "at 40mm: Rw 45dB, at
-- 80mm: Rw 52dB" against one product row. product_variants adds that
-- layer: a variant belongs to exactly one product, and a claim can now
-- optionally target a specific variant instead of the product as a
-- whole. Specs that don't differ by variant stay at the product level
-- (variant_id null) -- only the ones that genuinely differ move down.
-- =====================================================================

create table product_variants (
  id               uuid primary key default gen_random_uuid(),
  product_id       uuid not null references products (id) on delete cascade,
  label            text not null,             -- e.g. "40mm", "RW-211-40"
  manufacturer_ref text,
  slug             text not null,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  -- unique per product, not globally -- "40mm" is a near-certain label
  -- collision across different products/brands, unlike products.slug
  -- which folds in the product name too.
  unique (product_id, slug)
);

create index on product_variants (product_id);

create trigger t_product_variants_updated before update on product_variants
  for each row execute function set_updated_at();

alter table claims
  add column variant_id uuid references product_variants (id) on delete cascade;

create index on claims (variant_id);

-- variant_id, if set, must belong to the same product the claim is
-- against. Application code (persistence.py) always derives product_id
-- from the variant it's writing to, so this never fires in practice --
-- it's here so a hand-written insert/update can't silently desync the
-- two, since nothing else in the schema ties them together.
create or replace function public.check_claim_variant_matches_product()
returns trigger
language plpgsql
as $$
declare
  variant_product_id uuid;
begin
  if new.variant_id is not null then
    select product_id into variant_product_id from product_variants where id = new.variant_id;
    if variant_product_id is null then
      raise exception 'claims.variant_id % does not exist', new.variant_id;
    end if;
    if variant_product_id <> new.product_id then
      raise exception 'claims.variant_id % belongs to a different product than claims.product_id %',
        new.variant_id, new.product_id;
    end if;
  end if;
  return new;
end;
$$;

create trigger t_claims_variant_matches_product
  before insert or update of variant_id, product_id on claims
  for each row execute function public.check_claim_variant_matches_product();

-- Replace the old product-only uniqueness with two partial indexes: at
-- most one current claim per (product, spec) when there's no variant,
-- and at most one current claim per (variant, spec) when there is one.
-- A single (product_id, variant_id, spec_attribute_id) unique index
-- would NOT work here -- Postgres treats every NULL as distinct from
-- every other NULL, so it would silently allow unlimited product-level
-- (variant_id is null) current claims for the same spec instead of
-- enforcing "at most one".
drop index claims_current_uniq;

create unique index claims_current_product_uniq on claims (product_id, spec_attribute_id)
  where is_current and variant_id is null;
create unique index claims_current_variant_uniq on claims (variant_id, spec_attribute_id)
  where is_current and variant_id is not null;
