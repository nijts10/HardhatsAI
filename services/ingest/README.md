# ingest

Python worker that turns a supplier PDF into cited, searchable product specs.
See `docs/INGESTION.md` at the repo root for the pipeline contract this code
implements, and `CLAUDE.md` for the non-negotiable invariants (no fact without
a citation, three-state presence, controlled spec vocabulary).

## Setup

```bash
cd services/ingest
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in DATABASE_URL, SUPABASE_*, ANTHROPIC_API_KEY, OPENAI_API_KEY
```

## Running the worker

```bash
python -m ingest.worker
```

Polls `jobs` via `claim_job()` and runs whichever stage the job is for
(`parse_document` -> `embed_chunks` -> `extract_specs`), enqueuing the next
stage on success. A document reaches `documents.status = 'indexed'` once all
three have run.

## Ingesting a document

There's no supplier portal yet (see the build order in `CLAUDE.md` — public
manufacturer documentation comes first). Until it exists, this CLI is the
upload path:

```bash
python -m ingest.cli ingest path/to/datasheet.pdf \
  --supplier-slug rockwool --supplier-name Rockwool \
  --kind datasheet --title "Rockwool 211 datasheet"
```

Creates an unclaimed `supplier_companies` row on first use of a slug,
uploads the file to Supabase Storage, inserts the `documents` row (a
re-upload of identical bytes is a no-op — `sha256` is unique), and enqueues
`parse_document`.

## Module map

| Module | Stage |
|---|---|
| `parsing.py` | PDF -> per-page text, word-level font sizes, ruled tables. Falls back to Claude vision when the text layer is too sparse to trust. |
| `chunking.py` | Font-size heading detection, tables as atomic chunks, 500-800 token prose windows with 15% overlap. |
| `embedding.py` | OpenAI `text-embedding-3-small` (1536 dims, matches `document_chunks.embedding` natively). |
| `extraction.py` | One forced-tool-call Claude pass per document against the controlled `spec_definitions` vocabulary. |
| `verification.py` | The anti-hallucination check: normalizes and substring-matches every `source_snippet` against the cited page, checks the value's digits appear in the snippet, converts units, and rejects implausible values. |
| `persistence.py` | Upserts products, supersedes changed specs (append-only, `is_current` bookkeeping), recomputes `data_completeness`. |
| `db.py` | Thin data-access layer; callers own the transaction. |
| `worker.py` | Job loop: `claim_job()` -> dispatch by kind -> chain to the next stage. |
| `cli.py` | Manual document upload until the supplier portal exists. |

## Tests

```bash
pytest
```

Most tests run against a real Postgres 16 + pgvector database: `tests/conftest.py`
spins up a scratch database and applies the actual files in
`supabase/migrations/` and `supabase/seed.sql` (plus minimal stand-ins for the
Supabase-managed `auth.users`/`auth.uid()`/roles that a bare Postgres doesn't
have). This is deliberate — the append-only supersede logic and the
provenance check constraints are exactly the parts worth testing against the
real schema, not a mock of it. Needs a local `postgres` role reachable at
`127.0.0.1:5432` with password `postgres` (see `tests/conftest.py` for the
exact DSN); adjust there if your local setup differs.

`parsing.py` is tested against PDFs built on the fly with `reportlab`, not
mocked pdfplumber objects, so the font-size heading heuristic and table
detection run against real output.
