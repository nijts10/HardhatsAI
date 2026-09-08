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
from xml.sax.saxutils import escape as _xml_escape

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


INTENT_GLOSSARY: dict[str, dict[str, str]] = {
    "spec_constrained": {
        "label": "Requirement-driven",
        "definition": "The question states concrete, measurable requirements (values, standards, dimensions) "
                       "and asks for a product that meets them -- the way a specifier searches once they "
                       "already know what they need.",
        "example": "Systeemplafond met αw minimaal 0,90 en brandklasse A2-s1,d0",
    },
    "application_driven": {
        "label": "Application-driven",
        "definition": "The question describes a room or use case (classroom, swimming pool, operating theatre) "
                       "without stating any concrete specs, and asks what would be suitable there.",
        "example": "Welk systeemplafond is geschikt voor een klaslokaal met veel nagalm?",
    },
    "comparative": {
        "label": "Comparative",
        "definition": "The question explicitly compares two or more brands/products with each other, often "
                       "including named competitors.",
        "example": "Rockfon of Ecophon voor een schoolgebouw, wat is het verschil?",
    },
    "compliance": {
        "label": "Regulation-driven",
        "definition": "The question is about legal requirements, building-code or standard obligations, not "
                       "about a specific product.",
        "example": "Welke eisen stelt het Bouwbesluit aan geluidsabsorptie in een klaslokaal?",
    },
    "sustainability": {
        "label": "Sustainability-driven",
        "definition": "The question is about environmental impact: MKI, GWP, recyclability, EPDs, Cradle to "
                       "Cradle certification.",
        "example": "Systeemplafond met de laagste MKI-waarde",
    },
    "brand_direct": {
        "label": "Brand-specific",
        "definition": "The question names the brand itself. This is the only category where \"being found\" "
                       "is trivial (the brand name is already in the question) -- here, spec accuracy is the "
                       "metric that actually matters, not presence rate.",
        "example": "Welke akoestische plafonds levert Hunter Douglas?",
    },
    "problem_driven": {
        "label": "Problem-driven",
        "definition": "The question describes a complaint or failure with an existing ceiling and asks for an "
                       "alternative.",
        "example": "Mijn systeemplafond hangt door bij hoge luchtvochtigheid, welk alternatief?",
    },
}

# One English rationale per prompt, keyed by the prompt's exact (Dutch)
# text as stored in `prompts`. The prompts themselves stay in their
# original language -- they're real source data a client must be able to
# match verbatim against the appendix and the live prompts table, not
# something to translate. Written by hand against the real 50-prompt set
# (migration 0015), not auto-generated from a template, so each one
# actually reflects why that specific question is in the set.
PROMPT_RATIONALES: dict[str, str] = {
    "Akoestisch plafond voor een callcenter met veel werkplekken":
        "Tests whether the AI can turn a busy open-plan use case into an acoustic recommendation without "
        "the user stating any spec numbers.",
    "Akoestisch plafond voor een open kantoortuin met werkplekken":
        "The same use-case pattern applied to open-plan offices, one of the most common real-world "
        "specifier queries.",
    "Plafond voor een serverruimte, brandwerend en onderhoudsvriendelijk":
        "Tests whether the AI can combine two different requirements (fire safety and maintainability) "
        "implied by a single room type.",
    "Plafond voor een zwembad, bestand tegen hoge luchtvochtigheid":
        "Tests a recommendation for a demanding, humid environment where material choice matters most.",
    "Plafondsysteem voor een sporthal, balbestendig en geluidsabsorberend":
        "Tests a niche but real specifier need (impact resistance) alongside acoustics.",
    "Plafondsysteem voor een treinstation, hoog en goed bereikbaar voor onderhoud":
        "Tests a recommendation for large-span public infrastructure, a different product class than "
        "office ceilings.",
    "Systeemplafond voor een operatiekamer, reinigbaar en hygiënisch":
        "Tests a healthcare-grade hygiene requirement, a high-value specification niche.",
    "Systeemplafond voor een parkeergarage, corrosiebestendig":
        "Tests an unglamorous but common application where the wrong material choice fails quickly.",
    "Welk plafond gebruik je in een keuken van een restaurant?":
        "Tests a casual, non-technical phrasing of a hygiene/moisture requirement, closer to how a "
        "non-specialist might actually ask.",
    "Welk plafond past bij een theaterzaal met strenge akoestische eisen?":
        "Tests a high-acoustic-performance application without giving exact numbers.",
    "Welk systeemplafond is geschikt voor een klaslokaal met veel nagalm?":
        "The most common real-world education-sector query, and the brief's own flagship example.",
    "Welk systeemplafond werkt in combinatie met klimaatplafonds?":
        "Tests a technical integration question (compatibility with another building system), not just a "
        "standalone product pick.",
    "Levert Hunter Douglas ook houten plafondsystemen?":
        "Tests whether the AI knows the full breadth of Hunter Douglas's own product range, not just its "
        "best-known line.",
    "Welke akoestische plafonds levert Hunter Douglas?":
        "The most direct possible test: the brand name is already in the question, so any gap here is the "
        "hardest to explain away.",
    "Welke systeemplafonds heeft Rockfon in het assortiment?":
        "A control prompt aimed at a named competitor, included to see whether Hunter Douglas gets "
        "volunteered even when the question is about someone else.",
    "Knauf AMF versus Armstrong, wat zijn de akoestische verschillen?":
        "A competitor-vs-competitor control prompt; tests whether Hunter Douglas is ever volunteered as a "
        "third option even when not named.",
    "Metalen plafond versus minerale wol plafond, voor- en nadelen":
        "Tests a category-level (not brand-level) comparison, the kind of question that precedes a brand "
        "search.",
    "Open of gesloten systeemplafond, wat is akoestisch gunstiger?":
        "Tests a fundamental design decision that determines which product category gets considered at all.",
    "Rockfon of Ecophon voor een schoolgebouw, wat is het verschil?":
        "A named two-competitor comparison; again tests whether Hunter Douglas gets mentioned as an "
        "alternative.",
    "Saint-Gobain Ecophon versus Rockfon, verschil in brandklasse?":
        "The same pattern, narrowed to one spec (fire class), testing depth of comparison rather than just "
        "brand recall.",
    "Verschil tussen Hunter Douglas HeartFelt en een standaard baffleplafond":
        "The one comparative prompt that directly names Hunter Douglas; tests spec accuracy on home turf.",
    "Vilt plafond versus minerale wol, wat scoort beter op duurzaamheid?":
        "Tests a material-category comparison on sustainability, using HeartFelt's own core material.",
    "Wat is beter voor akoestiek: geperforeerd staal of glaswolplaat?":
        "Tests a technical material comparison one level below brand, common early in a specification "
        "process.",
    "Frisse Scholen klasse A, welke plafondeisen horen daarbij?":
        "Tests knowledge of a specific Dutch schools ventilation/acoustics standard, a real requirement in "
        "education projects.",
    "Frisse Scholen klasse B akoestiek, welk plafond haalt dat?":
        "The same standard one class down, testing whether the AI differentiates between compliance tiers.",
    "Is een DoP verplicht voor een systeemplafond onder CPR 305/2011?":
        "Tests regulatory/compliance knowledge (the Construction Products Regulation) rather than a product "
        "recommendation.",
    "Moet een systeemplafond CE-gemarkeerd zijn volgens EN 13964?":
        "Tests knowledge of the specific product standard for suspended ceilings.",
    "Wat zegt het Bouwbesluit over brandwerendheid van plafonds in vluchtwegen?":
        "Tests knowledge of Dutch building-code fire-safety requirements for escape routes, a high-stakes "
        "compliance topic.",
    "Welke brandklasse is vereist voor een plafond in een vluchtroute?":
        "A narrower, spec-level follow-up to the escape-route fire-safety question.",
    "Welke eisen stelt het Bouwbesluit aan geluidsabsorptie in een klaslokaal?":
        "The brief's own flagship compliance example.",
    "Welke norm geldt voor geluidsabsorptie in plafonds, EN ISO 11654?":
        "Tests knowledge of the underlying acoustic testing standard itself, not just a building code.",
    "Mijn systeemplafond hangt door bij hoge luchtvochtigheid, welk alternatief?":
        "The brief's own flagship problem-driven example; tests whether the AI recommends an alternative "
        "brand when a customer already has a failing product.",
    "Nagalm in kantoortuin is te hoog, welk plafond lost dit op?":
        "Tests a retrofit/complaint-driven scenario, common when a completed building underperforms "
        "acoustically.",
    "Akoestisch plafond met Dnf,w van minimaal 40 dB voor kantoorscheiding":
        "Tests matching a precise sound-insulation requirement between rooms, distinct from absorption.",
    "Baffleplafond met absorptieklasse B en gewicht max 3 kg/m²":
        "Tests a two-constraint match (acoustic class and weight) specific to baffle-type ceilings, "
        "HeartFelt's own product type.",
    "Demontabel metalen plafond, module 600x600, gewicht onder 5 kg/m²":
        "Tests matching a standard module size plus a weight ceiling, a very common architect shorthand.",
    "Geperforeerd metalen plafond, perforatiegraad rond 16%, met akoestisch vlies":
        "Tests a precise, product-catalogue-style spec combination.",
    "Gipsplafond met corrosieweerstandsklasse C voor vochtige ruimte":
        "Tests a material plus corrosion-class requirement outside Hunter Douglas's core felt/metal lines.",
    "Houten roosterplafond, open profiel, lichtreflectie minimaal 70%":
        "Tests a wood-ceiling requirement, one of Hunter Douglas's own product lines.",
    "Metalen lamellenplafond met perforatiegraad 20% en Rw 35 dB":
        "Tests the Luxalon Linear Metal product line's own real spec profile.",
    "Plafondsysteem met lichtreflectie boven 85% en absorptieklasse A":
        "Tests the combination of light reflectance and top acoustic class, both premium-tier requirements.",
    "Plafondsysteem met NRC 0,75 en modulemaat 1200x600":
        "Tests the North-American-style NRC metric alongside a specific module size.",
    "Systeemplafond brandklasse B-s2,d0, geschikt tot vochtklasse 90%":
        "Tests a fire class plus humidity tolerance combination.",
    "Systeemplafond met EPD beschikbaar en GWP onder 5 kg CO2-eq/m²":
        "Tests whether the AI can match a strict, numeric sustainability requirement, not just a general "
        "\"sustainable\" claim.",
    "Systeemplafond met αw minimaal 0,90 en brandklasse A2-s1,d0":
        "The brief's own flagship spec-constrained example, combining top-tier acoustics with a strict "
        "fire class.",
    "Demontabel plafond met hoog aandeel gerecycled materiaal":
        "Tests recycled-content knowledge combined with a demountability requirement.",
    "Systeemplafond met de laagste MKI-waarde":
        "Tests the Dutch environmental-cost indicator (MKI), used directly in Dutch tender scoring.",
    "Systeemplafond met hoogste percentage gerecycled materiaal":
        "Tests a superlative sustainability claim, the kind of question that rewards a documented, "
        "citable number.",
    "Welk plafond scoort goed voor BREEAM Excellent?":
        "Tests whether the AI connects a product to a specific building-certification tier.",
    "Welk plafondmerk publiceert een EPD volgens EN 15804?":
        "Tests whether the AI knows which brands have a compliant Environmental Product Declaration, a "
        "frequent tender requirement.",
}


@dataclass
class CriticalNote:
    heading: str
    body: str
    advice: Optional[str] = None


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
    prompts_and_answers: list[dict]
    remediation: list[RemediationItem] = field(default_factory=list)
    critical_notes: list[CriticalNote] = field(default_factory=list)
    problem_statement: str = ""


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
                    f"check it against. Note: an AI already producing an answer here does NOT mean this "
                    f"value is already reliably findable -- it may be inferred from generic category "
                    f"knowledge, a third party, or simply plausible-sounding. Publishing your own cited "
                    f"value gives you (and this audit) something authoritative to verify future AI answers "
                    f"against, and a documented basis to correct the record if an answer is ever wrong.",
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


# Below this data-completeness percentage, "spec accuracy" and "no incorrect
# findings" become unreliable signals of AI accuracy, not because the AI is
# untested but because too little of the brand's own ground truth exists to
# check anything against. A heuristic threshold (not derived from any
# statistical significance calculation), flagged as such wherever it's used.
_LOW_COMPLETENESS_THRESHOLD = 0.25


def _build_critical_notes(
    *, brand_name: str, worst_findings: list[dict], competitive_picture: list[dict],
    scorecard: ScoreCard, documentation_gaps: list[dict], page_visibility_gaps: list[dict],
) -> list[CriticalNote]:
    """Always returns at least one note -- this section is never a bare
    pass/fail line. Two failure modes this exists to prevent: (1) reading
    "no incorrect findings" as "the AI is accurate" when almost nothing
    was actually checkable, and (2) reading a documentation/unverifiable
    gap as "the AI is too vague" when the real cause is almost always that
    OUR OWN ground truth is too thin to check the AI's claim against in
    the first place -- the AI is not at fault for a gap in our own
    documentation. Every note here is derived from this run's real
    numbers, never templated filler."""
    notes: list[CriticalNote] = []

    own_rows = [r for r in competitive_picture if not r["is_competitor"]
                and r["brand_name"].strip().lower() == brand_name.strip().lower()]
    own_mentions = sum(r["mentions"] for r in own_rows)
    own_checkable = sum(r["correct"] + r["incorrect"] + r["likely_confusion"] for r in own_rows)
    own_unverifiable = sum(r["unverifiable"] for r in own_rows)

    if worst_findings:
        for finding in worst_findings:
            verdict_label = "incorrect claim" if finding["verdict"] == "incorrect" else "likely confused claim"
            advice = (
                f"Check whether “{finding['spec_name_nl']}” for {finding['product_name']} is "
                f"documented correctly and up to date, and consider whether wording used elsewhere online "
                f"(reviews, distributors, older versions of your own material) could be causing the confusion."
            )
            notes.append(CriticalNote(
                heading=f"{finding['product_name']} — {finding['spec_name_nl']} ({verdict_label})",
                body=f"AI said: “{finding['source_quote']}”. This diverges from your own, cited "
                     f"ground truth.",
                advice=advice,
            ))
    else:
        checkable_note = f"No claim about {brand_name} was flagged as demonstrably incorrect this run."
        if own_mentions:
            checkable_note += (
                f" Important context: of the {own_mentions} times {brand_name} was mentioned, only "
                f"{own_checkable} claim(s) could actually be checked against your own documentation; "
                f"{own_unverifiable} stayed “unverifiable” -- not because the AI said something "
                f"wrong, but because there was simply nothing in your own documentation to check it "
                f"against. This is NOT confirmation that every AI answer is correct, only that no error was "
                f"proven on the small share that could actually be checked."
            )
        notes.append(CriticalNote(
            heading="No incorrect claims found (read the context below)",
            body=checkable_note,
            advice="Our recommendation: fill in the documentation gaps below first, so a follow-up "
                   "measurement can produce a meaningful accuracy figure.",
        ))

    if scorecard.data_completeness is not None and scorecard.data_completeness < _LOW_COMPLETENESS_THRESHOLD:
        notes.append(CriticalNote(
            heading=f"Your own documentation coverage is low ({scorecard.data_completeness:.0%})",
            body="Data completeness measures what share of applicable specs has a cited value in your own "
                 "documentation -- independent of anything an AI says. At low coverage, this audit simply "
                 "has nothing to check most AI answers against, so they fall into “unverifiable” "
                 "instead of correct or incorrect.",
            advice="Our recommendation: treat this report, in its current form, primarily as a "
                   "documentation audit, not an AI-accuracy audit -- the latter only becomes reliable once "
                   "coverage goes up.",
        ))

    if documentation_gaps:
        notes.append(CriticalNote(
            heading=f"{len(documentation_gaps)} documentation gap(s) found",
            body="See the “Documentation gaps” section further on for the full list, prioritised "
                 "in the remediation table.",
        ))

    if page_visibility_gaps:
        notes.append(CriticalNote(
            heading=f"{len(page_visibility_gaps)} page-visibility gap(s) found",
            body="These specs ARE verified in your own documentation, but do not appear as readable text on "
                 "your own product page -- likely trapped in a PDF or a table rendered as an image.",
        ))

    return notes


def _build_problem_statement(*, brand_name: str, competitive_picture: list[dict]) -> str:
    """One sentence, as concrete and unavoidable as this run's real numbers
    allow -- the report's own opening line, not a teaser for later. Always
    derived from the actual competitive_picture data for this run, never a
    canned line: if the numbers don't support a striking comparison, this
    says so plainly instead of manufacturing one."""
    totals: dict[str, dict] = {}
    for row in competitive_picture:
        key = row["brand_name"].strip().lower()
        entry = totals.setdefault(key, {"name": row["brand_name"], "mentions": 0, "is_competitor": row["is_competitor"]})
        entry["mentions"] += row["mentions"]

    own = next((v for v in totals.values() if not v["is_competitor"] and v["name"].strip().lower() == brand_name.strip().lower()), None)
    own_mentions = own["mentions"] if own else 0
    competitors = sorted((v for v in totals.values() if v["is_competitor"]), key=lambda v: -v["mentions"])
    top_competitor = competitors[0] if competitors else None

    if not top_competitor:
        return (
            f"No competitor was mentioned often enough this run to draw a direct comparison -- see the "
            f"Competitive picture section for the full breakdown."
        )
    if own_mentions == 0:
        return (
            f"{top_competitor['name']} was mentioned {top_competitor['mentions']} time(s) by AI answers in "
            f"this audit -- {brand_name} was not mentioned once."
        )
    ratio = top_competitor["mentions"] / own_mentions
    return (
        f"{top_competitor['name']} is mentioned {ratio:.1f}x more often than {brand_name} in this audit's "
        f"AI answers ({top_competitor['mentions']} vs {own_mentions} mentions)."
    )


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
    critical_notes = _build_critical_notes(
        brand_name=run["brand_name"], worst_findings=worst_findings, competitive_picture=competitive_picture,
        scorecard=scorecard, documentation_gaps=documentation_gaps, page_visibility_gaps=page_visibility_gaps,
    )
    prompts_and_answers = db.get_prompts_and_answers_for_run(conn, run_id)
    problem_statement = _build_problem_statement(brand_name=run["brand_name"], competitive_picture=competitive_picture)

    return ReportData(
        brand_name=run["brand_name"], run=run, scorecard=scorecard, worst_findings=worst_findings,
        competitive_picture=competitive_picture, documentation_gaps=documentation_gaps,
        crawlability_check=crawlability_check, crawlability_agents=crawlability_agents,
        page_visibility_checks=page_visibility_checks, page_visibility_gaps=page_visibility_gaps,
        prompts_and_answers=prompts_and_answers, remediation=remediation, critical_notes=critical_notes,
        problem_statement=problem_statement,
    )


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle(name="H1", parent=ss["Heading1"], spaceBefore=12, spaceAfter=6))
    ss.add(ParagraphStyle(name="H2", parent=ss["Heading2"], spaceBefore=8, spaceAfter=3, fontSize=11))
    ss.add(ParagraphStyle(name="Small", parent=ss["Normal"], fontSize=8, leading=10, textColor=colors.grey))
    ss.add(ParagraphStyle(name="Cell", parent=ss["Normal"], fontSize=8, leading=10))
    return ss


def _cell(value, style) -> Paragraph:
    # XML-escape: reportlab's Paragraph parses a small XML/HTML-like markup
    # language, so unescaped freeform text (product names, AI-extracted
    # quotes, competitor names) containing '<', '>' or '&' is silently
    # mangled or dropped rather than shown literally -- e.g. a literal
    # brand name "<UNKNOWN>" (a real value claims_diff.py can emit for an
    # unresolved competitor) disappears entirely instead of printing.
    text = str(value) if value not in (None, "") else "-"
    return Paragraph(_xml_escape(text), style)


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
    n_answers = len(data.prompts_and_answers)
    n_prompts = len(set(r["prompt_id"] for r in data.prompts_and_answers))

    story.append(Paragraph(f"AI Visibility Audit: {data.brand_name}", ss["Title"]))
    story.append(Paragraph(
        f"Benchmark run against <b>{run['engine']}</b> (requested model: {run['requested_model']}), "
        f"{run['replicates']} repeat(s) per question, started {run['started_at']}.", ss["Small"],
    ))
    story.append(Spacer(1, 4 * mm))

    # 1. The problem -- one sentence, as concrete as the real numbers allow.
    story.append(Paragraph("1. The problem", ss["H1"]))
    story.append(Paragraph(f"<b>{_xml_escape(data.problem_statement)}</b>", ss["Normal"]))
    story.append(Spacer(1, 4 * mm))

    # 2. Introduction.
    story.append(Paragraph("2. Introduction", ss["H1"]))
    story.append(Paragraph(
        f"The sentence above is one of many findings in this research. This research was carried out to "
        f"investigate how well AI systems can find {data.brand_name}'s products, and can therefore cite "
        f"{data.brand_name} correctly as a supplier when someone asks. The rest of this report explains how "
        f"we tested that, what we found, and what to do about it.", ss["Normal"],
    ))
    story.append(Spacer(1, 4 * mm))

    # 3. About HardhatsAI.
    story.append(Paragraph("3. About HardhatsAI", ss["H1"]))
    story.append(Paragraph(
        "We are HardhatsAI, and we are construction professionals ourselves. We have experienced first-hand "
        "how hard it is to find the right suppliers and specifications among the thousands of products that "
        "can go into a single building. With that industry knowledge, we want to make a difference and close "
        "the gap between suppliers and architects/engineers by improving how well AI can find and represent "
        "them.", ss["Normal"],
    ))
    story.append(Spacer(1, 4 * mm))

    # 4. Methodology.
    story.append(Paragraph("4. Methodology", ss["H1"]))
    story.append(Paragraph(
        f"We asked 3 AI models 50 questions each. These questions are divided into themes (see Section 5), "
        f"and are specifically written so that the answer to each one is information that exists in "
        f"{data.brand_name}'s own documentation. We then test whether the AI mentions {data.brand_name} as a "
        f"product, why it possibly does or doesn't, who else gets mentioned instead, and turn that into data. "
        f"That data is summarised into four themes (Section 5), reported both overall and per question "
        f"category. From there we draw conclusions and give a recommendation and a remediation plan "
        f"(Section 6) on how AI could find {data.brand_name} better and more accurately. Every conclusion in "
        f"Section 6 states how it was determined.", ss["Normal"],
    ))
    story.append(Paragraph(
        "<b>What this methodology does NOT yet do:</b> today's questions are realistic, real-world-style "
        "questions an architect or specifier would actually ask -- they are not built by taking a specific "
        "fact from your documentation and asking the AI to retrieve it directly. A complementary, more "
        "targeted test -- picking a fact that is already correctly documented and asking the AI specifically "
        "about it, to measure retrieval/citation performance on KNOWN facts rather than organic mention "
        "behaviour -- is a natural next addition to this methodology. It is not implemented in this report.",
        ss["Small"],
    ))
    story.append(PageBreak())

    # 5. The research: intents with their real prompts, then the four themes explained.
    story.append(Paragraph("5. The research", ss["H1"]))
    story.append(Paragraph(
        f"We asked {n_prompts} questions, each repeated {run['replicates']} time(s) ({n_answers} answers "
        f"total against {run['engine']} for this run). Every question was pre-assigned to one of 7 "
        f"categories ({', '.join(INTENT_GLOSSARY)}), so we can see whether a brand is strong on one type of "
        f"question but invisible on another -- that difference is the whole sales argument of this report. "
        f"Below, each category is explained, followed by the real questions asked in it and why each one was "
        f"chosen. The answers themselves are in the Appendix.", ss["Normal"],
    ))
    story.append(Spacer(1, 3 * mm))

    prompts_by_intent: dict[str, list[str]] = {}
    for row in data.prompts_and_answers:
        lst = prompts_by_intent.setdefault(row["intent"], [])
        if row["prompt_text"] not in lst:
            lst.append(row["prompt_text"])

    for intent, info in INTENT_GLOSSARY.items():
        prompts = prompts_by_intent.get(intent, [])
        if not prompts:
            continue
        story.append(Paragraph(f"{info['label']} ({intent})", ss["H2"]))
        story.append(Paragraph(info["definition"], ss["Normal"]))
        for prompt_text in prompts:
            rationale = PROMPT_RATIONALES.get(prompt_text, "")
            block = [Paragraph(f"“{_xml_escape(prompt_text)}”", ss["Cell"])]
            if rationale:
                block.append(Paragraph(_xml_escape(rationale), ss["Small"]))
            story.append(KeepTogether(block))
        story.append(Spacer(1, 3 * mm))

    story.append(PageBreak())
    story.append(Paragraph("The four themes we test on", ss["H2"]))
    story.append(Paragraph(
        f"We assess every answer on four independent themes, deliberately not combined into one score. "
        f"<b>Important: the ground truth we compare against comes exclusively from {data.brand_name}'s own "
        f"documentation -- never from what the AI itself says.</b> We are not testing whether the AI agrees "
        f"with itself; we are testing whether the AI agrees with what {data.brand_name} itself publishes. A "
        f"claim with no ground truth to compare against -- no row at all, or the document is explicitly "
        f"silent -- is never counted as an error; it is reported separately as a documentation gap.",
        ss["Normal"],
    ))
    story.append(Spacer(1, 3 * mm))

    theme_defs = [
        ("Presence Rate", "Does the brand's name appear anywhere in the raw AI answer at all, regardless of "
                           "whether a specific, checkable claim comes with it.",
         "Without presence there is no chance at all of being chosen -- this is the baseline threshold."),
        ("Share of Voice", "This brand's share of all brand mentions that carry a checkable spec claim (so "
                            "not every mention -- only the \"substantive\" ones).",
         "Shows how you actually measure up against competitors, not just whether you get mentioned."),
        ("Spec Accuracy", "Of the claims that could actually be checked: what share of them were correct.",
         "The only theme that says anything about correctness -- and therefore the only one that can show "
         "\"no data\" when there isn't enough ground truth to check anything against."),
        ("Data Completeness", "What share of applicable specs has a cited value in the brand's own "
                               "documentation -- entirely independent of any AI answer.",
         "This sets the ceiling for how reliable Spec Accuracy can ever be: if this is low, a high Spec "
         "Accuracy is impossible to measure, no matter how good the AI actually is."),
    ]
    for title, what, why in theme_defs:
        story.append(Paragraph(title, ss["H2"]))
        story.append(Paragraph(f"<b>What it measures:</b> {what}", ss["Normal"]))
        story.append(Paragraph(f"<b>Why it matters:</b> {why}", ss["Normal"]))

    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "<b>Read this carefully:</b> an \"unverifiable\" verdict or a documentation gap does NOT mean the AI "
        "said something wrong, and it does not mean the AI is \"too generic\" either. It means our own "
        "documentation has no citable value on that point to check the answer against. At low data "
        "completeness, that is the most likely explanation for almost every \"unverifiable\" result in this "
        "report -- not a shortcoming of the AI.", ss["Normal"],
    ))
    if not sc.claims_extracted_for_run:
        story.append(Paragraph(
            "NOTE: claim extraction has not been run for this run yet -- share of voice and spec accuracy "
            "below show “no data”, not a real zero.", ss["Small"],
        ))

    story.append(PageBreak())

    # 6. Conclusions: everything this run actually found, in one place.
    story.append(Paragraph("6. Conclusions", ss["H1"]))

    story.append(Paragraph("Scores", ss["H2"]))
    story.append(Paragraph(
        "How this was determined: computed directly from this run's benchmark answers, extracted claims, "
        "and the brand's current documentation -- see Section 5 for what each theme measures.", ss["Small"],
    ))
    story.append(_table(
        [["Theme", "Overall"],
         ["Presence Rate", _fmt_rate(sc.presence_rate.overall)],
         ["Share of Voice", _fmt_rate(sc.share_of_voice.overall)],
         ["Spec Accuracy", _fmt_rate(sc.spec_accuracy.overall)],
         ["Data Completeness (brand-level, not run-scoped)", _fmt_rate(sc.data_completeness)]],
        col_widths=[110 * mm, 50 * mm],
    ))
    intents = sorted(set(sc.presence_rate.per_intent) | set(sc.share_of_voice.per_intent)
                      | set(sc.spec_accuracy.per_intent))
    if intents:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Per question category", ss["H2"]))
        rows = [["Category", "Presence Rate", "Share of Voice", "Spec Accuracy"]]
        for intent in intents:
            rows.append([intent, _fmt_rate(sc.presence_rate.per_intent.get(intent)),
                         _fmt_rate(sc.share_of_voice.per_intent.get(intent)),
                         _fmt_rate(sc.spec_accuracy.per_intent.get(intent))])
        story.append(_table(rows, col_widths=[50 * mm, 35 * mm, 40 * mm, 35 * mm]))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "Why this matters: a brand can score strongly on brand_direct (where the name is already in the "
        "question) while being invisible on spec_constrained (where a buyer names no brand, only a "
        "requirement) -- that is exactly the difference between \"being recognised\" and \"being found\".",
        ss["Small"],
    ))

    story.append(PageBreak())

    story.append(Paragraph("Critical Notes", ss["H2"]))
    story.append(Paragraph(
        "Our own assessment of the most important findings in this run, with advice. This section is always "
        "present, even when no demonstrably incorrect claims were found -- context and advice matter at "
        "least as much as a clean list.", ss["Small"],
    ))
    for note in data.critical_notes:
        block = [
            Paragraph(f"<b>{_xml_escape(note.heading)}</b>", ss["Normal"]),
            Paragraph(_xml_escape(note.body), ss["Normal"]),
        ]
        if note.advice:
            block.append(Paragraph(f"<b>Advice:</b> {_xml_escape(note.advice)}", ss["Normal"]))
        story.append(KeepTogether(block))
        story.append(Spacer(1, 3 * mm))

    if data.worst_findings:
        story.append(Paragraph("Detail per incorrect/confused claim", ss["H2"]))
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
                          ss["Normal"]),
                Paragraph(f"Question ({finding['intent']}): “{finding['prompt_text']}”", ss["Normal"]),
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

    story.append(Paragraph("Competitive Picture", ss["H2"]))
    story.append(Paragraph(
        "How this was determined: every distinct brand mention carrying a checkable spec claim, counted per "
        "question category. Correct/Incorrect/Confused are only filled in where a claim could actually be "
        "checked (see Data Completeness, Section 5); a row with only \"Unverifiable\" does not mean that "
        "brand said nothing useful -- only that there was nothing to check it against.", ss["Small"],
    ))
    if not data.competitive_picture:
        story.append(Paragraph(
            "No brand mentions with a checkable spec claim were extracted for this run.", ss["Normal"],
        ))
    else:
        rows = [["Category", "Brand", "Mentions", "Correct", "Incorrect", "Confused", "Unverifiable"]]
        for row in data.competitive_picture:
            label = row["brand_name"] + (" (competitor)" if row["is_competitor"] else "")
            rows.append([_cell(row["intent"], ss["Cell"]), _cell(label, ss["Cell"]), str(row["mentions"]),
                         str(row["correct"]), str(row["incorrect"]), str(row["likely_confusion"]),
                         str(row["unverifiable"])])
        story.append(_table(rows, col_widths=[26 * mm, 46 * mm, 16 * mm, 16 * mm, 16 * mm, 16 * mm, 20 * mm]))

    story.append(PageBreak())

    story.append(Paragraph("Documentation Gaps", ss["H2"]))
    story.append(Paragraph(
        "How this was determined: the AI made a claim about one of these specs, and the brand's own "
        "documentation has no current, verifiable value to check it against. This is NOT a list of AI "
        "mistakes -- it is a list of points where the brand itself has nothing citable on file to confirm "
        "or contradict what an AI says, meaning the brand has no control over that part of the story, "
        "whether the AI's answer happens to be right or not.", ss["Small"],
    ))
    if not data.documentation_gaps:
        story.append(Paragraph("None found.", ss["Normal"]))
    else:
        rows = [["Product", "Spec", "Category", "AI said"]]
        for gap in data.documentation_gaps:
            rows.append([_cell(gap["product_name"], ss["Cell"]), _cell(gap["spec_name_nl"], ss["Cell"]),
                         _cell(gap["intent"], ss["Cell"]), _cell(gap["source_quote"], ss["Cell"])])
        story.append(_table(rows, col_widths=[32 * mm, 32 * mm, 28 * mm, 68 * mm]))

    story.append(PageBreak())

    story.append(Paragraph("Crawlability", ss["H2"]))
    story.append(Paragraph(
        "How this was determined: robots.txt was fetched and evaluated against real precedence rules "
        "(most-specific user-agent group wins, longest matching path rule wins) for every major AI crawler. "
        "If a bot gets \"no\" here, that AI cannot read the site at all -- content quality stops mattering. "
        "This is the only purely technical section in this report (no AI answers needed to measure it): a "
        "block here is usually a one-line fix with the largest possible impact.", ss["Small"],
    ))
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
            rows.append([_cell(agent["agent_name"], ss["Cell"]), allowed,
                         _cell(agent["matched_rule"] or "(default -- no matching rule)", ss["Cell"])])
        story.append(_table(rows, col_widths=[35 * mm, 25 * mm, 100 * mm]))

    story.append(PageBreak())

    story.append(Paragraph("Page-Visibility Gaps", ss["H2"]))
    story.append(Paragraph(
        "How this was determined: for each verified spec value in the brand's own documentation, we checked "
        "whether that value also appears as literal, readable text on the product's own public page. A gap "
        "here means the value is likely trapped in a PDF or a table rendered as an image -- both invisible "
        "to an AI that only reads page text, even though the fact itself is fully documented and correct.",
        ss["Small"],
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

    story.append(Paragraph("Remediation, ordered by impact ÷ effort", ss["H2"]))
    story.append(Paragraph(
        "How this was determined: every item above (crawlability blocks, documentation gaps, page-visibility "
        "gaps, incorrect claims) rolled into one prioritised list. Effort/impact are staff judgment "
        "heuristics (1=low, 3=high), not derived from cost data -- ordering is a starting point for triage, "
        "not a guarantee.", ss["Small"],
    ))
    if not data.remediation:
        story.append(Paragraph("No remediation items identified.", ss["Normal"]))
    else:
        rows = [["Item", "Category", "Effort", "Impact", "Detail"]]
        for item in data.remediation:
            rows.append([_cell(item.title, ss["Cell"]), item.category, str(item.effort), str(item.impact),
                         _cell(item.detail, ss["Cell"])])
        story.append(_table(rows, col_widths=[32 * mm, 22 * mm, 14 * mm, 14 * mm, 78 * mm]))

    story.append(PageBreak())

    # 7. Appendix: every prompt and every raw answer -- full auditability.
    # Rationale for each question already lives in Section 5; this is
    # purely the source material -- a client must be able to read exactly
    # what was asked and exactly what came back, unmediated.
    story.append(Paragraph("7. Appendix: all questions and AI answers", ss["H1"]))
    story.append(Paragraph(
        f"All {n_answers} answers {run['engine']} actually gave to the {n_prompts} questions in this run, "
        f"unedited and in full -- this is the source text every conclusion in this report is based on. "
        f"Grouped by category, then by question; each repeat shown separately since answers can differ "
        f"between repeats. Questions and answers are shown in their original language (Dutch, matching this "
        f"brand's market) so they can be matched verbatim against the live prompt set.", ss["Normal"],
    ))

    current_intent = None
    current_prompt = None
    for row in data.prompts_and_answers:
        if row["intent"] != current_intent:
            current_intent = row["intent"]
            label = INTENT_GLOSSARY.get(current_intent, {}).get("label", current_intent)
            story.append(Paragraph(f"{label} ({current_intent})", ss["H1"]))
            current_prompt = None
        if row["prompt_text"] != current_prompt:
            current_prompt = row["prompt_text"]
            story.append(Paragraph(f"Question: “{_xml_escape(current_prompt)}”", ss["H2"]))
        model_note = f" — {_xml_escape(row['resolved_model_version'])}" if row.get("resolved_model_version") else ""
        story.append(Paragraph(f"<b>Answer {row['replicate_index'] + 1}{model_note}:</b>", ss["Small"]))
        answer_text = _xml_escape(row["response_text"] or "(empty answer)").replace("\n", "<br/>")
        story.append(Paragraph(answer_text, ss["Cell"]))
        story.append(Spacer(1, 3 * mm))

    doc = SimpleDocTemplate(
        output_path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
    )
    doc.build(story)


def generate_pdf_report(conn, *, run_id: str, output_path: str) -> ReportData:
    data = gather_report_data(conn, run_id=run_id)
    render_pdf(data, output_path)
    return data
