-- =====================================================================
-- product_lines — the missing layer in brand -> line -> product ->
-- product type -> spec (+ data) hierarchy.
--
-- Why: products.name has been carrying two concepts as one string all
-- along -- the sub-brand/line ("HeartFelt", "PareauLux", "Luxalon") and
-- the specific product within it ("Ceiling System", "30BD Aluminum").
-- Nothing in the schema ever grouped "these products belong to the same
-- real line" -- which is a direct cause of the repeated name-variant
-- fragmentation this project keeps hitting (2026-08-14, 08-19, 09-07:
-- "HeartFelt Ceiling" vs "HeartFelt Ceiling System" treated as two
-- unrelated products each time, because there was no structural anchor
-- linking them beyond an exact name-slug match). This does not solve
-- fuzzy name matching by itself, but it makes the line an explicit,
-- queryable fact instead of an implicit prefix convention, and gives
-- persistence.py something real to scope a same-line duplicate warning
-- against (see the pg_trgm check added alongside this migration).
--
-- Nullable on products: not every product belongs to a named sub-line
-- (e.g. a generic scoping product, or a document that never names one) --
-- absence stays "not yet known", never a guessed value.
-- =====================================================================

create table product_lines (
  id         uuid primary key default gen_random_uuid(),
  brand_id   uuid not null references brands (id) on delete cascade,
  name       text not null,
  slug       text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- scoped to brand, not global -- two different brands can each have a
  -- line called e.g. "Linear" without colliding.
  unique (brand_id, slug)
);

create index on product_lines (brand_id);

create trigger t_product_lines_updated before update on product_lines
  for each row execute function set_updated_at();

alter table products
  add column product_line_id uuid references product_lines (id) on delete set null;

create index on products (product_line_id);
