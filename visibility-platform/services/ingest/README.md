# ingest (AI Visibility Platform)

Python worker for Phase 2: PDF -> pages -> chunks -> embeddings -> cited
claims, plus a data-coverage report generator. See
`visibility-platform/supabase/migrations/` for the schema this implements.

Sibling of `services/ingest/` (the HardhatsAI worker) — deliberately
separate services, separate Supabase projects. `parsing.py`, `chunking.py`,
and `embedding.py` are copied verbatim from there; they have no dependency
on either schema. Everything else (`db.py`, `extraction.py`,
`verification.py`, `persistence.py`, `pipeline.py`, `report.py`, `cli.py`)
is written against this project's claims/spec_attributes vocabulary
instead of HardhatsAI's product_specs/spec_definitions.

## Why no job queue

The HardhatsAI worker polls a `jobs` table because it's bulk-ingesting many
manufacturer PDFs unattended. This platform's Phase 2 is analyst-driven —
one client's documents, uploaded manually, at low volume. `cli.py upload`
runs the whole pipeline synchronously and prints the result. Add a queue
back if upload volume ever actually justifies one.

## Setup

```bash
cd visibility-platform/services/ingest
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in DATABASE_URL, SUPABASE_*, ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY
```

## Usage

There's no portal yet, so this CLI is the only way to get data in until
one exists.

```bash
# 1. Create a product under an existing brand (create orgs/brands via the
#    Supabase dashboard or SQL for now — that's an analyst/sales action,
#    not something this ingest CLI does).
python -m ingest.cli create-product --brand-slug acme \
  --category-code brandwerende_systeemwanden --name "Acme 211"

# 2. Upload a document and run the pipeline (parse, chunk, embed, extract,
#    persist claims) in one shot.
python -m ingest.cli upload path/to/datasheet.pdf \
  --org-slug acme --product-slug acme-211 --kind datasheet \
  --title "Acme 211 datasheet"

# 3. A revised datasheet is a new VERSION of the same logical document —
#    pass --document-id (printed by step 2) rather than letting it create
#    a new document. There's no automatic "this looks like the same file"
#    detection; different bytes are different bytes, so the caller states
#    the relationship explicitly.
python -m ingest.cli upload path/to/datasheet-v2.pdf \
  --org-slug acme --document-id <uuid-from-step-2>

# 4. Run the prompt library against a real AI model and see whether the
#    brand actually gets mentioned. Costs real API credits each run.
python -m ingest.cli run-benchmark --brand-slug acme --model chatgpt
python -m ingest.cli run-benchmark --brand-slug acme --model gemini

# 5. Generate a report. Coverage is always included; the AI-visibility
#    section appears once at least one benchmark run above has completed.
python -m ingest.cli generate-report --brand-slug acme --kind audit
```

## What `generate-report` actually produces

Two independent halves. **Data coverage**: per product, how many of the
category-relevant spec_attributes have a current, cited claim versus how
many are still missing, plus any certifications found — doesn't depend on
any benchmark having run. **AI visibility**: mention rate per model from
the most recent successful `run-benchmark` for this brand, plus the exact
prompts where the brand didn't get mentioned. If no benchmark has run yet,
the report says so explicitly rather than silently omitting the section —
an empty result means "not measured", never "0%".

## How `run-benchmark` actually measures visibility

Calls the real ChatGPT (OpenAI chat completions) or Gemini (Google
`google-genai`) API with the prompt exactly as written in `prompt_library`
and a neutral system message — no mention of the brand being audited, no
retrieval/grounding/tools that could artificially inject it. Then it's a
plain case-insensitive substring check for the brand's name in the raw
response. Honest about what that is: a real signal, not a perfect replica
of what a consumer sees in the ChatGPT/Gemini apps (which may have
browsing or other tools enabled depending on the user's settings) — the
plain chat-completions API is the closest reproducible baseline available.

`OPENAI_CHAT_MODEL` / `GEMINI_MODEL` in `.env.example` are current as of
2026-08 — model names go stale fast, verify before relying on them.

## Tests

```bash
pytest
```

Same approach as the HardhatsAI worker: most tests run against a real
Postgres 16 + pgvector database with the actual
`visibility-platform/supabase/migrations/` and `seed.sql` applied, not
mocked. `parsing.py`/`chunking.py` tests are copied from the HardhatsAI
suite unchanged since the module they test is unchanged.
