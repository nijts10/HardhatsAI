-- =====================================================================
-- AI Visibility Platform — mandatory source provenance
--
-- Every document_version must record WHERE it was fetched from and WHEN.
-- Deliberately on document_versions, not documents: a re-upload creates a
-- new version (0001_init.sql's own immutability rule), and that new
-- version may genuinely come from a different URL fetched at a different
-- time than the original -- source_url/retrieved_at are per-upload
-- provenance facts, same as sha256/storage_path/uploaded_by, which
-- already live here rather than on the logical document.
--
-- documents table is currently empty on the live project, so this is a
-- plain NOT NULL add with no backfill needed.
-- =====================================================================

alter table document_versions
  add column source_url text not null,
  add column retrieved_at timestamptz not null;
