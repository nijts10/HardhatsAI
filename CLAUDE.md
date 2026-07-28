# HardhatsAI

AI product-selection assistant for building engineers, driven from inside Revit.
An engineer selects an element, asks a question in plain language ("systeemwand
EI60 S200, max 3400 mm hoog"), and gets ranked real products with an explicit
account of which requirements are met, unmet, and unknown — every technical
value traceable to a page in a manufacturer document.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Database | Supabase Postgres 15 + pgvector | relational + vector + object storage + auth in one |
| Object storage | Supabase Storage (`supplier-documents`, `product-images`) | same auth model as the DB |
| Web | Next.js (App Router) on Vercel | supplier portal + public product viewer in one app |
| Ingestion worker | Python, long-running process (Fly/Railway) | PDF parsing exceeds Edge Function limits |
| Revit add-in | C# / .NET, Revit 2024+ API | WPF dockable panel |
| LLM | Claude (extraction + answer generation) | |

## Repo layout

```
apps/web/                 Next.js — supplier portal, product viewer, API routes
services/ingest/          Python worker — parse, chunk, embed, extract
revit/HardhatsAI.Revit/   C# add-in
supabase/migrations/      schema (source of truth, never edit applied files)
supabase/seed.sql         categories + controlled spec vocabulary
docs/INGESTION.md         pipeline contract — read before touching the worker
```

## Non-negotiable invariants

These are enforced by check constraints and must not be worked around.

1. **No fact without a citation.** Any `product_specs` row with
   `presence = 'stated'` must carry `document_id`, `page_number`, and a
   `source_snippet` that appears verbatim in that page's text. The worker
   verifies this before insert and discards values that fail. This is the
   anti-hallucination mechanism — not a prompt instruction.

2. **Three states, not two.** No row = never evaluated. `presence='not_stated'`
   = we looked and the document is silent. A NULL value on a stated row is
   illegal. The UI must render "unknown" differently from "does not meet".

3. **Hard filter before semantic rank.** Requirements like EI60 or Rw ≥ 45 are
   evaluated in SQL against typed spec values (`spec_requirement_status`).
   Embeddings only rank what survives. Never let vector similarity decide a
   compliance question.

4. **Unknown never silently excludes.** A missing spec surfaces as a data gap in
   `requirements_unknown`, it does not drop the product. Suppliers with complete
   data win on the small `data_completeness` ranking bonus instead.

5. **Specs are append-only.** Correcting a value means inserting a new row and
   calling `supersede_spec`. `product_specs_current_uniq` guarantees one current
   row per (product, spec).

6. **`service_role` stays server-side.** The worker uses it and bypasses RLS.
   The web app and the Revit add-in authenticate as the user and go through RLS.

7. **The extractor cannot invent spec keys.** Every value maps to a row in
   `spec_definitions` or is dropped. Add the key to `seed.sql` first.

## Schema orientation

- `documents` → `document_pages` → `document_chunks` (embeddings live here)
- `products` ←→ `documents` via `product_documents` (many-to-many: one datasheet
  often covers a whole product family)
- `product_specs` is the typed, cited fact table; `spec_definitions` is the
  controlled vocabulary that makes it filterable
- `queries` / `query_results` / `selection_events` capture the full engineer
  interaction, including which requirements each result satisfied
- `jobs` is a plain table with `claim_job()` using `FOR UPDATE SKIP LOCKED`

Enum ordinals rank **best = highest**, so "at least Euroclass B" is a plain
`>=` on the ordinal. Keep that convention when adding enum specs.

## Key functions

```sql
search_products(embedding, query_text, category_id, supplier_id,
                required_specs jsonb, strict, limit, pool)
match_chunks(embedding, product_ids, limit)   -- grounding for answer generation
spec_requirement_status(product_id, req jsonb) -- 'met' | 'unmet' | 'unknown'
claim_job(worker, kinds)
```

Requirement shape consumed by `search_products` and produced by the prompt parser:

```json
[{"key": "fire_resistance_ei", "op": ">=", "value": 60},
 {"key": "smoke_leakage_class", "op": "=", "value": "S200"},
 {"key": "reaction_to_fire", "op": "class>=", "value": "B"}]
```

Operators: `>=`, `<=`, `=`, `class>=`.
Note `impact_sound_lnw` is lower-is-better — the parser must emit `<=` for it.

## Commands

```bash
supabase start                 # local stack
supabase db reset              # re-apply all migrations + seed
supabase migration new <name>  # never hand-edit an applied migration
supabase gen types typescript --local > apps/web/lib/database.types.ts

cd services/ingest && python -m ingest.worker      # run the pipeline worker
cd apps/web && pnpm dev
```

## Embedding model

`vector(1536)` is wired into the schema and both search functions. Changing the
embedding model means a migration, a column alter, and a full re-embed of the
corpus. Decide before the first large ingest, not after.

## Build order

1. Ingest public manufacturer documentation (Rockwool, Kingspan, Isover,
   Saint-Gobain, Knauf) → parsing, extraction, search working against real
   messy PDFs. No supplier portal, no supplier auth yet.
2. Revit add-in: element context capture → prompt panel → ranked answers.
3. Product pages, save-to-project, parameter write-back, feedback logging.
4. Supplier portal and claiming — only once being findable is worth something.

## Not in scope yet

Pricing engines, payments, RFQ/tender flows, lead marketplace, inventory,
admin dashboards, mobile app, agents beyond product search and reasoning.

## Conventions

- SQL: snake_case, plural table names, `id uuid` PKs, timestamps as `timestamptz`
- Requirements and specs are always stored in canonical SI-ish units from
  `spec_definitions.unit`; normalise at extraction time, never at read time
- Dutch is the primary source language; keep `name_nl` and `name_en` on
  anything an engineer sees
- Prefer working over elegant, modular over monolithic, real data over mockups
