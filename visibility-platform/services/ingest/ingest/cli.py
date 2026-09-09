"""Analyst-facing CLI until the portal exists.

Usage:
    python -m ingest.cli create-product --brand-slug acme --category-code \\
        brandwerende_systeemwanden --name "Acme 211"

    python -m ingest.cli upload path/to/datasheet.pdf \\
        --org-slug acme --product-slug acme-211 --kind datasheet \\
        --title "Acme 211 datasheet" \\
        --source-url https://acme.example/downloads/211-datasheet.pdf \\
        --retrieved-at 2026-08-09

    # A revised datasheet for the SAME logical document is a new version,
    # not a new document -- pass --document-id to say so explicitly. There
    # is no automatic "this looks like the same document" matching; the
    # brief's immutability principle only says a re-upload creates a new
    # version, it doesn't say how to detect "this is a re-upload" from
    # bytes alone (different content, by definition), so the caller states
    # it. --source-url/--retrieved-at are mandatory again here too -- a
    # revised version can genuinely come from a different URL/time than
    # the original.
    python -m ingest.cli upload path/to/datasheet-v2.pdf \\
        --org-slug acme --document-id <uuid-from-first-upload> \\
        --source-url https://acme.example/downloads/211-datasheet-v2.pdf \\
        --retrieved-at 2026-09-01

    # A fact the extractor found but couldn't map to any spec_attributes
    # key lands in review_queue, not claims -- triage it here. Promoting
    # only marks the row; add the real spec_attributes row via seed.sql
    # first (same controlled-vocabulary rule as everywhere else), then
    # promote points the finding at it for traceability.
    python -m ingest.cli review list
    python -m ingest.cli review promote <id> --key water_resistance_class
    python -m ingest.cli review reject <id>

    # Batch upload (STAP 7). No product is linked and extraction does not
    # run -- this just gets documents in with correct provenance. --dir
    # must contain sources.json (not a CLI flag -- see cmd_ingest_dir's
    # docstring for why), one entry per PDF filename:
    #   {"technical-fiche-x.pdf": {"source_url": "https://...",
    #                               "retrieved_at": "2026-08-09",
    #                               "kind": "datasheet"}}
    python -m ingest.cli ingest --brand hunter-douglas --dir .\\docs\\hd\\

    # MVP brief Part 3 -- visibility.py's engine, reads from `prompts`
    # (Part 2), not prompt_library, and runs all three real consumer-facing
    # AI products WITH their real web-search/grounding tool on, unlike
    # run-benchmark's plain chat completions above. --engine is repeatable
    # to restrict to a subset; omit it to run all three. Always dry-run
    # first -- it prints the planned answer count and a rough cost estimate
    # without making any calls.
    python -m ingest.cli benchmark --brand hunter-douglas --category systeemplafond --dry-run
    python -m ingest.cli benchmark --brand hunter-douglas --category systeemplafond

    # A run that hit VISIBILITY_MAX_ANSWERS_PER_RUN or
    # VISIBILITY_COST_CEILING_USD stops without failing (status stays
    # 'running') -- continue it with the run_id printed at stop time.
    # --resume-run only ever targets one engine (a run is engine-locked),
    # so --engine here must resolve to exactly one.
    python -m ingest.cli benchmark --brand hunter-douglas --category systeemplafond \\
        --engine openai --resume-run <run-id-from-earlier-output>

    # MVP brief Part 4 -- extract + diff technical claims out of one
    # benchmark run's answers (visibility_answers) against verified ground
    # truth (claims). Resumable per-answer: a re-run skips any answer that
    # already has answer_claims rows.
    python -m ingest.cli claims extract --run <run-id-from-benchmark-output>
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from typing import Optional

from anthropic import Anthropic

from . import (
    benchmark, claims_diff, crawlability, db, page_visibility, pdf_report, pipeline, report, scoring, storage,
    visibility, webpage,
)
from .config import load_config
from .http_fetch import make_httpx_fetcher
from .persistence import slugify

_DOCUMENT_KINDS = ["datasheet", "dop", "epd", "certificate", "test_report",
                    "installation_manual", "brochure", "bestektekst", "bim_asset", "other"]


def _parse_retrieved_at(value: str) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"--retrieved-at {value!r} isn't a valid ISO 8601 datetime "
            f"(e.g. 2026-08-09 or 2026-08-09T10:00:00+02:00)"
        ) from exc


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
    if args.product_url:
        db.set_product_url(conn, product_id, args.product_url)
    conn.commit()
    conn.close()
    print(f"created product {product_id} ({args.name})")
    return 0


def cmd_update_product(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    product = db.get_product_by_slug(conn, args.product_slug)
    if product is None:
        print(f"no product with slug '{args.product_slug}'")
        return 1

    db.set_product_url(conn, product["id"], args.product_url)
    conn.commit()
    conn.close()
    print(f"product {product['id']} ({product['name']}): product_url set to {args.product_url}")
    return 0


def cmd_update_brand(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand_slug)
    if brand is None:
        print(f"no brand with slug '{args.brand_slug}'")
        return 1

    db.set_brand_website(conn, brand["id"], args.website)
    conn.commit()
    conn.close()
    print(f"brand {brand['id']} ({brand['name']}): website set to {args.website}")
    return 0


def cmd_crawlability(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand)
    if brand is None:
        print(f"no brand with slug '{args.brand}'")
        return 1
    if not brand.get("website"):
        print(f"brand '{args.brand}' has no website set -- set one first: "
              f"python -m ingest.cli update-brand --brand-slug {args.brand} --website https://...")
        return 1

    check_id, stats = crawlability.run_crawlability_check(
        conn, brand_id=brand["id"], website_url=brand["website"], fetcher=make_httpx_fetcher(),
    )
    conn.commit()
    conn.close()

    print(f"crawlability check {check_id} for {brand['name']} ({brand['website']}):")
    if stats.fetch_error:
        print(f"  robots.txt fetch problem: {stats.fetch_error} -- all agents recorded as undetermined")
    print(f"  allowed ({len(stats.allowed)}): {', '.join(stats.allowed) or '-'}")
    print(f"  blocked ({len(stats.blocked)}): {', '.join(stats.blocked) or '-'}")
    if stats.undetermined:
        print(f"  undetermined ({len(stats.undetermined)}): {', '.join(stats.undetermined)}")
    return 0


def cmd_page_visibility(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand)
    if brand is None:
        print(f"no brand with slug '{args.brand}'")
        return 1

    stats = page_visibility.run_page_visibility_check(conn, brand_id=brand["id"], fetcher=make_httpx_fetcher())
    conn.commit()
    conn.close()

    print(f"page-visibility check for {brand['name']}:")
    print(f"  products checked: {stats.products_checked} "
          f"(skipped: {stats.products_skipped_no_url} no product_url, "
          f"{stats.products_skipped_no_ground_truth} no ground truth)")
    print(f"  claims checked: {stats.claims_checked} "
          f"(found: {stats.claims_found}, not found: {stats.claims_not_found}, "
          f"undetermined: {stats.claims_undetermined})")
    for line in stats.fetch_errors:
        print(f"  fetch error: {line}")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    # STAP 6: no document without a stated source and retrieval time --
    # argparse's required=True already refuses a bare-missing flag, but
    # catches an explicitly-blank one too (--source-url "" would otherwise
    # slip through as "present").
    if not args.source_url or not args.source_url.strip():
        print("--source-url is required and cannot be empty -- ingest refuses a document with no stated source")
        return 1
    try:
        retrieved_at = _parse_retrieved_at(args.retrieved_at)
    except ValueError as exc:
        print(str(exc))
        return 1

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
        sha256=sha256, uploaded_by=None, source_url=args.source_url, retrieved_at=retrieved_at,
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


def cmd_ingest_dir(args: argparse.Namespace) -> int:
    """Batch-upload every PDF in a directory (STAP 7). No per-file
    --source-url/--retrieved-at flags exist on this command -- with
    potentially dozens of files, that isn't a CLI-flag-shaped problem.
    Instead the directory must contain a sources.json manifest, keyed by
    filename, with the same two mandatory fields STAP 6 requires for a
    single upload (plus optional "kind"/"title" per file). This is a
    design choice, not something the brief specified -- flagged here and
    in the response to the person running this.

    No product is linked (there's no --product-slug here either, and one
    file rarely maps to exactly one product for a manufacturer's whole
    catalog) -- extraction is intentionally not run; documents just land
    with correct, mandatory provenance, ready to be linked and extracted
    later once a real product/category exists for them.
    """
    config = load_config()
    conn = db.connect(config.database_url)
    client = storage.make_client(config.supabase_url, config.supabase_service_role_key)

    brand = db.get_brand_by_slug(conn, args.brand)
    if brand is None:
        print(f"no brand with slug '{args.brand}' -- create it in the portal/dashboard first")
        return 1

    directory = pathlib.Path(args.dir)
    if not directory.is_dir():
        print(f"'{args.dir}' is not a directory")
        return 1

    manifest_path = directory / "sources.json"
    if not manifest_path.exists():
        print(f"no sources.json in '{args.dir}' -- every file needs a source_url and retrieved_at "
              f"entry there (same two fields --source-url/--retrieved-at require for a single upload), "
              f"e.g.:\n"
              f'  {{"datasheet.pdf": {{"source_url": "https://...", "retrieved_at": "2026-08-09", '
              f'"kind": "datasheet"}}}}')
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"sources.json is not valid JSON: {exc}")
        return 1

    pdf_paths = sorted(directory.glob("*.pdf"))
    if not pdf_paths:
        print(f"no PDFs found in '{args.dir}'")
        return 0

    processed: list[str] = []
    rejected: list[str] = []

    for path in pdf_paths:
        entry = manifest.get(path.name)
        if entry is None:
            rejected.append(f"{path.name}: no entry in sources.json")
            continue

        source_url = (entry.get("source_url") or "").strip()
        if not source_url:
            rejected.append(f"{path.name}: sources.json entry has no source_url")
            continue

        retrieved_at_raw = entry.get("retrieved_at")
        if not retrieved_at_raw:
            rejected.append(f"{path.name}: sources.json entry has no retrieved_at")
            continue
        try:
            retrieved_at = _parse_retrieved_at(retrieved_at_raw)
        except ValueError as exc:
            rejected.append(f"{path.name}: {exc}")
            continue

        kind = entry.get("kind", "datasheet")
        if kind not in _DOCUMENT_KINDS:
            rejected.append(f"{path.name}: kind {kind!r} in sources.json isn't one of {_DOCUMENT_KINDS}")
            continue

        data = path.read_bytes()
        sha256 = storage.sha256_bytes(data)

        existing_version = db.find_version_by_sha256(conn, sha256)
        if existing_version:
            processed.append(f"{path.name}: already uploaded as document_version "
                              f"{existing_version['id']} -- skipped, not re-uploaded")
            continue

        document_id = db.create_document(
            conn, organization_id=brand["organization_id"], product_id=None,
            title=entry.get("title") or path.stem, kind=kind,
        )
        path_in_bucket = storage.storage_path(args.brand, sha256, ext=path.suffix.lstrip(".") or "pdf")
        storage.upload(client, path_in_bucket, data)
        document_version_id = db.create_document_version(
            conn, document_id=document_id, storage_path=path_in_bucket,
            original_filename=path.name, mime_type="application/pdf", byte_size=len(data),
            sha256=sha256, uploaded_by=None, source_url=source_url, retrieved_at=retrieved_at,
        )
        conn.commit()
        processed.append(f"{path.name}: uploaded as document_version {document_version_id} "
                          f"(document {document_id}, kind={kind}, no product linked -- extraction not run)")

    conn.close()

    print(f"--- {len(processed)}/{len(pdf_paths)} processed ---")
    for line in processed:
        print(f"  OK    {line}")
    print(f"--- {len(rejected)}/{len(pdf_paths)} rejected ---")
    for line in rejected:
        print(f"  SKIP  {line}")

    return 1 if rejected else 0


def _recover_connection(conn, config):
    """Roll back if the connection is still alive; if it isn't (a dead
    pooler connection makes rollback() itself raise), close it and open a
    fresh one instead of letting a long batch job crash on one page."""
    try:
        conn.rollback()
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return db.connect(config.database_url)


def cmd_ingest_website(args: argparse.Namespace) -> int:
    """Crawl a brand's own live website (same-domain, same-path-prefix
    only -- see webpage.crawl) and run it through the exact same
    extraction/citation pipeline as an uploaded PDF (webpage.py's module
    docstring explains why this is genuinely the same standard, not a
    second weaker one). --dry-run crawls (fetching pages is unavoidable
    for link discovery) but prints the discovered page count/URLs without
    writing anything to the DB or spending any LLM credits -- always run
    this first, same convention as `benchmark --dry-run`."""
    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand_slug)
    if brand is None:
        print(f"no brand with slug '{args.brand_slug}' -- create it in the portal/dashboard first")
        return 1

    fetcher = make_httpx_fetcher(timeout=20.0)
    print(f"crawling {args.url} (max {args.max_pages} pages)...")
    pages = webpage.crawl(args.url, fetcher=fetcher, max_pages=args.max_pages)
    print(f"discovered {len(pages)} page(s)")

    if args.dry_run:
        for p in pages:
            print(f"  {p.url}")
        conn.close()
        return 0

    client = storage.make_client(config.supabase_url, config.supabase_service_role_key)
    org_slug = args.brand_slug
    allowed_prefix = webpage.compute_allowed_prefix(args.url)

    uploaded: list[str] = []
    skipped: list[str] = []
    extraction_results: list[dict] = []

    for crawled in pages:
        if not webpage.is_product_page(crawled.url, allowed_prefix):
            skipped.append(f"{crawled.url}: not a product-bearing page (path not in "
                            f"{sorted(webpage.PRODUCT_PATH_SEGMENTS)}) -- skipped")
            continue

        parsed = webpage.parse_webpage(crawled.html, crawled.url)
        if len(parsed.text) < webpage.MIN_PAGE_TEXT_CHARS:
            skipped.append(f"{crawled.url}: too little text ({len(parsed.text)} chars) -- skipped")
            continue

        html_bytes = crawled.html.encode("utf-8")
        sha256 = storage.sha256_bytes(html_bytes)
        existing_version = db.find_version_by_sha256(conn, sha256)
        if existing_version:
            skipped.append(f"{crawled.url}: unchanged since last crawl (document_version "
                            f"{existing_version['id']}) -- not re-ingested")
            continue

        # One page's failure -- a storage hiccup, a transient network error,
        # a bad extraction response -- must never kill the other 100+
        # pages already queued. Every step from here on is one unit: on
        # any exception, roll back whatever this page half-committed and
        # move on, same resilience precedent as the fix in commit eae26e4
        # (run_visibility/run_claims_extraction: a crash mid-run must not
        # discard already-paid-for work on EARLIER pages). Observed for
        # real ingesting ~240 pages: a run long enough to make dozens of
        # Claude calls interspersed with commits can outlive the Supabase
        # pooler's own connection lifetime even with TCP keepalives
        # (db.connect() already sets those) -- conn.rollback() itself then
        # raises "the connection is lost". _recover_connection reconnects
        # in that case instead of letting the whole batch die on one page.
        try:
            title = webpage.extract_title(crawled.html) or crawled.url
            document_id = db.create_document(
                conn, organization_id=brand["organization_id"], product_id=None, title=title, kind="webpage",
            )
            path_in_bucket = storage.storage_path(org_slug, sha256, ext="html")
            storage.upload(client, path_in_bucket, html_bytes, mime_type="text/html")
            document_version_id = db.create_document_version(
                conn, document_id=document_id, storage_path=path_in_bucket,
                original_filename=None, mime_type="text/html", byte_size=len(html_bytes),
                sha256=sha256, uploaded_by=None, source_url=crawled.url,
                retrieved_at=datetime.datetime.now(datetime.timezone.utc),
            )
            conn.commit()
            uploaded.append(f"{crawled.url}: document_version {document_version_id}")
        except Exception as exc:
            conn = _recover_connection(conn, config)
            skipped.append(f"{crawled.url}: upload/document creation failed -- {exc}")
            continue

        try:
            result = pipeline.run_webpage(
                conn, config, document_version_id=document_version_id,
                brand_id=brand["id"], brand_name=brand["name"],
            )
            conn.commit()
            extraction_results.append({"url": crawled.url, **result})
        except Exception as exc:
            conn = _recover_connection(conn, config)
            skipped.append(f"{crawled.url}: extraction failed -- {exc}")

    conn.close()

    print(f"--- {len(uploaded)} ingested, {len(skipped)} skipped ---")
    for line in skipped:
        print(f"  SKIP  {line}")
    total_claims = sum(r["claims_inserted"] for r in extraction_results)
    total_products = sum(r["products"] for r in extraction_results)
    all_dupes = [d for r in extraction_results for d in r.get("possible_duplicate_products", [])]
    print(f"total claims inserted: {total_claims} across {total_products} product-touches, "
          f"{len(extraction_results)} page(s) extracted")
    if all_dupes:
        print(f"possible duplicate products flagged ({len(all_dupes)}) -- review before trusting the catalog:")
        for d in all_dupes:
            print(f"  {d['new_name']!r} ~ existing {d['existing_name']!r} (similarity {d['similarity']})")
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


def _make_visibility_caller(engine: str, config) -> tuple[visibility.VisibilityCaller, str]:
    """Returns (caller, requested_model) for `engine`, or raises RuntimeError
    if that engine's API key isn't configured."""
    if engine == "openai":
        if not config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        return visibility.make_openai_caller(config.openai_api_key, config.visibility_openai_model), \
            config.visibility_openai_model
    if engine == "anthropic":
        if not config.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        return visibility.make_anthropic_caller(config.anthropic_api_key, config.visibility_anthropic_model), \
            config.visibility_anthropic_model
    if engine == "gemini":
        if not config.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set")
        return visibility.make_gemini_caller(config.google_api_key, config.visibility_gemini_model), \
            config.visibility_gemini_model
    raise ValueError(f"unknown engine {engine!r}")


def cmd_benchmark(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand)
    if brand is None:
        print(f"no brand with slug '{args.brand}'")
        return 1

    category = db.get_category_by_code(conn, args.category)
    if category is None:
        print(f"no category with code '{args.category}'")
        return 1

    if args.engine:
        engines = args.engine
    else:
        engines = [e for e in ["openai", "anthropic", "gemini"] if e not in config.visibility_disabled_engines]
        skipped = [e for e in ["openai", "anthropic", "gemini"] if e in config.visibility_disabled_engines]
        if skipped:
            print(f"skipping {', '.join(skipped)} (VISIBILITY_DISABLED_ENGINES) -- "
                  f"pass --engine {skipped[0]} explicitly to run it anyway")

    if args.resume_run and len(engines) != 1:
        print("--resume-run requires exactly one --engine (a run is locked to a single engine)")
        return 1

    if args.dry_run:
        total_cost = 0.0
        for engine in engines:
            plan = visibility.plan_run(
                conn, category_id=category["id"], engine=engine, replicates=args.replicates,
                resume_run_id=args.resume_run,
            )
            total_cost += plan.estimated_cost_usd
            print(f"{engine}: {plan.prompt_count} active prompts x {plan.replicates} replicates "
                  f"- {plan.already_answered} already answered = {plan.planned_answers} planned answers, "
                  f"~${plan.estimated_cost_usd:.2f} (rough estimate)")
        print(f"total estimated cost across {len(engines)} engine(s): ~${total_cost:.2f} -- "
              f"nothing was called, this is a projection only")
        conn.close()
        return 0

    exit_code = 0
    for engine in engines:
        try:
            caller, requested_model = _make_visibility_caller(engine, config)
        except RuntimeError as exc:
            print(f"skipping {engine}: {exc}")
            exit_code = 1
            continue

        print(f"running {engine} ({requested_model}) x{args.replicates} replicates for {brand['name']}...")
        try:
            run_id, stats = visibility.run_visibility(
                conn, organization_id=brand["organization_id"], brand_id=brand["id"],
                category_id=category["id"], engine=engine, requested_model=requested_model,
                caller=caller, replicates=args.replicates,
                max_answers_per_run=config.visibility_max_answers_per_run,
                cost_ceiling_usd=config.visibility_cost_ceiling_usd,
                resume_run_id=args.resume_run if len(engines) == 1 else None,
                commit_each_answer=True,
            )
        except Exception as exc:
            # run_visibility already marked the run 'failed' and committed
            # that itself (commit_each_answer=True) before re-raising --
            # report and move on to the next engine instead of crashing the
            # whole multi-engine loop over one engine's failure.
            print(f"  {engine} run failed: {exc}")
            exit_code = 1
            continue
        conn.commit()
        print(f"  run {run_id}: {stats.answers_succeeded} succeeded, {stats.answers_failed} failed "
              f"this invocation, ~${stats.cost_usd_this_invocation:.2f} spent this invocation")

    conn.close()
    return exit_code


def cmd_claims_extract(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    if not config.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set")
        return 1

    caller = claims_diff.make_anthropic_extractor(Anthropic(api_key=config.anthropic_api_key), config.anthropic_model)

    try:
        stats = claims_diff.run_claims_extraction(
            conn, run_id=args.run_id, caller=caller, commit_each_answer=True,
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        print(str(exc))
        return 1
    except Exception:
        conn.rollback()
        raise
    conn.close()

    print(f"run {args.run_id}: {stats.answers_processed} answer(s) processed "
          f"({stats.answers_skipped_already_done} already done), {stats.claims_extracted} claim(s) extracted "
          f"({stats.claims_rejected_unverifiable_quote} rejected: unverifiable quote, "
          f"{stats.claims_rejected_unknown_spec} rejected: unknown spec key)")
    for verdict, count in sorted(stats.verdict_counts.items()):
        print(f"  {verdict}: {count}")
    return 0


def _format_rate(value) -> str:
    return f"{value:.0%}" if value is not None else "no data"


def cmd_score(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    try:
        card = scoring.compute_scorecard(conn, run_id=args.run_id)
    except ValueError as exc:
        conn.close()
        print(str(exc))
        return 1
    conn.close()

    print(f"scorecard for run {card.run_id} ({card.brand_name}):")
    if not card.claims_extracted_for_run:
        print("  WARNING: no answer_claims found for this run -- share_of_voice and spec_accuracy "
              "will show 'no data' until `claims extract --run <id>` has been run")

    print(f"  presence_rate:    overall {_format_rate(card.presence_rate.overall)}")
    for intent, value in sorted(card.presence_rate.per_intent.items()):
        print(f"                    {intent}: {_format_rate(value)}")

    print(f"  share_of_voice:   overall {_format_rate(card.share_of_voice.overall)}")
    for intent, value in sorted(card.share_of_voice.per_intent.items()):
        print(f"                    {intent}: {_format_rate(value)}")

    print(f"  spec_accuracy:    overall {_format_rate(card.spec_accuracy.overall)}")
    for intent, value in sorted(card.spec_accuracy.per_intent.items()):
        print(f"                    {intent}: {_format_rate(value)}")

    print(f"  data_completeness: {_format_rate(card.data_completeness)} (brand-level, not run/intent-scoped)")
    return 0


def _run_brand_mismatch_error(run: Optional[dict], brand: dict, run_id: str, brand_slug: str) -> Optional[str]:
    """Pure so it's testable without a live DB connection. Returns an
    error message when `run_id` doesn't belong to `brand_slug`'s brand,
    None when it's fine to proceed.

    generate_pdf_report derives brand_name/scorecard entirely from the
    run's OWN brand_id, never from --brand -- without this check,
    `report --brand a --run <b's run>` would silently produce a PDF full
    of brand b's data, saved under and reported as brand a's filename."""
    if run is None:
        return f"visibility_runs {run_id} not found"
    if str(run["brand_id"]) != str(brand["id"]):
        return (f"run {run_id} belongs to a different brand (brand_id {run['brand_id']}) "
                f"than '{brand_slug}' (brand_id {brand['id']}) -- refusing to generate a mislabelled report")
    return None


def cmd_report(args: argparse.Namespace) -> int:
    if args.format != "pdf":
        print(f"--format {args.format!r} not supported yet -- only 'pdf'")
        return 1

    config = load_config()
    conn = db.connect(config.database_url)

    brand = db.get_brand_by_slug(conn, args.brand)
    if brand is None:
        print(f"no brand with slug '{args.brand}'")
        conn.close()
        return 1

    run = db.get_visibility_run(conn, args.run_id)
    mismatch = _run_brand_mismatch_error(run, brand, args.run_id, args.brand)
    if mismatch:
        print(mismatch)
        conn.close()
        return 1

    output_path = args.output or f"{args.brand}-{args.run_id}-visibility-audit.pdf"

    try:
        data = pdf_report.generate_pdf_report(conn, run_id=args.run_id, output_path=output_path)
    except ValueError as exc:
        conn.close()
        print(str(exc))
        return 1
    conn.close()

    print(f"report written to {output_path}")
    print(f"  worst findings: {len(data.worst_findings)}, documentation gaps: {len(data.documentation_gaps)}, "
          f"page-visibility gaps: {len(data.page_visibility_gaps)}, remediation items: {len(data.remediation)}")
    if not data.crawlability_check:
        print("  NOTE: no crawlability check on file for this brand -- run `crawlability --brand ...` first")
    if not data.scorecard.claims_extracted_for_run:
        print("  NOTE: no answer_claims for this run -- run `claims extract --run ...` first for real numbers")
    return 0


def cmd_review_list(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)
    entries = db.list_review_queue(conn, status=args.status)
    conn.close()

    if not entries:
        scope = f" with status '{args.status}'" if args.status else ""
        print(f"review queue empty{scope}")
        return 0

    for e in entries:
        snippet = e["source_snippet"]
        preview = snippet if len(snippet) <= 80 else snippet[:77] + "..."
        print(f"{e['id']}  [{e['status']}]  {e['found_term']!r}  "
              f"doc_version={e['document_version_id']} page={e['page_number']}  {preview!r}")
    return 0


def cmd_review_promote(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    entry = db.get_review_queue_entry(conn, args.id)
    if entry is None:
        print(f"no review_queue entry with id '{args.id}'")
        return 1
    if entry["status"] != "new":
        print(f"entry {args.id} is already '{entry['status']}' -- not promoting")
        return 1

    spec = db.get_spec_attribute_by_key(conn, args.key)
    if spec is None:
        print(f"no spec_attributes key '{args.key}' -- add it to seed.sql first, same as any other new spec")
        return 1

    db.promote_review_queue_entry(conn, args.id, args.key)
    conn.commit()
    conn.close()
    print(f"promoted {args.id} -> {args.key}")
    return 0


def cmd_review_reject(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.database_url)

    entry = db.get_review_queue_entry(conn, args.id)
    if entry is None:
        print(f"no review_queue entry with id '{args.id}'")
        return 1
    if entry["status"] != "new":
        print(f"entry {args.id} is already '{entry['status']}' -- not rejecting")
        return 1

    db.reject_review_queue_entry(conn, args.id)
    conn.commit()
    conn.close()
    print(f"rejected {args.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    p_product = sub.add_parser("create-product", help="create a product under an existing brand")
    p_product.add_argument("--brand-slug", required=True)
    p_product.add_argument("--category-code", default=None)
    p_product.add_argument("--name", required=True)
    p_product.add_argument("--manufacturer-ref", default=None)
    p_product.add_argument("--product-url", default=None,
                            help="public product page URL -- needed later for `page-visibility` (Part 5)")
    p_product.set_defaults(func=cmd_create_product)

    p_update_product = sub.add_parser("update-product", help="set/update an existing product's public URL")
    p_update_product.add_argument("--product-slug", required=True)
    p_update_product.add_argument("--product-url", required=True)
    p_update_product.set_defaults(func=cmd_update_product)

    p_update_brand = sub.add_parser("update-brand", help="set/update an existing brand's website")
    p_update_brand.add_argument("--brand-slug", required=True)
    p_update_brand.add_argument("--website", required=True)
    p_update_brand.set_defaults(func=cmd_update_brand)

    p_upload = sub.add_parser("upload", help="upload a document version and run extraction")
    p_upload.add_argument("file", help="path to a PDF on disk")
    p_upload.add_argument("--org-slug", required=True)
    p_upload.add_argument("--product-slug", default=None, help="omit only for a brand-new document")
    p_upload.add_argument("--document-id", default=None, help="add a new version to this existing document")
    p_upload.add_argument("--kind", default="datasheet", choices=_DOCUMENT_KINDS)
    p_upload.add_argument("--title", default=None)
    p_upload.add_argument("--source-url", required=True,
                           help="where this file was fetched from -- mandatory, no anonymous documents")
    p_upload.add_argument("--retrieved-at", required=True,
                           help="ISO 8601 datetime this file was actually fetched, e.g. 2026-08-09")
    p_upload.set_defaults(func=cmd_upload)

    p_ingest_dir = sub.add_parser("ingest", help="batch-upload every PDF in a directory (STAP 7)")
    p_ingest_dir.add_argument("--brand", required=True, help="brand slug; its organization owns the documents")
    p_ingest_dir.add_argument("--dir", required=True, help="directory of PDFs, must contain sources.json")
    p_ingest_dir.set_defaults(func=cmd_ingest_dir)

    p_ingest_website = sub.add_parser(
        "ingest-website",
        help="crawl a brand's own live website and run it through the same extraction pipeline as a PDF",
    )
    p_ingest_website.add_argument("--brand-slug", required=True)
    p_ingest_website.add_argument("--url", required=True,
                                   help="start URL; only links under this same scheme+host+path-prefix are followed")
    p_ingest_website.add_argument("--max-pages", type=int, default=300)
    p_ingest_website.add_argument(
        "--dry-run", action="store_true",
        help="crawl and print discovered page URLs only -- no document/claim writes, no LLM cost "
             "(the crawl itself does fetch each page, since link discovery requires it)",
    )
    p_ingest_website.set_defaults(func=cmd_ingest_website)

    p_report = sub.add_parser("generate-report", help="generate a data-coverage report for a brand")
    p_report.add_argument("--brand-slug", required=True)
    p_report.add_argument("--kind", default="audit", choices=["audit", "monthly"])
    p_report.set_defaults(func=cmd_generate_report)

    p_benchmark = sub.add_parser("run-benchmark", help="run the prompt library against ChatGPT or Gemini")
    p_benchmark.add_argument("--brand-slug", required=True)
    p_benchmark.add_argument("--model", required=True, choices=["chatgpt", "gemini"])
    p_benchmark.set_defaults(func=cmd_run_benchmark)

    p_benchmark_v2 = sub.add_parser(
        "benchmark",
        help="MVP brief Part 3: run `prompts` through OpenAI/Anthropic/Gemini with real web search/grounding",
    )
    p_benchmark_v2.add_argument("--brand", required=True, help="brand slug")
    p_benchmark_v2.add_argument("--category", required=True, help="product_categories.code")
    p_benchmark_v2.add_argument(
        "--engine", action="append", choices=["openai", "anthropic", "gemini"],
        help="repeatable, e.g. --engine openai --engine gemini; omit to run all three",
    )
    p_benchmark_v2.add_argument("--replicates", type=int, default=3)
    p_benchmark_v2.add_argument(
        "--dry-run", action="store_true", help="print planned answer count/cost, execute nothing"
    )
    p_benchmark_v2.add_argument(
        "--resume-run", default=None,
        help="continue an existing run_id (requires exactly one --engine, a run is engine-locked)",
    )
    p_benchmark_v2.set_defaults(func=cmd_benchmark)

    p_claims = sub.add_parser(
        "claims", help="MVP brief Part 4: extract + diff technical claims from a visibility run's answers",
    )
    claims_sub = p_claims.add_subparsers(dest="claims_command", required=True)

    p_claims_extract = claims_sub.add_parser("extract", help="extract and diff claims for one visibility_runs id")
    p_claims_extract.add_argument("--run", required=True, dest="run_id")
    p_claims_extract.set_defaults(func=cmd_claims_extract)

    p_crawlability = sub.add_parser(
        "crawlability", help="MVP brief Part 5a: check robots.txt precedence for every test AI-crawler agent",
    )
    p_crawlability.add_argument("--brand", required=True, help="brand slug -- uses brands.website")
    p_crawlability.set_defaults(func=cmd_crawlability)

    p_page_visibility = sub.add_parser(
        "page-visibility",
        help="MVP brief Part 5b: for each product with ground truth, check whether its verified spec "
             "values appear as literal text on its public product page",
    )
    p_page_visibility.add_argument("--brand", required=True, help="brand slug")
    p_page_visibility.set_defaults(func=cmd_page_visibility)

    p_score = sub.add_parser(
        "score",
        help="MVP brief Part 6: the four independent scores (presence rate, share of voice, "
             "spec accuracy, data completeness), overall and per prompt-intent, for one run",
    )
    p_score.add_argument("--run", required=True, dest="run_id")
    p_score.set_defaults(func=cmd_score)

    p_report_v2 = sub.add_parser(
        "report",
        help="MVP brief Part 7: full PDF visibility-audit report for one run (method, scores, worst "
             "findings, competitive picture, documentation/page-visibility gaps, remediation)",
    )
    p_report_v2.add_argument("--brand", required=True, help="brand slug")
    p_report_v2.add_argument("--run", required=True, dest="run_id")
    p_report_v2.add_argument("--format", default="pdf", choices=["pdf"])
    p_report_v2.add_argument("--output", default=None, help="output path; default <brand>-<run>-visibility-audit.pdf")
    p_report_v2.set_defaults(func=cmd_report)

    p_review = sub.add_parser("review", help="triage found-but-undefined specs")
    review_sub = p_review.add_subparsers(dest="review_command", required=True)

    p_review_list = review_sub.add_parser("list", help="list review queue entries")
    p_review_list.add_argument("--status", default=None, choices=["new", "promoted", "rejected"],
                                help="omit to list all statuses")
    p_review_list.set_defaults(func=cmd_review_list)

    p_review_promote = review_sub.add_parser(
        "promote", help="mark an entry as promoted to a real spec_attributes key"
    )
    p_review_promote.add_argument("id")
    p_review_promote.add_argument("--key", required=True, help="must already exist in spec_attributes")
    p_review_promote.set_defaults(func=cmd_review_promote)

    p_review_reject = review_sub.add_parser("reject", help="reject an entry")
    p_review_reject.add_argument("id")
    p_review_reject.set_defaults(func=cmd_review_reject)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
