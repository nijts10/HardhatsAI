-- =====================================================================
-- AI Visibility Platform — close the 'derived' claim verification gap
--
-- claims_provenance_ck previously only required a citation when
-- presence = 'stated'. A 'derived' claim (the AI's own inference from
-- other stated values) could carry ANY source_snippet, or none at all,
-- and still pass the check constraint -- and verify_claim() in
-- verification.py mirrored that gap, skipping the verbatim-match check
-- entirely for 'derived'. In practice this meant a claim like "this
-- product's US noncombustibility rating corresponds to Euroclass A1"
-- could be accepted with zero grounding, as long as it was labeled
-- 'derived' instead of 'stated'.
--
-- Fix: 'derived' now requires the same citation as 'stated' --
-- source_snippet must still be a pure verbatim quote of the underlying
-- facts (checked in verification.py exactly like 'stated'). The model's
-- own reasoning for the derivation goes in the new derivation_note
-- column instead, which is explicitly NOT verified verbatim since it's
-- the model's own words, not a quote.
-- =====================================================================

alter table claims add column derivation_note text;

alter table claims add constraint claims_derivation_note_ck check (
  derivation_note is null or presence = 'derived'
);

alter table claims drop constraint claims_provenance_ck;

alter table claims add constraint claims_provenance_ck check (
  presence not in ('stated', 'derived')
  or (document_version_id is not null and source_snippet is not null and page_number is not null)
);
