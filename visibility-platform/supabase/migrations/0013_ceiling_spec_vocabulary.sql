-- =====================================================================
-- AI Visibility Platform — ceiling spec vocabulary (MVP brief Part 1)
--
-- Category subtree: plafondsystemen (root) -> systeemplafond -> six
-- material families. All new spec_attributes below are scoped to
-- 'systeemplafond' itself, not the individual materials -- they apply
-- regardless of which material family a given product belongs to, and
-- category_and_ancestor_codes() (0006_category_hierarchy.sql) means every
-- material subtype inherits them automatically without repeating the tag
-- six times.
--
-- Two notes on deviating from the brief, both because the literal
-- instruction couldn't be followed as-is:
--
-- 1. comparison_tolerance / confusable_with columns already exist
--    (0010_spec_comparison_metadata.sql, done in an earlier session step
--    before this brief was available) -- not re-added here.
--
-- 2. The brief's "reuse existing keys" list (reaction_to_fire,
--    smoke_production_class, flaming_droplets_class, thickness_mm,
--    weight_per_m2, mki_value, gwp_a1a3, recycled_content_pct,
--    epd_available, ce_marked) assumes a broader vocabulary than this
--    schema actually has. Checked directly: only reaction_to_fire,
--    thickness_mm, and ce_marked actually exist (all already
--    category_codes='{}', i.e. universal -- genuinely reusable as-is, no
--    change needed). The other seven don't exist and can't be "reused";
--    they're created here as new rows instead, with unit/norm/tolerance
--    inferred from standard EN/NMD construction-industry convention
--    since the brief's table only covers the 15 truly-new ceiling keys.
--    Flagged in the response, not just this comment.
--
-- 'demountable' already exists too, but scoped only to
-- technische_binnenwanden (not universal) -- extended rather than
-- duplicated, since spec_attributes.key is globally unique.
-- =====================================================================

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

-- ---------------------------------------------------------------------
-- New spec_attributes -- the 15 from the brief's table, plus the 7
-- "reuse" keys that turned out not to exist yet (see note above).
-- ---------------------------------------------------------------------

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

  -- The 7 "assumed to already exist" keys -- see migration header.
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

-- ---------------------------------------------------------------------
-- Extend 'demountable' rather than duplicate it (key is globally unique).
-- ---------------------------------------------------------------------

update spec_attributes
   set category_codes = array_append(category_codes, 'systeemplafond')
 where key = 'demountable'
   and not ('systeemplafond' = any(category_codes));

-- ---------------------------------------------------------------------
-- Confusion pairs, wired both directions.
-- ---------------------------------------------------------------------

update spec_attributes set confusable_with = array['nrc'] where key = 'sound_absorption_alpha_w';
update spec_attributes set confusable_with = array['sound_absorption_alpha_w'] where key = 'nrc';

update spec_attributes set confusable_with = array['airborne_sound_reduction_rw'] where key = 'sound_attenuation_dnfw';
update spec_attributes set confusable_with = array['sound_attenuation_dnfw'] where key = 'airborne_sound_reduction_rw';

update spec_attributes set confusable_with = array['reaction_to_fire'] where key = 'sound_absorption_class';
update spec_attributes set confusable_with = array['sound_absorption_class'] where key = 'reaction_to_fire';
