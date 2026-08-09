-- =====================================================================
-- AI Visibility Platform — 50 Dutch systeemplafond prompts (MVP brief
-- Part 2). Prompts 1-24 are verbatim from the brief (a domain expert's
-- own examples). 25-50 are generated in the same register (terse Dutch
-- bouwfysicus/architect typing into a chat box -- no pleasantries, norm
-- codes mixed with plain language), distributed across the same 7
-- intents at a similar relative weight to the given 24.
-- =====================================================================

with cat as (
  select id from product_categories where code = 'systeemplafond'
)
insert into prompts (category_id, intent, text)
select cat.id, v.intent::prompt_intent, v.text
from cat, (values
  -- spec_constrained (12: 1-5 verbatim from brief, 6-12 generated)
  ('spec_constrained', 'Systeemplafond met αw minimaal 0,90 en brandklasse A2-s1,d0'),
  ('spec_constrained', 'Demontabel metalen plafond, module 600x600, gewicht onder 5 kg/m²'),
  ('spec_constrained', 'Akoestisch plafond met Dnf,w van minimaal 40 dB voor kantoorscheiding'),
  ('spec_constrained', 'Plafondsysteem met lichtreflectie boven 85% en absorptieklasse A'),
  ('spec_constrained', 'Geperforeerd metalen plafond, perforatiegraad rond 16%, met akoestisch vlies'),
  ('spec_constrained', 'Plafondsysteem met NRC 0,75 en modulemaat 1200x600'),
  ('spec_constrained', 'Metalen lamellenplafond met perforatiegraad 20% en Rw 35 dB'),
  ('spec_constrained', 'Systeemplafond brandklasse B-s2,d0, geschikt tot vochtklasse 90%'),
  ('spec_constrained', 'Baffleplafond met absorptieklasse B en gewicht max 3 kg/m²'),
  ('spec_constrained', 'Gipsplafond met corrosieweerstandsklasse C voor vochtige ruimte'),
  ('spec_constrained', 'Houten roosterplafond, open profiel, lichtreflectie minimaal 70%'),
  ('spec_constrained', 'Systeemplafond met EPD beschikbaar en GWP onder 5 kg CO2-eq/m²'),

  -- application_driven (12: 6-11 verbatim from brief, 13-18 generated)
  ('application_driven', 'Welk systeemplafond is geschikt voor een klaslokaal met veel nagalm?'),
  ('application_driven', 'Plafond voor een zwembad, bestand tegen hoge luchtvochtigheid'),
  ('application_driven', 'Systeemplafond voor een operatiekamer, reinigbaar en hygiënisch'),
  ('application_driven', 'Akoestisch plafond voor een open kantoortuin met werkplekken'),
  ('application_driven', 'Plafondsysteem voor een sporthal, balbestendig en geluidsabsorberend'),
  ('application_driven', 'Welk plafond gebruik je in een keuken van een restaurant?'),
  ('application_driven', 'Welk plafond past bij een theaterzaal met strenge akoestische eisen?'),
  ('application_driven', 'Systeemplafond voor een parkeergarage, corrosiebestendig'),
  ('application_driven', 'Plafond voor een serverruimte, brandwerend en onderhoudsvriendelijk'),
  ('application_driven', 'Akoestisch plafond voor een callcenter met veel werkplekken'),
  ('application_driven', 'Welk systeemplafond werkt in combinatie met klimaatplafonds?'),
  ('application_driven', 'Plafondsysteem voor een treinstation, hoog en goed bereikbaar voor onderhoud'),

  -- comparative (8: 12-15 verbatim from brief, 25-28 generated)
  ('comparative', 'Rockfon of Ecophon voor een schoolgebouw, wat is het verschil?'),
  ('comparative', 'Metalen plafond versus minerale wol plafond, voor- en nadelen'),
  ('comparative', 'Verschil tussen Hunter Douglas HeartFelt en een standaard baffleplafond'),
  ('comparative', 'Wat is beter voor akoestiek: geperforeerd staal of glaswolplaat?'),
  ('comparative', 'Knauf AMF versus Armstrong, wat zijn de akoestische verschillen?'),
  ('comparative', 'Vilt plafond versus minerale wol, wat scoort beter op duurzaamheid?'),
  ('comparative', 'Open of gesloten systeemplafond, wat is akoestisch gunstiger?'),
  ('comparative', 'Saint-Gobain Ecophon versus Rockfon, verschil in brandklasse?'),

  -- compliance (8: 16-19 verbatim from brief, 29-32 generated)
  ('compliance', 'Welke eisen stelt het Bouwbesluit aan geluidsabsorptie in een klaslokaal?'),
  ('compliance', 'Frisse Scholen klasse B akoestiek, welk plafond haalt dat?'),
  ('compliance', 'Moet een systeemplafond CE-gemarkeerd zijn volgens EN 13964?'),
  ('compliance', 'Welke brandklasse is vereist voor een plafond in een vluchtroute?'),
  ('compliance', 'Wat zegt het Bouwbesluit over brandwerendheid van plafonds in vluchtwegen?'),
  ('compliance', 'Is een DoP verplicht voor een systeemplafond onder CPR 305/2011?'),
  ('compliance', 'Welke norm geldt voor geluidsabsorptie in plafonds, EN ISO 11654?'),
  ('compliance', 'Frisse Scholen klasse A, welke plafondeisen horen daarbij?'),

  -- sustainability (5: 20-22 verbatim from brief, 33-34 generated)
  ('sustainability', 'Systeemplafond met de laagste MKI-waarde'),
  ('sustainability', 'Welk plafond scoort goed voor BREEAM Excellent?'),
  ('sustainability', 'Demontabel plafond met hoog aandeel gerecycled materiaal'),
  ('sustainability', 'Systeemplafond met hoogste percentage gerecycled materiaal'),
  ('sustainability', 'Welk plafondmerk publiceert een EPD volgens EN 15804?'),

  -- brand_direct (3: 23 verbatim from brief, 35-36 generated)
  ('brand_direct', 'Welke akoestische plafonds levert Hunter Douglas?'),
  ('brand_direct', 'Welke systeemplafonds heeft Rockfon in het assortiment?'),
  ('brand_direct', 'Levert Hunter Douglas ook houten plafondsystemen?'),

  -- problem_driven (2: 24 verbatim from brief, 37 generated)
  ('problem_driven', 'Mijn systeemplafond hangt door bij hoge luchtvochtigheid, welk alternatief?'),
  ('problem_driven', 'Nagalm in kantoortuin is te hoog, welk plafond lost dit op?')
) as v(intent, text);
