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
-- Category hierarchy — the first real branch under the parent_id tree
-- from 0006_category_hierarchy.sql (that migration only added the
-- mechanism; this is actual category data). Insulation materials get
-- progressively more specific: isolatiematerialen (root) -> steenwol
-- (material) -> brandwerende_steenwol (fire-safing/firestopping use --
-- matches the first real document ingested this project, Rockwool ROXUL
-- Safe, which has no home in the four wall categories above since it
-- isn't a wall system). Parents are inserted before children so each
-- child's parent_id subquery resolves; on conflict (code) do nothing
-- keeps this idempotent across reseeds, same as the block above.
-- ---------------------------------------------------------------------

insert into product_categories (code, name_nl, name_en) values
  ('isolatiematerialen', 'Isolatiematerialen', 'Insulation materials')
on conflict (code) do nothing;

insert into product_categories (code, name_nl, name_en, parent_id) values
  ('steenwol', 'Steenwol', 'Mineral/rock wool',
   (select id from product_categories where code = 'isolatiematerialen'))
on conflict (code) do nothing;

insert into product_categories (code, name_nl, name_en, parent_id) values
  ('brandwerende_steenwol', 'Brandwerende steenwol (brandstopping)', 'Fire-resistant mineral wool (firestopping)',
   (select id from product_categories where code = 'steenwol'))
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

-- ---------------------------------------------------------------------
-- Spec vocabulary (Phase 2) — enough to make the four wall categories and
-- the prompt library's own use cases (demountable, BREEAM, Revit
-- families) answerable once extraction actually runs against a document.
-- ---------------------------------------------------------------------

insert into spec_attributes
  (key, name_nl, name_en, data_type, unit, enum_values, enum_ordinals, norm_reference, category_codes, description)
values
  ('fire_resistance_ei', 'Brandwerendheid EI', 'Fire resistance EI',
   'numeric', 'min', null, null, 'EN 13501-2',
   '{brandwerende_systeemwanden,akoestische_systeemwanden,technische_binnenwanden,glaswanden}',
   'Integrity + insulation period in minutes.'),

  ('fire_resistance_criterion', 'Brandwerendheidscriterium', 'Fire resistance criterion',
   'enum', null, '{R,E,EW,EI,REI,REI-M}',
   '{"R":1,"E":2,"EW":3,"EI":4,"REI":5,"REI-M":6}',
   'EN 13501-2', '{brandwerende_systeemwanden}', null),

  ('smoke_leakage_class', 'Rookdoorlatendheid', 'Smoke leakage class',
   'enum', null, '{Sa,S200,Sm}', '{"Sa":1,"S200":2,"Sm":3}',
   'EN 13501-2', '{brandwerende_systeemwanden}', null),

  ('reaction_to_fire', 'Brandklasse (materiaal)', 'Reaction to fire (Euroclass)',
   'enum', null, '{A1,A2,B,C,D,E,F}',
   '{"A1":7,"A2":6,"B":5,"C":4,"D":3,"E":2,"F":1}',
   'EN 13501-1', '{}', null),

  ('airborne_sound_reduction_rw', 'Luchtgeluidisolatie Rw', 'Airborne sound reduction Rw',
   'numeric', 'dB', null, null, 'EN ISO 717-1',
   '{akoestische_systeemwanden,glaswanden,technische_binnenwanden}', null),

  ('max_height_mm', 'Maximale wandhoogte', 'Maximum wall height',
   'numeric', 'mm', null, null, null,
   '{technische_binnenwanden,akoestische_systeemwanden,brandwerende_systeemwanden,glaswanden}',
   'Directly answers the "geschikt tot een hoogte van 4 meter" prompt.'),

  ('thickness_mm', 'Dikte', 'Thickness', 'numeric', 'mm', null, null, null, '{}', null),

  ('demountable', 'Demontabel', 'Demountable',
   'boolean', null, null, null, null, '{technische_binnenwanden}',
   'Directly answers the demountable-partition prompt.'),

  ('ce_marked', 'CE-markering', 'CE marked',
   'boolean', null, null, null, 'CPR 305/2011', '{}', null),

  ('breeam_contribution', 'BREEAM-bijdrage', 'BREEAM contribution',
   'text', null, null, null, null, '{}',
   'Free text describing how the product contributes to BREEAM credits. Directly answers the BREEAM prompt.'),

  ('revit_family_available', 'Revit-familie beschikbaar', 'Revit family available',
   'boolean', null, null, null, null, '{}',
   'Directly answers the Revit-family-availability prompt.'),

  ('application_area', 'Toepassingsgebied', 'Application area',
   'text', null, null, null, null, '{}',
   'Free text; covers hospital/school/lab suitability without a dedicated boolean per use case.')
on conflict (key) do nothing;

update spec_attributes set is_filterable = false where key in ('breeam_contribution', 'application_area');

-- ---------------------------------------------------------------------
-- Ceiling spec vocabulary (MVP brief Part 1 / STAP 5). See
-- 0013_ceiling_spec_vocabulary.sql for the full rationale, including why
-- seven of the brief's "reuse existing keys" are actually new here (this
-- schema didn't have them yet) and why 'demountable' is extended instead
-- of duplicated.
-- ---------------------------------------------------------------------

insert into product_categories (code, name_nl, name_en) values
  ('plafondsystemen', 'Plafondsystemen', 'Ceiling systems')
on conflict (code) do nothing;

insert into product_categories (code, name_nl, name_en, parent_id) values
  ('systeemplafond', 'Systeemplafond', 'Suspended ceiling system',
   (select id from product_categories where code = 'plafondsystemen'))
on conflict (code) do nothing;

insert into product_categories (code, name_nl, name_en, parent_id) values
  ('metaal',        'Metaal',       'Metal',
   (select id from product_categories where code = 'systeemplafond')),
  ('minerale_wol',  'Minerale wol', 'Mineral wool',
   (select id from product_categories where code = 'systeemplafond')),
  ('glaswol',       'Glaswol',      'Glass wool',
   (select id from product_categories where code = 'systeemplafond')),
  ('hout',          'Hout',         'Wood',
   (select id from product_categories where code = 'systeemplafond')),
  ('pes_vilt',      'PES-vilt',     'Polyester felt',
   (select id from product_categories where code = 'systeemplafond')),
  ('gips',          'Gips',         'Gypsum',
   (select id from product_categories where code = 'systeemplafond'))
on conflict (code) do nothing;

insert into spec_attributes
  (key, name_nl, name_en, data_type, unit, enum_values, enum_ordinals, norm_reference,
   category_codes, comparison_tolerance, is_filterable, description)
values
  ('sound_absorption_alpha_w', 'Geluidsabsorptie αw', 'Sound absorption αw',
   'numeric', null, null, null, 'EN ISO 11654',
   '{systeemplafond}', 0.025, true,
   '0.00-1.00, declared in 0.05 steps.'),

  ('sound_absorption_class', 'Geluidsabsorptieklasse', 'Sound absorption class',
   'enum', null, '{A,B,C,D,E}', '{"A":5,"B":4,"C":3,"D":2,"E":1}', 'EN ISO 11654',
   '{systeemplafond}', null, true,
   'A best, E worst. Confusable with reaction_to_fire -- both use A-E letters with opposite conventions in some documents.'),

  ('nrc', 'NRC (geluidsabsorptie, US-norm)', 'Noise Reduction Coefficient',
   'numeric', null, null, null, 'ASTM C423',
   '{systeemplafond}', 0.025, true,
   'US standard -- not interchangeable with alpha_w (EN ISO 11654), routinely conflated by LLMs.'),

  ('sound_attenuation_dnfw', 'Geluidreductie tussen ruimtes via plafond Dn,f,w', 'Ceiling sound attenuation Dn,f,w',
   'numeric', 'dB', null, null, 'EN ISO 10848',
   '{systeemplafond}', 1, true,
   'Ceiling-path attenuation between adjacent rooms. Confusable with airborne_sound_reduction_rw, a partition property -- conflating them is the single most common technical error in ceiling specification.'),

  ('perforation_percentage', 'Perforatiegraad', 'Perforation percentage',
   'numeric', '%', null, null, null,
   '{systeemplafond}', 0.5, true, 'Open area.'),

  ('module_width_mm', 'Modulebreedte', 'Module width',
   'numeric', 'mm', null, null, null,
   '{systeemplafond}', null, true, null),

  ('module_length_mm', 'Modulelengte', 'Module length',
   'numeric', 'mm', null, null, null,
   '{systeemplafond}', null, true, null),

  ('light_reflectance', 'Lichtreflectie (LR)', 'Light reflectance',
   'numeric', '%', null, null, 'EN ISO 7724',
   '{systeemplafond}', 1, true, 'LR value.'),

  ('max_relative_humidity', 'Maximale relatieve luchtvochtigheid', 'Max relative humidity',
   'numeric', '%', null, null, 'EN 13964',
   '{systeemplafond}', null, true, 'E.g. 95% for pool/wet areas.'),

  ('edge_type', 'Kanttype', 'Edge type',
   'text', null, null, null, 'EN 13964',
   '{systeemplafond}', null, true, 'A / E / D / SL etc.'),

  ('base_material', 'Basismateriaal', 'Base material',
   'enum', null, '{staal,aluminium,minerale_wol,glaswol,hout,pes_vilt,gips}',
   '{"staal":1,"aluminium":2,"minerale_wol":3,"glaswol":4,"hout":5,"pes_vilt":6,"gips":7}', null,
   '{systeemplafond}', null, true,
   'Not a performance ranking -- ordinals are arbitrary sequence, required by spec_attributes_enum_ck but not meaningful here.'),

  ('corrosion_resistance_class', 'Corrosieweerstandsklasse', 'Corrosion resistance class',
   'text', null, null, null, 'EN 13964',
   '{systeemplafond}', null, true, 'A/B/C/D.'),

  ('cleaning_method', 'Reinigingsmethode', 'Cleaning method',
   'text', null, null, null, null,
   '{systeemplafond}', null, false, null),

  ('suspension_system_type', 'Type ophangsysteem', 'Suspension system type',
   'text', null, null, null, 'EN 13964',
   '{systeemplafond}', null, false, null),

  ('smoke_production_class', 'Rookontwikkelingsklasse', 'Smoke production class',
   'enum', null, '{s1,s2,s3}', '{"s1":3,"s2":2,"s3":1}', 'EN 13501-1',
   '{systeemplafond}', null, true, 's1 best (least smoke) .. s3 worst.'),

  ('flaming_droplets_class', 'Klasse brandend druppelen', 'Flaming droplets class',
   'enum', null, '{d0,d1,d2}', '{"d0":3,"d1":2,"d2":1}', 'EN 13501-1',
   '{systeemplafond}', null, true, 'd0 best (no droplets) .. d2 worst.'),

  ('weight_per_m2', 'Gewicht per m²', 'Weight per m²',
   'numeric', 'kg/m2', null, null, null,
   '{systeemplafond}', 0.5, true, null),

  ('mki_value', 'MKI-waarde', 'MKI (environmental cost indicator) value',
   'numeric', 'EUR/m2', null, null, null,
   '{systeemplafond}', 0.01, true, 'Dutch Nationale Milieudatabase concept -- no fixed EN norm code.'),

  ('gwp_a1a3', 'GWP A1-A3', 'Global Warming Potential, product stage A1-A3',
   'numeric', 'kg CO2-eq/m2', null, null, 'EN 15804',
   '{systeemplafond}', 0.5, true, 'From an EPD.'),

  ('recycled_content_pct', 'Gerecycled materiaal (%)', 'Recycled content',
   'numeric', '%', null, null, null,
   '{systeemplafond}', 1, true, null),

  ('epd_available', 'EPD beschikbaar', 'EPD available',
   'boolean', null, null, null, null,
   '{systeemplafond}', null, true, null)
on conflict (key) do nothing;

update spec_attributes
   set category_codes = array_append(category_codes, 'systeemplafond')
 where key = 'demountable'
   and not ('systeemplafond' = any(category_codes));

update spec_attributes set confusable_with = array['nrc'] where key = 'sound_absorption_alpha_w';
update spec_attributes set confusable_with = array['sound_absorption_alpha_w'] where key = 'nrc';

update spec_attributes set confusable_with = array['airborne_sound_reduction_rw'] where key = 'sound_attenuation_dnfw';
update spec_attributes set confusable_with = array['sound_attenuation_dnfw'] where key = 'airborne_sound_reduction_rw';

update spec_attributes set confusable_with = array['reaction_to_fire'] where key = 'sound_absorption_class';
update spec_attributes set confusable_with = array['sound_absorption_class'] where key = 'reaction_to_fire';
