-- =====================================================================
-- Re-tag the 50 existing prompts onto the new axis taxonomy (0022), and
-- add the 4 new a2_leverancier_categorie examples -- that category had
-- zero real examples in the original 50 ("which suppliers offer X" was
-- never actually asked), so these are genuinely new prompts, not
-- re-tagged ones. Mirrors 0015_prompts_seed_systeemplafond.sql's own
-- precedent: prompt data lives in its own migration file, deliberately
-- not mirrored into seed.sql (see that migration's own comment).
-- =====================================================================

-- Most of the old taxonomy renames 1:1.
update prompts set intent = 'a1_functie_gedreven' where intent = 'application_driven';
update prompts set intent = 'b2_norm_regelgeving' where intent = 'compliance';
update prompts set intent = 'b3_duurzaamheidseis' where intent = 'sustainability';
update prompts set intent = 'b4_probleem_gedreven' where intent = 'problem_driven';
update prompts set intent = 'b1_eis_gedreven' where intent = 'spec_constrained';

-- 'comparative' splits by actual question shape -- a blanket rename would
-- have been wrong: a competitor-vs-competitor comparison, a generic
-- material-vs-material property comparison, and an HD-named comparison
-- are three different success definitions, not one category.
update prompts set intent = 'a3_concurrent_vergelijking'
  where intent = 'comparative' and text in (
    'Knauf AMF versus Armstrong, wat zijn de akoestische verschillen?',
    'Rockfon of Ecophon voor een schoolgebouw, wat is het verschil?',
    'Saint-Gobain Ecophon versus Rockfon, verschil in brandklasse?'
  );
update prompts set intent = 'c1_merk_specifiek'
  where intent = 'comparative' and text = 'Verschil tussen Hunter Douglas HeartFelt en een standaard baffleplafond';
update prompts set intent = 'b1_eis_gedreven'
  where intent = 'comparative' and text in (
    'Metalen plafond versus minerale wol plafond, voor- en nadelen',
    'Open of gesloten systeemplafond, wat is akoestisch gunstiger?',
    'Vilt plafond versus minerale wol, wat scoort beter op duurzaamheid?',
    'Wat is beter voor akoestiek: geperforeerd staal of glaswolplaat?'
  );

-- 'brand_direct': two are genuine direct-brand questions; the third
-- ("what does Rockfon offer") is really a competitor-named control
-- prompt in disguise -- it tests whether HD gets volunteered when a
-- competitor is asked about directly, the same success shape as a3.
update prompts set intent = 'c1_merk_specifiek'
  where intent = 'brand_direct' and text in (
    'Levert Hunter Douglas ook houten plafondsystemen?',
    'Welke akoestische plafonds levert Hunter Douglas?'
  );
update prompts set intent = 'a3_concurrent_vergelijking'
  where intent = 'brand_direct' and text = 'Welke systeemplafonds heeft Rockfon in het assortiment?';

-- New a2 examples (genuinely new, not re-tagged).
insert into prompts (category_id, intent, text)
select id, 'a2_leverancier_categorie'::prompt_intent, v.text
from product_categories, (values
  ('Welke fabrikanten leveren akoestische systeemplafonds voor kantoren?'),
  ('Noem leveranciers van metalen lamellenplafonds in Nederland'),
  ('Welke merken bieden systeemplafonds met Cradle to Cradle-certificering?'),
  ('Wie zijn de belangrijkste leveranciers van houten roosterplafonds voor de zakelijke markt?')
) as v(text)
where product_categories.code = 'systeemplafond';
