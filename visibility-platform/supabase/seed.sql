-- =====================================================================
-- AI Visibility Platform — starter reference data
--
-- Per the brief: "Start with one strong category where specs matter."
-- These four cover its own suggested list; narrow further once a first
-- real client is onboarded.
-- =====================================================================

insert into product_categories (code, name_nl, name_en) values
  ('brandwerende_systeemwanden', 'Brandwerende systeemwanden', 'Fire-rated system walls'),
  ('akoestische_systeemwanden',  'Akoestische systeemwanden',  'Acoustic system walls'),
  ('technische_binnenwanden',    'Technische binnenwanden',    'Technical interior walls'),
  ('glaswanden',                 'Glaswanden',                 'Glass walls')
on conflict (code) do nothing;

-- ---------------------------------------------------------------------
-- Prompt library — one prompt per use case from the brief's own example
-- list. use_case is the stable slug; prompt_text is what actually gets
-- fed to an AI assistant during a benchmark run (Phase 2).
-- ---------------------------------------------------------------------

insert into prompt_library (use_case, prompt_text, category_id, language) values
  ('fire_rated_office_partition',
   'Welke brandwerende systeemwand kan ik gebruiken voor een kantoorscheiding met EI60?',
   (select id from product_categories where code = 'brandwerende_systeemwanden'), 'nl'),

  ('acoustic_glass_wall',
   'Welke akoestische glaswand heeft de beste geluidsisolatie voor vergaderruimtes?',
   (select id from product_categories where code = 'glaswanden'), 'nl'),

  ('demountable_partition',
   'Wat zijn de beste demontabele scheidingswanden voor een flexibele kantoorinrichting?',
   (select id from product_categories where code = 'technische_binnenwanden'), 'nl'),

  ('four_meter_high_system_wall',
   'Welke systeemwanden zijn geschikt tot een hoogte van 4 meter?',
   (select id from product_categories where code = 'technische_binnenwanden'), 'nl'),

  ('hospital_corridor_wall',
   'Welke wandsystemen worden aanbevolen voor ziekenhuisgangen?',
   (select id from product_categories where code = 'technische_binnenwanden'), 'nl'),

  ('school_classroom_wall',
   'Welke scheidingswanden zijn geschikt voor klaslokalen op scholen?',
   (select id from product_categories where code = 'akoestische_systeemwanden'), 'nl'),

  ('lab_partition',
   'Welke wandsystemen zijn geschikt voor laboratoria?',
   (select id from product_categories where code = 'technische_binnenwanden'), 'nl'),

  ('circular_reusable_wall',
   'Welke circulaire of herbruikbare wandsystemen zijn er beschikbaar?',
   (select id from product_categories where code = 'technische_binnenwanden'), 'nl'),

  ('breeam_friendly_wall',
   'Welke wandsystemen dragen bij aan een BREEAM-certificering?',
   (select id from product_categories where code = 'technische_binnenwanden'), 'nl'),

  ('revit_family_availability',
   'Welke leveranciers van systeemwanden bieden Revit-families aan?',
   (select id from product_categories where code = 'technische_binnenwanden'), 'nl')
on conflict do nothing;
