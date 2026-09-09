-- =====================================================================
-- Prompt taxonomy redesign (docs/prompt-taxonomy-proposal.md, proposed
-- 2026-08-14, applied 2026-09-09 after concrete evidence justified it):
-- the flat 7-intent taxonomy scored every prompt the same way, but
-- "is the brand mentioned" is a fundamentally different success
-- criterion for a prompt that never asks for a brand (a pure regulation
-- question) than for one that explicitly asks for a product
-- recommendation. Confirmed for real on the Hunter Douglas Gemini run:
-- 7 of 8 'compliance' prompts got ZERO brand mentions from ANY brand
-- (not just Hunter Douglas) -- proving presence_rate is close to
-- meaningless for that category, not a real visibility failure.
--
-- Two axes: A (brand recall -- does the brand get volunteered at all,
-- meaningful only where a brand answer is actually plausible) and B
-- (product correctness -- does the right product/spec surface, judged
-- against ground truth, recognized by sub-brand name too).
--
-- Postgres enums can't remove a value, only add -- the old 7 values stay
-- defined but unused going forward (additive, same precedent as
-- document_kind's 'webpage' addition in 0021).
-- =====================================================================

alter type prompt_intent add value 'a1_functie_gedreven';
alter type prompt_intent add value 'a2_leverancier_categorie';
alter type prompt_intent add value 'a3_concurrent_vergelijking';
alter type prompt_intent add value 'b1_eis_gedreven';
alter type prompt_intent add value 'b2_norm_regelgeving';
alter type prompt_intent add value 'b3_duurzaamheidseis';
alter type prompt_intent add value 'b4_probleem_gedreven';
alter type prompt_intent add value 'c1_merk_specifiek';
