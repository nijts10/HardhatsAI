# Ingestion pipeline

From a manufacturer PDF to a searchable, cited product record.

## Stages

| # | Stage | Job kind | `documents.status` after |
|---|---|---|---|
| 1 | Store file + hash | — | `uploaded` |
| 2 | Enqueue | — | `queued` |
| 3 | Parse to pages | `parse_document` | `parsed` |
| 4 | Chunk | `parse_document` | `parsed` |
| 5 | Embed | `embed_chunks` | `parsed` |
| 6 | Extract specs | `extract_specs` | `extracting` |
| 7 | Verify + persist | `extract_specs` | `indexed` |

Any stage failure writes `parse_error` and sets `failed`. Jobs retry up to
`max_attempts` with backoff on `run_after`, then go `dead` for manual review.

## 1–2. Storage and idempotency

Upload to the `supplier-documents` bucket, compute SHA-256 of the bytes, insert
into `documents`. `sha256` is UNIQUE — a re-upload of identical bytes is a
conflict, not a second parse run. Re-processing an unchanged file is always an
explicit act (new `extraction_runs` row), never an accident.

Path convention: `{supplier_slug}/{sha256[:2]}/{sha256}.pdf`

## 3. Parsing

Datasheets are table-dominant. Naive text extraction destroys exactly the values
that matter — an EI rating in a spec table becomes an unmoored number.

- Text-layer PDFs: a layout-aware extractor (pdfplumber / Docling). Persist per
  page into `document_pages.text`, with word boxes into `document_pages.layout`
  for later highlighting.
- Scanned or heavily graphical pages: fall back to Claude vision per page,
  rendering the page to an image. Detect this by text-layer character count.
- Always persist page text before chunking. The verification step in stage 7
  depends on it.

## 4. Chunking

Rules, in priority order:

1. **Never split a table.** A table becomes one chunk regardless of size, with
   `chunk_type = 'table'` or `'spec_table'`. Serialise it as markdown so the
   row/column relationship survives embedding.
2. **Prepend `heading_path`.** `Technische gegevens > Brandwerendheid` in front
   of the content before embedding. Chunks stripped of their section heading are
   the single biggest cause of bad retrieval on technical documents.
3. Prose targets 500–800 tokens with ~15% overlap.
4. Record `page_start` / `page_end` on every chunk. Provenance depends on it.

## 5. Embedding

Batch chunks, write to `document_chunks.embedding`. The column is `vector(1536)`
and both search functions declare that dimension — changing model means a
migration plus a full re-embed. See CLAUDE.md.

## 6. Extraction

One LLM pass per document, given: the controlled vocabulary from
`spec_definitions` (filtered to the plausible categories), the chunk texts, and
their page numbers.

Output contract — the model returns, per product it identifies:

```json
{
  "canonical_name": "...",
  "manufacturer_ref": "...",
  "category_code": "systeemwand",
  "specs": [
    {
      "key": "fire_resistance_ei",
      "value_numeric": 60,
      "unit": "min",
      "presence": "stated",
      "page_number": 4,
      "source_snippet": "Brandwerendheid EI 60 volgens NEN-EN 13501-2",
      "confidence": 0.94
    },
    {
      "key": "airborne_sound_reduction_rw",
      "presence": "not_stated"
    }
  ]
}
```

Hard requirements on the prompt:

- Every key must exist in the supplied vocabulary. Unknown keys are dropped.
- `source_snippet` must be copied verbatim from the document, not paraphrased.
- Every requested key must appear in the output, either with a value or as
  `not_stated`. Silence is not an acceptable answer — that is what produces the
  "we don't know" state the engineer needs to see.
- One document routinely describes a product *family*. Emit one product per
  distinct article/system code rather than collapsing them.

Record the run in `extraction_runs` with `parser_version`, `prompt_version`,
`model`, and cost. This is what lets you re-extract the whole corpus after a
prompt change and diff before promoting.

## 7. Verification and persistence

Before any insert, for each `presence='stated'` value:

1. Normalise both snippet and `document_pages.text` for the cited page
   (collapse whitespace, unify unicode dashes and non-breaking spaces, casefold).
2. Assert the snippet is a substring of the page text. **If not, discard the
   value and log it.** A hallucinated citation is the cheapest hallucination to
   catch, and catching it here is what makes the no-hallucination promise real
   rather than aspirational.
3. Assert the numeric value's digits appear within the snippet.
4. Normalise units to `spec_definitions.unit`. Reject values that fail a
   plausibility range for that key (a lambda of 35 is 35 mW/mK misread).

Then:

- Upsert `products` (match on `supplier_company_id` + `manufacturer_ref`).
- For each spec, if a current row exists with a different value, insert the new
  row and call `supersede_spec(old, new)`; bump `products.version`.
- Link `product_documents`.
- Recompute `supplier_companies.data_completeness` as the share of
  category-relevant filterable specs that are `stated`.
- Set `documents.status = 'indexed'`, `indexed_at = now()`.

Derived values (e.g. `thermal_resistance_rd` from lambda and thickness) are
written with `presence='derived'`, citing the two source rows in
`source_snippet`. Never write a derived value as `stated`.

## Versioning

A newer revision of a datasheet is a new `documents` row with
`supersedes_document_id` pointing at the old one. The old document goes to
`superseded`; its specs are superseded rather than deleted. Nothing is ever
destroyed — an engineer who specified a product last year must still be able to
see what the documentation said at that time.

## Query path (for contrast)

1. Revit add-in posts element context + prompt → `queries`.
2. Prompt parser (Claude, structured output) produces `parsed_requirements` in
   the `search_products` shape, merging explicit prompt requirements with
   constraints implied by the element (wall thickness, height, fire compartment
   parameter if present).
3. `search_products(...)` — hard filter, then hybrid rank.
4. `match_chunks(...)` for the top products → grounding context.
5. Answer generation, permitted to cite only the returned chunk ids, and
   required to reproduce `requirements_unmet` and `requirements_unknown` rather
   than smoothing over them.
6. Persist to `query_results`; the engineer's action lands in
   `selection_events`, which is the training signal for ranking later.
