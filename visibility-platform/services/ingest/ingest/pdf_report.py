"""Part 7: PDF report -- ties Parts 3-6 together into the actual sellable
deliverable.

Renders directly via reportlab's Platypus layout engine, NOT HTML ->
WeasyPrint as the brief's first choice describes. WeasyPrint was tried on
this machine and confirmed not to work: `pip install weasyprint` succeeds
(it now ships as pure-Python wheels), but `from weasyprint import HTML`
fails at import with a missing `libgobject-2.0-0` DLL -- WeasyPrint still
needs the GTK3/Pango/cairo native runtime at USE time, which plain pip
cannot install on Windows. The brief explicitly allows a "headless-
browser fallback ... if CSS blocks"; reportlab is the pragmatic
equivalent here rather than a browser -- already a proven dependency in
this exact venv (it generates the synthetic PDFs test_parsing.py feeds
into the parser), pure-Python/wheel-only install, zero native-runtime
risk. Section CONTENT and ORDER match the brief exactly; only the
rendering technology differs, and that's flagged here rather than
silently swapped.

Section order (brief is explicit this matters -- "reproducibility is the
product"):
  1. Method
  2. The four numbers, overall + per prompt-intent
  3. Worst findings (<=10)
  4. Competitive picture per intent
  5. Documentation gaps
  6. Crawlability
  7. Page-visibility gaps
  8. Remediation, ordered by impact / effort

This is a NEW deliverable -- a PDF file on disk, path returned/printed by
the CLI -- not routed through the Phase 1/2 reports/report_sections
tables (those serve report.py's own older, narrower coverage-only
report; ground rule is additive-only, not a rewrite of what's proven).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import db
from .scoring import ScoreCard, compute_scorecard


@dataclass
class RemediationItem:
    title: str
    category: str
    effort: int  # 1 (low) .. 3 (high) -- staff judgment heuristic, not derived from cost data
    impact: int  # 1 (low) .. 3 (high)
    detail: str

    @property
    def score(self) -> float:
        return self.impact / self.effort


@dataclass
class ReportData:
    brand_name: str
    run: dict
    scorecard: ScoreCard
    worst_findings: list[dict]
    competitive_picture: list[dict]
    documentation_gaps: list[dict]
    crawlability_check: Optional[dict]
    crawlability_agents: list[dict]
    page_visibility_checks: list[dict]
    page_visibility_gaps: list[dict]
    remediation: list[RemediationItem] = field(default_factory=list)


def _format_value(prefix: str, row: dict) -> str:
    """Render whichever value_* column is populated as a display string.
    `prefix` lets the same helper read either an answer_claims row's own
    columns ('') or a joined ground-truth claim's ('gt_')."""
    numeric = row.get(f"{prefix}value_numeric")
    if numeric is not None:
        text = str(numeric)
        numeric_max = row.get(f"{prefix}value_numeric_max")
        if numeric_max is not None:
            text += f"-{numeric_max}"
        unit = row.get(f"{prefix}unit")
        return f"{text} {unit}" if unit else text
    text_value = row.get(f"{prefix}value_text")
    if text_value is not None:
        return text_value
    bool_value = row.get(f"{prefix}value_bool")
    if bool_value is not None:
        return "yes" if bool_value else "no"
    enum_value = row.get(f"{prefix}value_enum")
    if enum_value is not None:
        return enum_value
    return "-"


def _aggregate_competitive_picture(mentions: list[dict], verdicts: list[dict]) -> list[dict]:
    mention_counts: dict[tuple, dict] = {}
    for row in mentions:
        key = (row["intent"], row["brand_name"].strip().lower())
        entry = mention_counts.setdefault(
            key, {"intent": row["intent"], "brand_name": row["brand_name"],
                  "is_competitor": row["is_competitor"], "mentions": 0},
        )
        entry["mentions"] += 1

    verdict_counts: dict[tuple, dict] = {}
    for row in verdicts:
        key = (row["intent"], row["brand_name"].strip().lower())
        entry = verdict_counts.setdefault(key, {"correct": 0, "incorrect": 0, "likely_confusion": 0, "unverifiable": 0})
        if row["verdict"] == "correct":
            entry["correct"] += 1
        elif row["verdict"] == "incorrect":
            entry["incorrect"] += 1
        elif row["verdict"] == "likely_confusion":
            entry["likely_confusion"] += 1
        else:
            entry["unverifiable"] += 1

    empty = {"correct": 0, "incorrect": 0, "likely_confusion": 0, "unverifiable": 0}
    rows = [{**entry, **verdict_counts.get(key, empty)} for key, entry in mention_counts.items()]
    rows.sort(key=lambda r: (r["intent"], -r["mentions"]))
    return rows


def _build_remediation(
    documentation_gaps: list[dict], page_visibility_checks: list[dict],
    crawlability_agents: list[dict], worst_findings: list[dict],
) -> list[RemediationItem]:
    items: list[RemediationItem] = []

    blocked = sorted(a["agent_name"] for a in crawlability_agents if a["allowed"] is False)
    if blocked:
        items.append(RemediationItem(
            title=f"Unblock {len(blocked)} AI crawler(s) in robots.txt", category="crawlability",
            effort=1, impact=3,
            detail=f"Currently disallowed at the site root: {', '.join(blocked)}. Usually a single-line "
                    f"robots.txt fix; impact is total -- these agents cannot see ANY page while blocked.",
        ))

    gaps_by_spec: dict[str, int] = {}
    for gap in documentation_gaps:
        gaps_by_spec[gap["spec_name_nl"]] = gaps_by_spec.get(gap["spec_name_nl"], 0) + 1
    for spec_name, count in sorted(gaps_by_spec.items(), key=lambda kv: -kv[1]):
        items.append(RemediationItem(
            title=f"Publish {spec_name} in product documentation", category="documentation gap",
            effort=1, impact=min(3, math.ceil(count / 2)),
            detail=f"Referenced in AI answers {count} time(s) with no current ground truth on file to "
                    f"check it against -- likely a real value simply not documented yet.",
        ))

    gaps_by_product: dict[str, int] = {}
    for row in page_visibility_checks:
        if row["value_found_as_text"] is False:
            gaps_by_product[row["product_name"]] = gaps_by_product.get(row["product_name"], 0) + 1
    for product_name, count in sorted(gaps_by_product.items(), key=lambda kv: -kv[1]):
        items.append(RemediationItem(
            title=f"Add verified spec values as page text for {product_name}", category="page visibility",
            effort=2, impact=min(3, math.ceil(count / 2)),
            detail=f"{count} verified spec value(s) exist only in a PDF/table-image on this product's "
                    f"own page, never as text an AI can read directly.",
        ))

    findings_by_spec: dict[str, int] = {}
    for finding in worst_findings:
        findings_by_spec[finding["spec_name_nl"]] = findings_by_spec.get(finding["spec_name_nl"], 0) + 1
    for spec_name, count in sorted(findings_by_spec.items(), key=lambda kv: -kv[1]):
        items.append(RemediationItem(
            title=f"Investigate why AI answers get {spec_name} wrong", category="incorrect answer",
            effort=3, impact=min(3, math.ceil(count / 2)),
            detail=f"{count} answer(s) stated a value contradicting current ground truth, or confusable "
                    f"with a related spec -- no single-step fix; may need clarified phrasing elsewhere "
                    f"online, not just in this documentation.",
        ))

    items.sort(key=lambda item: -item.score)
    return items


def gather_report_data(conn, *, run_id: str) -> ReportData:
    run = db.get_visibility_run(conn, run_id)
    if run is None:
        raise ValueError(f"visibility_runs {run_id} not found")

    scorecard = compute_scorecard(conn, run_id=run_id)
    worst_findings = db.get_worst_findings_for_run(conn, run_id)
    competitive_picture = _aggregate_competitive_picture(
        db.get_brand_mentions_by_intent_for_run(conn, run_id),
        db.get_claim_verdicts_by_brand_for_run(conn, run_id),
    )
    documentation_gaps = db.get_documentation_gaps_for_run(conn, run_id)

    crawlability_check = db.get_latest_crawlability_check(conn, run["brand_id"])
    crawlability_agents = db.get_crawlability_agents(conn, crawlability_check["id"]) if crawlability_check else []

    page_visibility_checks = db.get_latest_page_visibility_checks(conn, run["brand_id"])
    page_visibility_gaps = [c for c in page_visibility_checks if c["value_found_as_text"] is False]

    remediation = _build_remediation(documentation_gaps, page_visibility_checks, crawlability_agents, worst_findings)

    return ReportData(
        brand_name=run["brand_name"], run=run, scorecard=scorecard, worst_findings=worst_findings,
        competitive_picture=competitive_picture, documentation_gaps=documentation_gaps,
        crawlability_check=crawlability_check, crawlability_agents=crawlability_agents,
        page_visibility_checks=page_visibility_checks, page_visibility_gaps=page_visibility_gaps,
        remediation=remediation,
    )


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle(name="H1", parent=ss["Heading1"], spaceBefore=12, spaceAfter=6))
    ss.add(ParagraphStyle(name="H2", parent=ss["Heading2"], spaceBefore=8, spaceAfter=3, fontSize=11))
    ss.add(ParagraphStyle(name="Small", parent=ss["Normal"], fontSize=8, leading=10, textColor=colors.grey))
    ss.add(ParagraphStyle(name="Cell", parent=ss["Normal"], fontSize=8, leading=10))
    return ss


def _cell(value, style) -> Paragraph:
    return Paragraph(str(value) if value not in (None, "") else "-", style)


def _table(rows: list[list], col_widths: list[float]) -> Table:
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b2b2b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    return table


def _fmt_rate(value: Optional[float]) -> str:
    return f"{value:.0%}" if value is not None else "no data"


def render_pdf(data: ReportData, output_path: str) -> None:
    ss = _styles()
    story: list = []
    run = data.run
    sc = data.scorecard

    # 1. Method -- first, not an appendix.
    story.append(Paragraph(f"AI Visibility Audit: {data.brand_name}", ss["Title"]))
    story.append(Paragraph("Method", ss["H1"]))
    story.append(Paragraph(
        f"This report covers one benchmark run against <b>{run['engine']}</b> "
        f"(requested model: {run['requested_model']}), {run['replicates']} replicate(s) per prompt, "
        f"started {run['started_at']}. Every technical claim an AI answer makes is extracted and compared "
        f"against verified ground truth: a fact stated in a manufacturer document with a page number and a "
        f"verbatim quote. A claim with no ground truth to compare against -- no row at all, or the document "
        f"is explicitly silent -- is never counted as an error; it is reported separately as a documentation "
        f"gap.", ss["Normal"],
    ))
    story.append(Paragraph(
        "Four numbers are reported below, deliberately not combined into one score: presence rate (does the "
        "brand's name appear in the raw answer at all), share of voice (this brand's share of all distinct "
        "brand mentions carrying a checkable spec claim), spec accuracy (of claims that could be checked, "
        "the fraction that were correct), and data completeness (fraction of applicable specs backed by a "
        "citation in the brand's own documentation -- independent of any AI answer).", ss["Normal"],
    ))
    if not sc.claims_extracted_for_run:
        story.append(Paragraph(
            "NOTE: claim extraction has not been run for this run yet -- share of voice and spec accuracy "
            "below show “no data”, not a real zero.", ss["Small"],
        ))

    # 2. The four numbers, overall + per intent.
    story.append(Paragraph("Scores", ss["H1"]))
    story.append(_table(
        [["Metric", "Overall"],
         ["Presence rate", _fmt_rate(sc.presence_rate.overall)],
         ["Share of voice", _fmt_rate(sc.share_of_voice.overall)],
         ["Spec accuracy", _fmt_rate(sc.spec_accuracy.overall)],
         ["Data completeness (brand-level, not run-scoped)", _fmt_rate(sc.data_completeness)]],
        col_widths=[110 * mm, 50 * mm],
    ))

    intents = sorted(set(sc.presence_rate.per_intent) | set(sc.share_of_voice.per_intent)
                      | set(sc.spec_accuracy.per_intent))
    if intents:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Per prompt-intent", ss["H2"]))
        rows = [["Intent", "Presence", "Share of voice", "Spec accuracy"]]
        for intent in intents:
            rows.append([intent, _fmt_rate(sc.presence_rate.per_intent.get(intent)),
                         _fmt_rate(sc.share_of_voice.per_intent.get(intent)),
                         _fmt_rate(sc.spec_accuracy.per_intent.get(intent))])
        story.append(_table(rows, col_widths=[50 * mm, 35 * mm, 40 * mm, 35 * mm]))

    story.append(PageBreak())

    # 3. Worst findings (<=10).
    story.append(Paragraph("Worst findings", ss["H1"]))
    if not data.worst_findings:
        story.append(Paragraph(
            "None -- no incorrect or likely-confused claims found for this brand's own products.", ss["Normal"],
        ))
    for finding in data.worst_findings:
        citations = finding.get("citations") or []
        gt_known = bool(finding.get("gt_document_title"))
        gt_text = (
            f"{_format_value('gt_', finding)} -- {finding['gt_document_title']}, "
            f"p.{finding['gt_page_number']}: “{finding['gt_source_snippet']}”"
            if gt_known else "no ground-truth claim on file for this spec/product"
        )
        block = [
            Paragraph(f"<b>{finding['product_name']} — {finding['spec_name_nl']}</b> ({finding['verdict']})",
                      ss["H2"]),
            Paragraph(f"Prompt ({finding['intent']}): “{finding['prompt_text']}”", ss["Normal"]),
            Paragraph(
                f"AI said: “{finding['source_quote']}” ({finding['engine']}"
                + (f", resolved model {finding['resolved_model_version']}"
                   if finding.get("resolved_model_version") else "") + ")",
                ss["Normal"],
            ),
            Paragraph(f"Ground truth: {gt_text}", ss["Normal"]),
        ]
        if finding["verdict"] == "likely_confusion" and finding.get("confusable_key"):
            block.append(Paragraph(f"Likely confused with: {finding['confusable_key']}", ss["Small"]))
        if citations:
            block.append(Paragraph(f"Browsed source: {citations[0]}", ss["Small"]))
        story.append(KeepTogether(block))
        story.append(Spacer(1, 3 * mm))

    story.append(PageBreak())

    # 4. Competitive picture per intent.
    story.append(Paragraph("Competitive picture per intent", ss["H1"]))
    if not data.competitive_picture:
        story.append(Paragraph(
            "No brand mentions with a checkable spec claim were extracted for this run.", ss["Normal"],
        ))
    else:
        rows = [["Intent", "Brand", "Mentions", "Correct", "Incorrect", "Confused", "Unverifiable"]]
        for row in data.competitive_picture:
            label = row["brand_name"] + (" (competitor)" if row["is_competitor"] else "")
            rows.append([row["intent"], label, str(row["mentions"]), str(row["correct"]),
                         str(row["incorrect"]), str(row["likely_confusion"]), str(row["unverifiable"])])
        story.append(_table(rows, col_widths=[28 * mm, 42 * mm, 18 * mm, 18 * mm, 18 * mm, 18 * mm, 22 * mm]))

    story.append(PageBreak())

    # 5. Documentation gaps.
    story.append(Paragraph("Documentation gaps", ss["H1"]))
    story.append(Paragraph(
        "The AI made a claim about one of these specs, but your own documentation has no current, "
        "verifiable value to check it against — not an error, an opportunity.", ss["Normal"],
    ))
    if not data.documentation_gaps:
        story.append(Paragraph("None found.", ss["Normal"]))
    else:
        rows = [["Product", "Spec", "Intent", "AI said"]]
        for gap in data.documentation_gaps:
            rows.append([_cell(gap["product_name"], ss["Cell"]), _cell(gap["spec_name_nl"], ss["Cell"]),
                         _cell(gap["intent"], ss["Cell"]), _cell(gap["source_quote"], ss["Cell"])])
        story.append(_table(rows, col_widths=[32 * mm, 32 * mm, 28 * mm, 68 * mm]))

    story.append(PageBreak())

    # 6. Crawlability.
    story.append(Paragraph("Crawlability", ss["H1"]))
    if not data.crawlability_check:
        story.append(Paragraph("No crawlability check has been run yet for this brand.", ss["Normal"]))
    else:
        check = data.crawlability_check
        story.append(Paragraph(
            f"robots.txt: {check['robots_txt_url']} (status {check['http_status']}, "
            f"fetched {check['fetched_at']})", ss["Normal"],
        ))
        if check.get("error"):
            story.append(Paragraph(f"Fetch problem: {check['error']}", ss["Small"]))
        rows = [["Agent", "Allowed", "Matched rule"]]
        for agent in data.crawlability_agents:
            allowed = "yes" if agent["allowed"] is True else ("no" if agent["allowed"] is False else "undetermined")
            rows.append([agent["agent_name"], allowed,
                         _cell(agent["matched_rule"] or "(default -- no matching rule)", ss["Cell"])])
        story.append(_table(rows, col_widths=[35 * mm, 25 * mm, 100 * mm]))

    story.append(PageBreak())

    # 7. Page-visibility gaps.
    story.append(Paragraph("Page-visibility gaps", ss["H1"]))
    story.append(Paragraph(
        "For each gap below, a verified spec value exists in your documentation but does not appear as "
        "literal text on the product's own public page — likely trapped in a PDF or a table rendered "
        "as an image, both invisible to an AI that only reads page text.", ss["Normal"],
    ))
    if not data.page_visibility_gaps:
        story.append(Paragraph("None found.", ss["Normal"]))
    else:
        rows = [["Product", "Spec", "Page URL", "schema.org Product?"]]
        for gap in data.page_visibility_gaps:
            rows.append([_cell(gap["product_name"], ss["Cell"]), _cell(gap["spec_name_nl"], ss["Cell"]),
                         _cell(gap["product_url"], ss["Cell"]),
                         "yes" if gap["schema_org_product_present"] else "no"])
        story.append(_table(rows, col_widths=[32 * mm, 32 * mm, 76 * mm, 20 * mm]))

    story.append(PageBreak())

    # 8. Remediation, ordered by impact / effort.
    story.append(Paragraph("Remediation, ordered by impact ÷ effort", ss["H1"]))
    story.append(Paragraph(
        "Effort/impact are staff judgment heuristics (1=low, 3=high), not derived from cost data -- "
        "ordering is a starting point for triage, not a guarantee.", ss["Small"],
    ))
    if not data.remediation:
        story.append(Paragraph("No remediation items identified.", ss["Normal"]))
    else:
        rows = [["Item", "Category", "Effort", "Impact", "Detail"]]
        for item in data.remediation:
            rows.append([_cell(item.title, ss["Cell"]), item.category, str(item.effort), str(item.impact),
                         _cell(item.detail, ss["Cell"])])
        story.append(_table(rows, col_widths=[32 * mm, 22 * mm, 14 * mm, 14 * mm, 78 * mm]))

    doc = SimpleDocTemplate(
        output_path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
    )
    doc.build(story)


def generate_pdf_report(conn, *, run_id: str, output_path: str) -> ReportData:
    data = gather_report_data(conn, run_id=run_id)
    render_pdf(data, output_path)
    return data
