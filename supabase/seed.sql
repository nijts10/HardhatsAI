-- =====================================================================
-- HardhatsAI — reference data seed
--
-- Only controlled vocabulary. No fake products: the corpus is built from
-- real manufacturer documentation from day one.
--
-- Adding a spec key here is what makes it extractable and filterable.
-- The extractor is not permitted to invent keys outside this table.
-- =====================================================================

insert into product_categories (code, name_nl, name_en) values
  ('systeemwand',            'Systeemwand',                  'System wall'),
  ('scheidingswand',         'Scheidingswand',               'Partition wall'),
  ('brandwerende_deur',      'Brandwerende deur',            'Fire-rated door'),
  ('binnendeur',             'Binnendeur',                   'Internal door'),
  ('kozijn',                 'Kozijn',                       'Window/door frame'),
  ('beglazing',              'Beglazing',                    'Glazing'),
  ('isolatie_plaat',         'Isolatieplaat',                'Insulation board'),
  ('isolatie_deken',         'Isolatiedeken',                'Insulation roll/blanket'),
  ('dakbedekking',           'Dakbedekking',                 'Roof covering'),
  ('gevelbekleding',         'Gevelbekleding',               'Facade cladding'),
  ('vloersysteem',           'Vloersysteem',                 'Floor system'),
  ('plafondsysteem',         'Plafondsysteem',               'Ceiling system'),
  ('doorvoering',            'Brandwerende doorvoering',     'Fire-stopping penetration seal'),
  ('kitwerk_afdichting',     'Kit- en afdichtingsmateriaal', 'Sealant and jointing'),
  ('constructief_element',   'Constructief element',         'Structural element')
on conflict (code) do nothing;

-- ---------------------------------------------------------------------
-- FIRE — EN 13501
--
-- enum_ordinals rank BEST = HIGHEST so a requirement like "at least
-- Euroclass B" is evaluated as ordinal(have) >= ordinal(B).
-- ---------------------------------------------------------------------

insert into spec_definitions
  (key, name_nl, name_en, data_type, unit, enum_values, enum_ordinals, norm_reference, category_codes, description)
values
  ('fire_resistance_ei', 'Brandwerendheid EI', 'Fire resistance EI',
   'numeric', 'min', null, null, 'EN 13501-2',
   '{systeemwand,scheidingswand,brandwerende_deur,doorvoering,vloersysteem,plafondsysteem}',
   'Integrity + insulation period in minutes. Store the number only; the criterion letters go in fire_resistance_criterion.'),

  ('fire_resistance_criterion', 'Brandwerendheidscriterium', 'Fire resistance criterion',
   'enum', null,
   '{R,E,EW,EI,REI,REI-M}',
   '{"R":1,"E":2,"EW":3,"EI":4,"REI":5,"REI-M":6}',
   'EN 13501-2', '{systeemwand,scheidingswand,brandwerende_deur,doorvoering,vloersysteem}',
   'Which criteria the rating covers. Higher ordinal = more onerous, not merely different.'),

  ('smoke_leakage_class', 'Rookdoorlatendheid', 'Smoke leakage class',
   'enum', null,
   '{Sa,S200,Sm}',
   '{"Sa":1,"S200":2,"Sm":3}',
   'EN 13501-2', '{brandwerende_deur,systeemwand,scheidingswand,doorvoering}',
   'Sa = ambient temperature only; S200 = also at 200 C; Sm = medium/high pressure.'),

  ('reaction_to_fire', 'Brandklasse (materiaal)', 'Reaction to fire (Euroclass)',
   'enum', null,
   '{A1,A2,B,C,D,E,F}',
   '{"A1":7,"A2":6,"B":5,"C":4,"D":3,"E":2,"F":1}',
   'EN 13501-1', '{}',
   'Bbl/Bouwbesluit references these classes directly for surface requirements.'),

  ('smoke_production_class', 'Rookproductieklasse', 'Smoke production class',
   'enum', null, '{s1,s2,s3}', '{"s1":3,"s2":2,"s3":1}',
   'EN 13501-1', '{}', 'Sub-classification of reaction to fire.'),

  ('flaming_droplets_class', 'Brandende druppels', 'Flaming droplets class',
   'enum', null, '{d0,d1,d2}', '{"d0":3,"d1":2,"d2":1}',
   'EN 13501-1', '{}', 'Sub-classification of reaction to fire.'),

-- ---------------------------------------------------------------------
-- THERMAL
-- ---------------------------------------------------------------------

  ('thermal_conductivity_lambda', 'Warmtegeleiding (lambda)', 'Thermal conductivity',
   'numeric_range', 'W/mK', null, null, 'EN 12667',
   '{isolatie_plaat,isolatie_deken,systeemwand,dakbedekking,gevelbekleding}',
   'Declared lambda. Datasheets often give a range; store min in value_numeric and max in value_numeric_max.'),

  ('thermal_resistance_rd', 'Warmteweerstand Rd', 'Thermal resistance Rd',
   'numeric', 'm2K/W', null, null, 'EN 12667',
   '{isolatie_plaat,isolatie_deken}',
   'Per declared thickness. Always co-extract thickness_mm or the value is meaningless.'),

  ('rc_value', 'Rc-waarde', 'Rc value (construction)',
   'numeric', 'm2K/W', null, null, 'NEN 1068',
   '{systeemwand,dakbedekking,vloersysteem,gevelbekleding}',
   'Dutch construction-level thermal resistance used by Bbl. Not interchangeable with Rd.'),

  ('u_value', 'U-waarde', 'U value',
   'numeric', 'W/m2K', null, null, 'NEN 1068',
   '{kozijn,beglazing,brandwerende_deur,binnendeur}',
   'State whether Ug, Uf or Uw in the source snippet.'),

-- ---------------------------------------------------------------------
-- ACOUSTIC
-- ---------------------------------------------------------------------

  ('airborne_sound_reduction_rw', 'Luchtgeluidisolatie Rw', 'Airborne sound reduction Rw',
   'numeric', 'dB', null, null, 'EN ISO 717-1',
   '{systeemwand,scheidingswand,brandwerende_deur,binnendeur,beglazing,kozijn}',
   'Laboratory value. Do not conflate with on-site DnT,A.'),

  ('rw_c', 'Rw + C', 'Rw + C spectrum adaptation',
   'numeric', 'dB', null, null, 'EN ISO 717-1', '{}', null),

  ('rw_ctr', 'Rw + Ctr', 'Rw + Ctr spectrum adaptation',
   'numeric', 'dB', null, null, 'EN ISO 717-1', '{}',
   'Traffic-noise weighted. Relevant for facade elements.'),

  ('impact_sound_lnw', 'Contactgeluid Lnw', 'Impact sound Ln,w',
   'numeric', 'dB', null, null, 'EN ISO 717-2', '{vloersysteem}',
   'Lower is better — invert the comparison operator when parsing requirements.'),

  ('sound_absorption_alpha_w', 'Geluidabsorptie alpha_w', 'Sound absorption alpha_w',
   'numeric', null, null, null, 'EN ISO 11654',
   '{plafondsysteem,gevelbekleding}', 'Dimensionless, 0.00–1.00.'),

-- ---------------------------------------------------------------------
-- GEOMETRY & PHYSICAL
-- ---------------------------------------------------------------------

  ('thickness_mm', 'Dikte', 'Thickness', 'numeric', 'mm', null, null, null, '{}', null),
  ('width_mm',     'Breedte', 'Width',   'numeric', 'mm', null, null, null, '{}', null),
  ('height_mm',    'Hoogte', 'Height',   'numeric', 'mm', null, null, null, '{}', null),
  ('length_mm',    'Lengte', 'Length',   'numeric', 'mm', null, null, null, '{}', null),

  ('max_height_mm', 'Maximale wandhoogte', 'Maximum wall height',
   'numeric', 'mm', null, null, null, '{systeemwand,scheidingswand}',
   'Frequently the binding constraint in practice and frequently missed by generic extraction.'),

  ('density', 'Volumieke massa', 'Density',
   'numeric', 'kg/m3', null, null, 'EN 1602', '{isolatie_plaat,isolatie_deken}', null),

  ('weight_per_m2', 'Gewicht per m2', 'Weight per m2',
   'numeric', 'kg/m2', null, null, null, '{systeemwand,gevelbekleding,dakbedekking}',
   'Needed for structural load checks against the selected Revit element.'),

  ('compressive_strength_cs', 'Druksterkte', 'Compressive strength',
   'numeric', 'kPa', null, null, 'EN 826', '{isolatie_plaat,vloersysteem}', null),

  ('water_vapour_resistance_mu', 'Waterdampdiffusieweerstand mu', 'Water vapour resistance factor',
   'numeric', null, null, null, 'EN 12086', '{isolatie_plaat,dakbedekking}', null),

  ('service_temp_min', 'Min. gebruikstemperatuur', 'Minimum service temperature',
   'numeric', 'C', null, null, null, '{}', null),
  ('service_temp_max', 'Max. gebruikstemperatuur', 'Maximum service temperature',
   'numeric', 'C', null, null, null, '{}', null),

  ('load_bearing', 'Dragend', 'Load bearing',
   'boolean', null, null, null, null, '{systeemwand,vloersysteem,constructief_element}', null),

-- ---------------------------------------------------------------------
-- COMPLIANCE & ENVIRONMENT
-- ---------------------------------------------------------------------

  ('ce_marked', 'CE-markering', 'CE marked',
   'boolean', null, null, null, 'CPR 305/2011', '{}', null),

  ('komo_certified', 'KOMO-certificaat', 'KOMO certified',
   'boolean', null, null, null, null, '{}', 'NL-specific quality mark; strong buying signal for Dutch specifiers.'),

  ('dop_number', 'DoP-nummer', 'Declaration of Performance number',
   'text', null, null, null, 'CPR 305/2011', '{}', null),

  ('epd_available', 'EPD beschikbaar', 'EPD available',
   'boolean', null, null, null, 'EN 15804', '{}', null),

  ('gwp_a1a3', 'GWP A1-A3', 'Global warming potential A1-A3',
   'numeric', 'kgCO2eq', null, null, 'EN 15804', '{}',
   'Always co-extract the declared functional unit into the source snippet.'),

  ('mki_value', 'MKI-waarde', 'Environmental cost indicator (MKI)',
   'numeric', 'EUR', null, null, 'NMD / Bepalingsmethode', '{}',
   'Dutch shadow-price indicator feeding MPG calculations.'),

  ('recycled_content_pct', 'Aandeel gerecycled materiaal', 'Recycled content',
   'numeric', '%', null, null, null, '{}', null),

  ('application_area', 'Toepassingsgebied', 'Application area',
   'text', null, null, null, null, '{}',
   'Free text; not filterable but heavily used in semantic retrieval.'),

  ('installation_notes', 'Verwerkingsvoorschrift', 'Installation notes',
   'text', null, null, null, null, '{}', null),

  ('limitations', 'Beperkingen', 'Limitations and exclusions',
   'text', null, null, null, null, '{}',
   'Explicit exclusions in the datasheet. High value for the "probably not suitable" answer path.')
on conflict (key) do nothing;

update spec_definitions set is_filterable = false
 where key in ('application_area', 'installation_notes', 'limitations', 'dop_number');
