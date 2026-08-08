-- =====================================================================
-- AI Visibility Platform — category hierarchy
--
-- product_categories becomes a tree (parent_id), so a spec_attribute
-- tagged at a broad category (e.g. "mineral wool") is inherited by every
-- descendant (e.g. "rock wool", a specific product line under it)
-- instead of needing to be re-listed on every leaf category_code.
-- category_and_ancestor_codes() does the tree walk once, in SQL, so
-- application code (extraction scoping in pipeline.py, coverage in
-- report.py) just asks "which codes apply to this category" without
-- hand-rolling a recursive query in two places.
-- =====================================================================

alter table product_categories
  add column parent_id uuid references product_categories (id) on delete set null;

create index on product_categories (parent_id);

-- A plain FK can't express "not an ancestor of itself" -- walk the chain
-- on write instead of discovering a cycle later via infinite recursion
-- in category_and_ancestor_codes().
create or replace function public.prevent_category_cycle()
returns trigger
language plpgsql
as $$
declare
  current_id uuid := new.parent_id;
  hops int := 0;
begin
  while current_id is not null loop
    if current_id = new.id then
      raise exception 'product_categories: parent_id would create a cycle';
    end if;
    hops := hops + 1;
    if hops > 100 then
      raise exception 'product_categories: parent chain too deep (possible cycle)';
    end if;
    select parent_id into current_id from product_categories where id = current_id;
  end loop;
  return new;
end;
$$;

create trigger t_product_categories_no_cycle
  before insert or update of parent_id on product_categories
  for each row execute function public.prevent_category_cycle();

-- Every code from this category up through its ancestors (closest first).
-- Returns '{}' for a null category_id, which correctly leaves only
-- universal (category_codes = '{}') spec_attributes applicable -- a
-- product with no category assigned yet is "not yet evaluated" for
-- category-scoped specs, not "none apply".
create or replace function public.category_and_ancestor_codes(p_category_id uuid)
returns text[]
language sql
stable
set search_path = public
as $$
  with recursive ancestors as (
    select id, code, parent_id, 0 as depth
      from product_categories
     where id = p_category_id
    union all
    select pc.id, pc.code, pc.parent_id, a.depth + 1
      from product_categories pc
      join ancestors a on pc.id = a.parent_id
  )
  select coalesce(array_agg(code order by depth), '{}')
    from ancestors;
$$;
