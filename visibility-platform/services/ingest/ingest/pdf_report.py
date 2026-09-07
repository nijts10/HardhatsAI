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
        "label": "Eis-gedreven",
        "definition": "De vraag noemt concrete, meetbare eisen (waarden, normen, afmetingen) en vraagt om een "
                       "product dat daaraan voldoet.",
        "example": "Systeemplafond met αw minimaal 0,90 en brandklasse A2-s1,d0",
    },
    "application_driven": {
        "label": "Toepassing-gedreven",
        "definition": "De vraag beschrijft een ruimte of gebruikssituatie (klaslokaal, zwembad, operatiekamer) "
                       "zonder concrete specs te noemen, en vraagt wat daarvoor geschikt is.",
        "example": "Welk systeemplafond is geschikt voor een klaslokaal met veel nagalm?",
    },
    "comparative": {
        "label": "Vergelijkend",
        "definition": "De vraag vergelijkt expliciet twee of meer merken/producten (vaak inclusief concurrenten) "
                       "met elkaar.",
        "example": "Rockfon of Ecophon voor een schoolgebouw, wat is het verschil?",
    },
    "compliance": {
        "label": "Regelgeving-gedreven",
        "definition": "De vraag gaat over wettelijke eisen, bouwbesluit- of normeringsverplichtingen, niet over "
                       "een specifiek product.",
        "example": "Welke eisen stelt het Bouwbesluit aan geluidsabsorptie in een klaslokaal?",
    },
    "sustainability": {
        "label": "Duurzaamheid-gedreven",
        "definition": "De vraag draait om milieu-impact: MKI, GWP, recyclebaarheid, EPD's, Cradle to Cradle.",
        "example": "Systeemplafond met de laagste MKI-waarde",
    },
    "brand_direct": {
        "label": "Merk-specifiek",
        "definition": "De vraag noemt het merk zelf letterlijk. Dit is de enige categorie waarin \"gevonden "
                       "worden\" triviaal is (de merknaam staat al in de vraag) — hier is spec accuracy de "
                       "metric die er echt toe doet, niet presence rate.",
        "example": "Welke akoestische plafonds levert Hunter Douglas?",
    },
    "problem_driven": {
        "label": "Probleem-gedreven",
        "definition": "De vraag beschrijft een klacht of storing aan een bestaand plafond en vraagt om een "
                       "alternatief.",
        "example": "Mijn systeemplafond hangt door bij hoge luchtvochtigheid, welk alternatief?",
    },
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
            verdict_label = "onjuiste claim" if finding["verdict"] == "incorrect" else "mogelijk verwarde claim"
            advice = (
                f"Controleer of “{finding['spec_name_nl']}” voor {finding['product_name']} "
                f"correct en actueel gedocumenteerd is, en overweeg of de gebruikte bewoording elders online "
                f"(reviews, distributeurs, oudere versies van eigen materiaal) tot verwarring kan leiden."
            )
            notes.append(CriticalNote(
                heading=f"{finding['product_name']} — {finding['spec_name_nl']} ({verdict_label})",
                body=f"AI zei: “{finding['source_quote']}”. Dit wijkt af van de eigen, geciteerde "
                     f"ground truth.",
                advice=advice,
            ))
    else:
        checkable_note = (
            f"Geen enkele claim over {brand_name} is deze run als aantoonbaar onjuist gemarkeerd."
        )
        if own_mentions:
            checkable_note += (
                f" Belangrijk om dit correct te lezen: van de {own_mentions} keer dat {brand_name} genoemd "
                f"werd, kon slechts {own_checkable} claim(s) daadwerkelijk gecontroleerd worden tegen eigen "
                f"documentatie; {own_unverifiable} bleven “unverifiable” — niet omdat de AI iets fout "
                f"beweerde, maar omdat er simpelweg niets in de eigen documentatie stond om het tegen te "
                f"leggen. Dit is dus GEEN bevestiging dat alle AI-antwoorden correct zijn, alleen dat er geen "
                f"fout is aangetoond op het kleine deel dat wél te checken was."
            )
        notes.append(CriticalNote(
            heading="Geen aangetoonde onjuiste claims (lees de context hieronder)",
            body=checkable_note,
            advice="Onze aanbeveling: vul eerst de documentatiegaten hieronder in, zodat een vervolgmeting "
                   "wél een betekenisvol accuracy-cijfer kan opleveren.",
        ))

    if scorecard.data_completeness is not None and scorecard.data_completeness < _LOW_COMPLETENESS_THRESHOLD:
        notes.append(CriticalNote(
            heading=f"Eigen documentatiedekking is laag ({scorecard.data_completeness:.0%})",
            body="Data completeness meet welk deel van de toepasselijke specs een geciteerde waarde heeft in "
                 "de eigen documentatie — onafhankelijk van wat een AI zegt. Bij een lage dekking heeft dit "
                 "audit-systeem voor het grootste deel van de AI-antwoorden simpelweg niets om tegen te "
                 "controleren, dus vallen ze in “unverifiable” in plaats van correct of incorrect.",
            advice="Onze aanbeveling: behandel dit rapport in de huidige vorm primair als een "
                   "documentatie-audit, niet als een AI-accuracy-audit — dat laatste wordt pas betrouwbaar "
                   "zodra de dekking omhoog gaat.",
        ))

    if documentation_gaps:
        notes.append(CriticalNote(
            heading=f"{len(documentation_gaps)} documentatiegat(en) gevonden",
            body="Zie de sectie “Documentation gaps” verderop voor de volledige lijst, geprioriteerd "
                 "in de remediation-tabel.",
        ))

    if page_visibility_gaps:
        notes.append(CriticalNote(
            heading=f"{len(page_visibility_gaps)} page-visibility gat(en) gevonden",
            body="Deze specs staan wél geverifieerd in de eigen documentatie, maar niet als leesbare tekst op "
                 "de eigen productpagina — waarschijnlijk vastzittend in een PDF of een tabel-als-afbeelding.",
        ))

    return notes


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

    return ReportData(
        brand_name=run["brand_name"], run=run, scorecard=scorecard, worst_findings=worst_findings,
        competitive_picture=competitive_picture, documentation_gaps=documentation_gaps,
        crawlability_check=crawlability_check, crawlability_agents=crawlability_agents,
        page_visibility_checks=page_visibility_checks, page_visibility_gaps=page_visibility_gaps,
        prompts_and_answers=prompts_and_answers, remediation=remediation, critical_notes=critical_notes,
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

    # 1. Method -- first, not an appendix.
    story.append(Paragraph(f"AI Visibility Audit: {data.brand_name}", ss["Title"]))
    story.append(Paragraph("Method", ss["H1"]))
    story.append(Paragraph(
        f"This report covers one benchmark run against <b>{run['engine']}</b> "
        f"(requested model: {run['requested_model']}), {run['replicates']} replicate(s) per prompt, "
        f"started {run['started_at']}.", ss["Normal"],
    ))

    story.append(Paragraph("Hoe dit rapport is opgebouwd", ss["H2"]))
    story.append(Paragraph(
        "Elke sectie beantwoordt een andere vraag, in deze volgorde: <b>Scores</b> (de 4 kerncijfers, overall "
        "en per intentie), <b>Critical notes</b> (onze eigen beoordeling en advies — altijd ingevuld, ook als "
        "er geen fouten zijn gevonden), <b>Competitive picture</b> (wie wordt hoe vaak genoemd, per intentie, "
        "inclusief concurrenten), <b>Documentation gaps</b> (wat de AI beweerde zonder dat wij het konden "
        "controleren), <b>Crawlability</b> (mogen AI-bots de site uberhaupt lezen), <b>Page-visibility gaps</b> "
        "(staat een geverifieerde waarde ook als leesbare tekst op de eigen productpagina), "
        "<b>Remediation</b> (alles hierboven samengevat in één geprioriteerde actielijst), en tot slot een "
        "<b>Appendix</b> met alle prompts en alle daadwerkelijk gegeven AI-antwoorden, zodat elke conclusie "
        "in dit rapport zelf te controleren is.", ss["Normal"],
    ))

    story.append(Paragraph("Wat wordt precies gemeten, en hoe", ss["H2"]))
    story.append(Paragraph(
        "Elke technische bewering die de AI doet ({} antwoorden op {} prompts x {} herhaling(en)) wordt eruit "
        "gehaald en vergeleken met geverifieerde ground truth: een feit dat letterlijk terug te vinden is in "
        "een fabrikantendocument, met paginanummer en woordelijk citaat. <b>Belangrijk: die ground truth komt "
        "uitsluitend uit de eigen documentatie van {} — nooit uit wat de AI zelf zegt.</b> Er wordt dus niet "
        "getest of de AI het met zichzelf eens is; er wordt getest of de AI het eens is met wat {} zelf "
        "publiceert. Een bewering zonder ground truth om tegen te leggen — geen enkele rij, of het document "
        "is er expliciet stil over — telt nooit als fout; die wordt apart gerapporteerd als een "
        "documentatiegat.".format(
            len(data.prompts_and_answers), len(set(r["prompt_id"] for r in data.prompts_and_answers)),
            run["replicates"], data.brand_name, data.brand_name,
        ),
        ss["Normal"],
    ))
    story.append(Paragraph(
        "Vier cijfers worden hieronder los gerapporteerd, bewust niet samengevoegd tot één score (één score "
        "nodigt uit tot \"hoe kom je daarbij\" en verplaatst het gesprek naar grond die niet te verdedigen "
        "is):", ss["Normal"],
    ))
    metric_rows_raw = [
        ("Presence rate", "Komt de merknaam letterlijk voor in het ruwe AI-antwoord, ongeacht of er een "
                           "specifieke claim bij zit.",
         "Zonder presence geen enkele kans om gekozen te worden — dit is de basisdrempel."),
        ("Share of voice", "Het aandeel van dit merk in alle merkvermeldingen die een controleerbare "
                            "spec-claim bevatten (dus niet elke vermelding, alleen de \"zakelijke\" "
                            "vermeldingen).",
         "Laat zien hoe je concreet meetelt tussen concurrenten, niet alleen of je genoemd wordt."),
        ("Spec accuracy", "Van de claims die daadwerkelijk gecontroleerd konden worden: welk deel klopte.",
         "Dit is de enige metric die iets zegt over juistheid — en dus ook de enige die \"no data\" kan "
         "tonen als er te weinig ground truth is om iets te controleren."),
        ("Data completeness", "Welk deel van de toepasselijke specs een geciteerde waarde heeft in de eigen "
                               "documentatie — volledig onafhankelijk van welk AI-antwoord dan ook.",
         "Dit cijfer legt het plafond vast voor hoe betrouwbaar spec accuracy kán zijn: is dit laag, dan is "
         "een hoge spec accuracy sowieso onmogelijk te meten, ongeacht hoe goed de AI het echt doet."),
    ]
    metric_rows = [["Cijfer", "Wat het meet", "Waarom het ertoe doet"]]
    for cijfer, wat, waarom in metric_rows_raw:
        metric_rows.append([_cell(cijfer, ss["Cell"]), _cell(wat, ss["Cell"]), _cell(waarom, ss["Cell"])])
    story.append(_table(metric_rows, col_widths=[32 * mm, 68 * mm, 60 * mm]))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "<b>Lees dit zorgvuldig:</b> een \"unverifiable\"-verdict of een documentatiegat betekent NIET dat de "
        "AI iets fout heeft gezegd, en ook niet dat de AI \"te generiek\" is. Het betekent dat onze eigen "
        "documentatie op dat punt geen citeerbare waarde bevat om het antwoord tegen te controleren. Bij een "
        "lage data completeness (zie hierboven) is dat de meest waarschijnlijke verklaring voor bijna elk "
        "\"unverifiable\"-resultaat in dit rapport — niet een tekortkoming van de AI.", ss["Normal"],
    ))
    if not sc.claims_extracted_for_run:
        story.append(Paragraph(
            "NOTE: claim extraction has not been run for this run yet -- share of voice and spec accuracy "
            "below show “no data”, not a real zero.", ss["Small"],
        ))

    story.append(Paragraph("Wat betekenen de \"intents\" (promptcategorieën)", ss["H2"]))
    story.append(Paragraph(
        "Elke prompt is vooraf ingedeeld in één van 7 categorieën, om te kunnen zien of een merk sterk is op "
        "het ene type vraag maar onzichtbaar op het andere — dat verschil is precies het verkoopargument.",
        ss["Normal"],
    ))
    glossary_rows = [["Intent", "Betekenis", "Voorbeeldprompt"]]
    for key, info in INTENT_GLOSSARY.items():
        glossary_rows.append([
            _cell(f"{key} ({info['label']})", ss["Cell"]),
            _cell(info["definition"], ss["Cell"]),
            _cell(f"“{info['example']}”", ss["Cell"]),
        ])
    story.append(_table(glossary_rows, col_widths=[35 * mm, 75 * mm, 55 * mm]))

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

    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "Waarom dit ertoe doet: een merk kan sterk scoren op brand_direct (waar de naam al in de vraag "
        "staat) en tegelijk onzichtbaar zijn op spec_constrained (waar een koper geen merk noemt, alleen een "
        "eis) — dat is precies het verschil tussen \"herkend worden\" en \"gevonden worden\", en de kern van "
        "het verkoopargument in dit rapport.", ss["Small"],
    ))

    story.append(PageBreak())

    # 3. Critical notes -- ALWAYS populated, never a bare pass/fail line.
    # Renamed from "Worst findings": this section must carry our own
    # advisory read of the numbers even when nothing was found to be
    # flatly wrong, because "nothing wrong found" and "confirmed correct"
    # are two very different claims once data completeness is low.
    story.append(Paragraph("Critical notes", ss["H1"]))
    story.append(Paragraph(
        "Onze eigen beoordeling van de belangrijkste bevindingen in deze run, met advies. Deze sectie staat "
        "er altijd, ook wanneer er geen aantoonbaar onjuiste claims zijn gevonden — context en advies zijn "
        "dan minstens zo belangrijk als een schone lijst.", ss["Small"],
    ))
    for note in data.critical_notes:
        block = [
            Paragraph(f"<b>{note.heading}</b>", ss["H2"]),
            Paragraph(note.body, ss["Normal"]),
        ]
        if note.advice:
            block.append(Paragraph(f"<b>Advies:</b> {note.advice}", ss["Normal"]))
        story.append(KeepTogether(block))
        story.append(Spacer(1, 3 * mm))

    if data.worst_findings:
        story.append(Paragraph("Detail per onjuiste/verwarde claim", ss["H2"]))
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
    story.append(Paragraph(
        "Waarom dit ertoe doet: dit is de tabel achter de kale presence rate/share of voice-cijfers hierboven "
        "— hier zie je met concurrentnamen en aantallen wie er daadwerkelijk wordt genoemd per type vraag. "
        "Correct/Incorrect/Confused zijn alleen gevuld waar een claim daadwerkelijk gecontroleerd kon worden "
        "(zie de Method-sectie hierboven over data completeness); een rij met alleen \"Unverifiable\" betekent "
        "dus niet dat dat merk niets goeds zei, alleen dat er niets was om het tegen te controleren.", ss["Small"],
    ))
    if not data.competitive_picture:
        story.append(Paragraph(
            "No brand mentions with a checkable spec claim were extracted for this run.", ss["Normal"],
        ))
    else:
        rows = [["Intent", "Brand", "Mentions", "Correct", "Incorrect", "Confused", "Unverifiable"]]
        for row in data.competitive_picture:
            label = row["brand_name"] + (" (competitor)" if row["is_competitor"] else "")
            rows.append([_cell(row["intent"], ss["Cell"]), _cell(label, ss["Cell"]), str(row["mentions"]),
                         str(row["correct"]), str(row["incorrect"]), str(row["likely_confusion"]),
                         str(row["unverifiable"])])
        story.append(_table(rows, col_widths=[26 * mm, 46 * mm, 16 * mm, 16 * mm, 16 * mm, 16 * mm, 20 * mm]))

    story.append(PageBreak())

    # 5. Documentation gaps.
    story.append(Paragraph("Documentation gaps", ss["H1"]))
    story.append(Paragraph(
        "The AI made a claim about one of these specs, but your own documentation has no current, "
        "verifiable value to check it against — not an error, an opportunity.", ss["Normal"],
    ))
    story.append(Paragraph(
        "Waarom dit ertoe doet: dit is GEEN lijst van fouten van de AI. Het is een lijst van punten waar "
        "wij zelf niets citeerbaars hebben staan om te bevestigen of tegen te spreken wat een AI erover zegt "
        "— waardoor we geen controle hebben over dat deel van het verhaal, ongeacht of het antwoord nu "
        "toevallig klopt of niet.", ss["Small"],
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
    story.append(Paragraph(
        "Waarom dit ertoe doet: als een AI-bot hier \"no\" krijgt, kan die AI de site letterlijk niet lezen — "
        "dan doet de kwaliteit van de content er niet meer toe. Dit is de enige sectie in dit rapport die "
        "puur technisch is (geen AI-antwoorden nodig om te meten): een blokkade hier is meestal een "
        "één-regel-fix met het grootst mogelijke effect.", ss["Small"],
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

    story.append(PageBreak())

    # 9. Appendix: every prompt and every raw answer -- full auditability.
    # Every claim/verdict/gap above is derived from this data; a client
    # must be able to read the source material itself, not just our
    # extracted conclusions about it.
    story.append(Paragraph("Appendix: alle prompts en AI-antwoorden", ss["H1"]))
    story.append(Paragraph(
        f"Alle {len(data.prompts_and_answers)} antwoorden die {run['engine']} daadwerkelijk gaf op de "
        f"{len(set(r['prompt_id'] for r in data.prompts_and_answers))} prompts van deze run, ongewijzigd en "
        f"volledig — dit is de brontekst waar elke conclusie in dit rapport op is gebaseerd. Gegroepeerd per "
        f"intent, dan per prompt; elke herhaling (replicate) apart, omdat de antwoorden per keer kunnen "
        f"verschillen.", ss["Normal"],
    ))

    current_intent = None
    current_prompt = None
    for row in data.prompts_and_answers:
        if row["intent"] != current_intent:
            current_intent = row["intent"]
            label = INTENT_GLOSSARY.get(current_intent, {}).get("label", current_intent)
            story.append(Paragraph(f"{current_intent} ({label})", ss["H1"]))
            current_prompt = None
        if row["prompt_text"] != current_prompt:
            current_prompt = row["prompt_text"]
            story.append(Paragraph(f"Prompt: “{_xml_escape(current_prompt)}”", ss["H2"]))
        model_note = f" — {_xml_escape(row['resolved_model_version'])}" if row.get("resolved_model_version") else ""
        story.append(Paragraph(f"<b>Antwoord {row['replicate_index'] + 1}{model_note}:</b>", ss["Small"]))
        answer_text = _xml_escape(row["response_text"] or "(leeg antwoord)").replace("\n", "<br/>")
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
