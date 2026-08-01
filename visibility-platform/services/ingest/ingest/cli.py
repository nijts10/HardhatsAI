"""Analyst-facing CLI until the portal exists.

Usage:
    python -m ingest.cli create-product --brand-slug acme --category-code \\
        brandwerende_systeemwanden --name "Acme 211"

    python -m ingest.cli upload path/to/datasheet.pdf \\
        --org-slug acme --product-slug acme-211 --kind datasheet \\
        --title "Acme 211 datasheet"

    # A revised datasheet for the SAME logical document is a new version,
    # not a new document -- pass --document-id to say so explicitly. There
    # is no automatic "this looks like the same document" matching; the
    # brief's immutability principle only says a re-upload creates a new
    # version, it doesn't say how to detect "this is a re-upload" from
    # bytes alone (different content, by definition), so the caller states
    # it.
    python -m ingest.cli upload path/to/datasheet-v2.pdf \\
        --org-slug acme --document-id <uuid-from-first-upload>
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from . import benchmark, db, pipeline, report, storage
from .config import load_config
from .persistence import slugify


def cmd_create_product(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand_slug)
    if brand is None:
        print(f"no brand with slug '{args.brand_slug}' -- create it in the portal/dashboard first")
        return 1

    category = db.get_category_by_code(conn, args.category_code) if args.category_code else None
    if args.category_code and category is None:
        print(f"no category with code '{args.category_code}'")
        return 1

    product_id = db.create_product(
        conn, brand_id=brand["id"], category_id=category["id"] if category else None,
        name=args.name, slug=slugify(args.name, args.manufacturer_ref),
        manufacturer_ref=args.manufacturer_ref,
    )
    conn.commit()
    conn.close()
    print(f"created product {product_id} ({args.name})")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)
    client = storage.make_client(config.supabase_url, config.supabase_service_role_key)

    org = db.get_organization_by_slug(conn, args.org_slug)
    if org is None:
        print(f"no organization with slug '{args.org_slug}' -- create it in the portal/dashboard first")
        return 1

    path = pathlib.Path(args.file)
    data = path.read_bytes()
    sha256 = storage.sha256_bytes(data)

    existing_version = db.find_version_by_sha256(conn, sha256)
    if existing_version:
        print(f"already uploaded as document_version {existing_version['id']}; not re-uploading")
        conn.close()
        return 0

    if args.document_id:
        document_id = args.document_id
        db.get_document(conn, document_id)  # raises if it doesn't exist
    else:
        product = None
        if args.product_slug:
            product = db.get_product_by_slug(conn, args.product_slug)
            if product is None:
                print(f"no product with slug '{args.product_slug}'")
                return 1
        document_id = db.create_document(
            conn, organization_id=org["id"], product_id=product["id"] if product else None,
            title=args.title, kind=args.kind,
        )

    path_in_bucket = storage.storage_path(args.org_slug, sha256, ext=path.suffix.lstrip(".") or "pdf")
    storage.upload(client, path_in_bucket, data)

    document_version_id = db.create_document_version(
        conn, document_id=document_id, storage_path=path_in_bucket,
        original_filename=path.name, mime_type="application/pdf", byte_size=len(data),
        sha256=sha256, uploaded_by=None,
    )
    conn.commit()
    print(f"uploaded document_version {document_version_id} (document {document_id})")

    document = db.get_document(conn, document_id)
    if document["product_id"] is None:
        print("document has no product linked yet -- skipping extraction; "
              "link it to a product and re-run the pipeline manually")
        conn.close()
        return 0

    print("running extraction pipeline...")
    try:
        result = pipeline.run(conn, config, document_version_id=document_version_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"done: {result}")
    conn.close()
    return 0


def cmd_generate_report(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand_slug)
    if brand is None:
        print(f"no brand with slug '{args.brand_slug}'")
        return 1

    report_id = report.create_report(
        conn, brand_id=brand["id"], organization_id=brand["organization_id"], kind=args.kind,
    )
    conn.commit()
    conn.close()
    print(f"created report {report_id} (kind={args.kind}, status=draft)")
    return 0


def cmd_run_benchmark(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand_slug)
    if brand is None:
        print(f"no brand with slug '{args.brand_slug}'")
        return 1

    if args.model == "chatgpt":
        if not config.openai_api_key:
            print("OPENAI_API_KEY is not set")
            return 1
        caller = benchmark.make_chatgpt_caller(config.openai_api_key, config.openai_chat_model)
    else:
        if not config.google_api_key:
            print("GOOGLE_API_KEY is not set")
            return 1
        caller = benchmark.make_gemini_caller(config.google_api_key, config.gemini_model)

    print(f"running {args.model} against the active prompt library for {brand['name']}...")
    run_id, stats = benchmark.run_benchmark(
        conn, organization_id=brand["organization_id"], brand_id=brand["id"],
        brand_name=brand["name"], model=args.model, caller=caller,
    )
    conn.commit()
    conn.close()
    print(f"run {run_id}: {stats.mentions}/{stats.prompts_run} prompts mentioned "
          f"{brand['name']} ({stats.mention_rate:.0%})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    p_product = sub.add_parser("create-product", help="create a product under an existing brand")
    p_product.add_argument("--brand-slug", required=True)
    p_product.add_argument("--category-code", default=None)
    p_product.add_argument("--name", required=True)
    p_product.add_argument("--manufacturer-ref", default=None)
    p_product.set_defaults(func=cmd_create_product)

    p_upload = sub.add_parser("upload", help="upload a document version and run extraction")
    p_upload.add_argument("file", help="path to a PDF on disk")
    p_upload.add_argument("--org-slug", required=True)
    p_upload.add_argument("--product-slug", default=None, help="omit only for a brand-new document")
    p_upload.add_argument("--document-id", default=None, help="add a new version to this existing document")
    p_upload.add_argument("--kind", default="datasheet",
                           choices=["datasheet", "dop", "epd", "certificate", "test_report",
                                    "installation_manual", "brochure", "bestektekst", "bim_asset", "other"])
    p_upload.add_argument("--title", default=None)
    p_upload.set_defaults(func=cmd_upload)

    p_report = sub.add_parser("generate-report", help="generate a data-coverage report for a brand")
    p_report.add_argument("--brand-slug", required=True)
    p_report.add_argument("--kind", default="audit", choices=["audit", "monthly"])
    p_report.set_defaults(func=cmd_generate_report)

    p_benchmark = sub.add_parser("run-benchmark", help="run the prompt library against ChatGPT or Gemini")
    p_benchmark.add_argument("--brand-slug", required=True)
    p_benchmark.add_argument("--model", required=True, choices=["chatgpt", "gemini"])
    p_benchmark.set_defaults(func=cmd_run_benchmark)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
