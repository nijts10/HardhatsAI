# Prompt taxonomy redesign — proposal (2026-08-14)

**Status: proposal, under review with stijn. Not yet applied to the
`prompts` table or `scoring.py` — the current 7-intent enum
(`spec_constrained`/`application_driven`/`comparative`/`compliance`/
`sustainability`/`brand_direct`/`problem_driven`, see `0014_prompts.sql`)
is still what's live.**

## Why

Reviewing the first real Hunter Douglas test run (HeartFelt Linear
PareauLux, 2026-08-11/12) surfaced two real problems with the current
prompt/scoring design, not just "the numbers looked flat":

1. **`presence_rate` conflates two fundamentally different success
   criteria under one metric.** For a prompt that already names the
   brand (`brand_direct`), "is the brand mentioned in the answer" is
   nearly tautological. For a prompt that describes a spec/requirement
   with no brand named, "is the brand mentioned" is the hard, valuable
   question. Treating both the same way with one metric flattens the
   result.
2. **`detect_mention()` only searches for the literal string "hunter
   douglas"** (verified in `benchmark.py`) — it does not credit an
   answer that correctly recommends a Hunter Douglas *product* by its
   sub-brand name (HeartFelt®, PareauLux, Luxalon) without also saying
   the parent company name. For exactly the prompts where product
   correctness is what matters, this likely undercounts real
   visibility.

Both point at the same underlying fix: prompts (and their scoring)
should split along **which kind of correct answer actually matters**,
not sit under one flat intent taxonomy scored the same way throughout.

Related, not yet built, deliberately deferred:
`visibility-platform-eligible-not-mentioned-gap` (Claude memory) — for
Axis B prompts, check whether the brand had a qualifying product per
its own ground truth even when the answer didn't surface it at all,
not just whether an extracted claim happened to be correct.

## The two axes

**Axis A — brand recall.** Prompt doesn't name Hunter Douglas. Success
= Hunter Douglas (or a recognized sub-brand) gets surfaced as an
option at all. This is what `presence_rate` already measures — the
fix here is category design (only ask this of prompts where it's a
meaningful test), not the metric itself.

**Axis B — product correctness.** Success = the *correct product*
(Hunter Douglas's or not) with *correct specs* surfaces, judged
against `claims` ground truth — recognized by product/sub-brand name,
not by requiring the literal company name to appear. This is where
the "eligible but not mentioned" gap and the sub-brand detection gap
both live.

## Proposed categories

| Code | Was | Axis | Success definition |
|---|---|---|---|
| A1 Functie-gedreven | `application_driven` | A | HD suggested as a supplier for a described application/need |
| A2 Leverancier/categorie-zoekopdracht | new (split from `comparative`) | A | HD appears in a requested list of suppliers/manufacturers |
| A3 Concurrent-geïnitieerde vergelijking | new (split from `comparative`) | A | HD volunteered as an alternative when a competitor is named first |
| B1 Eis-gedreven | `spec_constrained` | B | Qualifying product (by sub-brand/product name) surfaces for a stated hard spec |
| B2 Norm/regelgeving-gedreven | `compliance` | B | Same as B1, requirement expressed via a norm instead of a raw value |
| B3 Duurzaamheidseis | `sustainability` | B | Same as B1, environmental/sustainability spec — all 3 real example prompts in the current seed are eis-shaped, none ask about HD's own sustainability specs directly, so this belongs wholly to Axis B (not split, as first guessed) |
| B4 Probleem-gedreven aanbeveling | `problem_driven` | B | Same as B1, requirement implicit in a described problem |
| C1 Merk-specifieke productvraag | `brand_direct` | B only (A is trivial) | Brand/product already named — success is whether the stated specs are correct, not presence |

`comparative` splits into A2/A3 because its real seed examples are two
different question shapes ("Rockfon of Ecophon, wat zijn de
alternatieven?" vs "Top leveranciers van X" vs a HD-already-named
comparison) that don't share one success definition.

## Example prompts drafted for review

See the conversation this proposal came out of for the full drafted
set (4 examples per category, Dutch, systeemplafond domain, matching
the existing prompt register). Not copied into `seed.sql`/a migration
yet — pending stijn's review of both the category split and the
example wording.

## What implementing this would actually touch (not done yet)

- `prompt_intent` enum (`0014_prompts.sql`) — replace or extend with
  the 8 codes above, decide migration vs. new enum values
  (`comparative` splitting into two loses the ability to just add
  values, since existing prompts need re-tagging — needs a real
  decision before writing the migration, not assumed)
- `prompts` seed data — re-tag/replace the 50 existing rows
- `scoring.py` — presence_rate should probably only compute (or only
  be *reported* as meaningful) for Axis A categories; Axis B needs the
  sub-brand-name-aware detection AND the eligible-but-not-mentioned
  ground-truth check to be a meaningful metric at all
- `benchmark.py`'s `detect_mention()` — extend to accept a list of
  brand + sub-brand names, not one literal string
