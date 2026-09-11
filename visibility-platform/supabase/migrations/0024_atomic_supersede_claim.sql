-- =====================================================================
-- Make supersede_claim() also promote the new claim to current, in the
-- SAME statement -- closes a real, confirmed data-loss window.
--
-- Investigated 2026-09-10 after a review of the Hunter Douglas Gemini
-- report flagged "HeartFelt EPD beschikbaar" as a documentation gap even
-- though HD's own EPD exists. Root cause traced via direct DB queries to
-- something unrelated to that specific document: 90 (product,
-- spec_attribute[, variant]) pairs across the Hunter Douglas brand have
-- claim history but NO row with is_current = true. Every one of the 90
-- has the exact same shape: the most recent claim for that pair has
-- presence = 'stated', came from a `kind = 'webpage'` document, has
-- superseded_by IS NULL (nothing superseded it), and yet is_current =
-- false. That is exactly the state left behind if the OLD claim's
-- update (is_current=false, superseded_by=<new>) commits but the NEW
-- claim's is_current=true update never does -- previously two separate
-- round trips from persistence.py's _persist_claims (supersede_claim(),
-- then a second set_claim_current() call). This DB's own worker code
-- documents a real, load-bearing reason a round trip can go missing
-- mid-run against the Supabase pooler (cli.py's _recover_connection
-- docstring: "a run long enough to make dozens of Claude calls
-- interspersed with commits can outlive the Supabase pooler's own
-- connection lifetime ... conn.rollback() itself then raises"); the
-- orphaned pairs are spread across ~11 different extraction_run_ids
-- from the one long live-website crawl (2026-09-08), 7-26 per run,
-- consistent with this being a recurring risk across a long run rather
-- than a one-off fluke. The exact failure point between the two round
-- trips was not reproduced live -- this migration does not depend on
-- knowing it. Folding both updates into one function call removes the
-- round-trip boundary entirely, so this failure mode becomes structurally
-- impossible regardless of what was causing it.
--
-- persistence.py's _persist_claims no longer needs its own
-- set_claim_current() call after supersede_claim() -- removed there in
-- the same change, not left as a harmless-but-redundant second call.
-- =====================================================================

-- plpgsql with two SEQUENTIAL statements, not `language sql` with both
-- updates folded into one WITH-query -- tried that first and it failed
-- the existing test suite with a live claims_current_product_uniq
-- violation (a duplicate-key error where the "duplicate" was the old and
-- new row sharing the same product_id/spec_attribute_id, which the
-- CTE version is supposed to resolve). Postgres does not guarantee the
-- CTE's write settles against the partial unique index before the main
-- query's write is checked when the two aren't data-dependent (the main
-- query here never reads old_row's output) -- both writes land in the
-- same statement, so the constraint can see both "is_current = true"
-- rows simultaneously. Two plain sequential UPDATEs inside one plpgsql
-- function keeps the single-round-trip property this migration exists
-- for (see the header comment) while giving Postgres an actual
-- statement boundary between "old goes false" and "new goes true".
create or replace function public.supersede_claim(
  p_old_claim_id uuid,
  p_new_claim_id uuid
)
returns void
language plpgsql
set search_path = public
as $$
begin
  update claims
     set is_current    = false,
         superseded_by = p_new_claim_id
   where id = p_old_claim_id;

  update claims
     set is_current = true
   where id = p_new_claim_id;
end;
$$;

-- ---------------------------------------------------------------------
-- One-off data repair for the 90 pairs already orphaned by the bug above.
-- Promotes the most recently created claim in each orphaned group to
-- current -- recovers real, already-extracted, already-cited ground
-- truth that was simply invisible to every is_current-filtered query
-- (report generation, scoring, claims-diff) until now. Not destructive:
-- every promoted row already existed with its own document_version_id/
-- page_number/source_snippet: this only flips is_current, it manufactures
-- no new values. Idempotent -- running it again finds nothing left to
-- repair since claims_current_product_uniq/claims_current_variant_uniq
-- will already hold.
-- ---------------------------------------------------------------------

with orphaned_groups as (
  select product_id, spec_attribute_id, variant_id
    from claims
   group by product_id, spec_attribute_id, variant_id
  having bool_or(is_current) = false
),
latest_per_group as (
  select distinct on (c.product_id, c.spec_attribute_id, c.variant_id)
         c.id
    from claims c
    join orphaned_groups g
      on g.product_id = c.product_id
     and g.spec_attribute_id = c.spec_attribute_id
     and g.variant_id is not distinct from c.variant_id
   order by c.product_id, c.spec_attribute_id, c.variant_id, c.created_at desc
)
update claims
   set is_current = true
 where id in (select id from latest_per_group);
