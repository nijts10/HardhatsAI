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
cp .env.example .env   # fill in DATABASE_URL, SUPABASE_*, ANTHROPIC_API_KEY, OPENAI_API_KEY
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

# 4. Generate a data-coverage report once some claims exist.
python -m ingest.cli generate-report --brand-slug acme --kind audit
```

## What `generate-report` actually produces

A data-*coverage* report, not an AI-visibility score. No prompt has been
run against any AI model yet — that's a later phase, and which AI systems
to actually benchmark against is a product decision the brief doesn't
make, not something to default silently. What this generates: per product,
how many of the category-relevant spec_attributes have a current,
cited claim versus how many are still missing, plus any certifications
found. Real, useful, and honest about what it is.

## Tests

```bash
pytest
```

Same approach as the HardhatsAI worker: most tests run against a real
Postgres 16 + pgvector database with the actual
`visibility-platform/supabase/migrations/` and `seed.sql` applied, not
mocked. `parsing.py`/`chunking.py` tests are copied from the HardhatsAI
suite unchanged since the module they test is unchanged.
