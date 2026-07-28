"""Manual ingestion entrypoint.

There is no supplier portal yet (see CLAUDE.md build order: step 1 is public
manufacturer documentation, ingested before any supplier signs up). This CLI
is the upload path until that portal exists — it creates an unclaimed
supplier_companies row on first use of a given slug, matching the comment
on that column in the schema.

Usage:
    python -m ingest.cli ingest path/to/datasheet.pdf \\
        --supplier-slug rockwool --supplier-name Rockwool \\
        --kind datasheet --title "Rockwool 211 datasheet"
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from . import db, storage
from .config import load_config


def _get_or_create_supplier(conn, slug: str, name: str) -> str:
    with conn.cursor() as cur:
        cur.execute("select id from supplier_companies where slug = %s", (slug,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute(
            "insert into supplier_companies (name, slug) values (%s, %s) returning id",
            (name, slug),
        )
        return cur.fetchone()["id"]


def cmd_ingest(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)
    client = storage.make_client(config.supabase_url, config.supabase_service_role_key)

    path = pathlib.Path(args.file)
    data = path.read_bytes()
    sha256 = storage.sha256_bytes(data)

    existing = db.find_document_by_sha256(conn, sha256)
    if existing:
        print(f"already ingested as document {existing['id']} (status={existing['status']}); not re-uploading")
        conn.close()
        return 0

    supplier_company_id = _get_or_create_supplier(conn, args.supplier_slug, args.supplier_name or args.supplier_slug)

    path_in_bucket = storage.storage_path(args.supplier_slug, sha256, ext=path.suffix.lstrip(".") or "pdf")
    storage.upload(client, path_in_bucket, data)

    document_id = db.insert_document(
        conn,
        supplier_company_id=supplier_company_id,
        uploaded_by=None,
        source=args.source,
        source_url=args.source_url,
        storage_bucket=storage.BUCKET,
        storage_path=path_in_bucket,
        original_filename=path.name,
        mime_type="application/pdf",
        byte_size=len(data),
        sha256=sha256,
        kind=args.kind,
        title=args.title,
    )
    db.enqueue_job(conn, "parse_document", {"document_id": document_id})
    conn.commit()
    conn.close()

    print(f"queued document {document_id} ({path.name}) for supplier {args.supplier_slug}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="upload a document and enqueue the pipeline")
    p_ingest.add_argument("file", help="path to a PDF on disk")
    p_ingest.add_argument("--supplier-slug", required=True)
    p_ingest.add_argument("--supplier-name", default=None)
    p_ingest.add_argument("--kind", default="datasheet",
                           choices=["datasheet", "dop", "epd", "certificate", "test_report",
                                    "installation_manual", "bim_asset", "brochure", "other"])
    p_ingest.add_argument("--title", default=None)
    p_ingest.add_argument("--source", default="crawled", choices=["supplier_upload", "crawled", "manual"])
    p_ingest.add_argument("--source-url", default=None)
    p_ingest.set_defaults(func=cmd_ingest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
