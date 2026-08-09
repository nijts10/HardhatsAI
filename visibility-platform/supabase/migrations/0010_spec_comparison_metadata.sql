-- =====================================================================
-- AI Visibility Platform — comparison metadata on spec_attributes
--
-- Two columns describing how a spec's values should be compared/matched,
-- not what the values themselves are (that's data_type/unit/enum_values):
--
--   comparison_tolerance — how far apart two numeric values of this spec
--   can be and still count as "the same" (e.g. rounding differences
--   between manufacturers). Null means exact match, not "no tolerance
--   configured yet" -- a spec with no tolerance set is meant to be
--   compared exactly until someone deliberately decides otherwise.
--   Only meaningful for numeric-ish data types; left nullable rather than
--   constrained to those types so setting data_type doesn't have to
--   happen before this can be set, and because "meaningless for this row"
--   and "null" are the same thing here anyway.
--
--   confusable_with — other spec_attributes.key values (or free-text
--   terms) this one is commonly mixed up with, e.g. during extraction or
--   manual review. Plain text[], not FK-validated against spec_attributes
--   -- deliberately loose since a "confusable with" note is just as
--   useful pointing at a term that ISN'T a real key (a raw phrase from a
--   document that looks like it belongs to this spec but doesn't) as
--   pointing at one that is.
-- =====================================================================

alter table spec_attributes
  add column comparison_tolerance numeric,
  add column confusable_with text[] not null default '{}';

alter table spec_attributes
  add constraint spec_attributes_comparison_tolerance_ck
  check (comparison_tolerance is null or comparison_tolerance >= 0);
