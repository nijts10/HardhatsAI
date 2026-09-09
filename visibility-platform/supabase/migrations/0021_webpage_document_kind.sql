-- =====================================================================
-- Add 'webpage' to document_kind -- ground truth now has a second real
-- source besides uploaded PDFs: a brand's own live website, ingested
-- through the exact same extraction + verbatim-citation pipeline
-- (services/ingest/ingest/webpage.py, pipeline.run_webpage). Found while
-- auditing Hunter Douglas: several "documentation gaps" the AI got right
-- turned out to be genuinely, correctly published on hunterdouglasarchitectural.eu
-- -- just never ingested, because the pipeline only ever read PDFs.
-- =====================================================================

alter type document_kind add value 'webpage';
