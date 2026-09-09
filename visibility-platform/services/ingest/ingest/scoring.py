"""Part 6: scoring -- four independent numbers, deliberately NOT combined
into a single score. The brief is explicit about why: "a single score
invites 'how did you get that' and moves the conversation onto ground you
cannot defend." Each is computed both overall and per prompt_intent for
ONE visibility_runs id (matches Part 7's own `report --run <id>` shape),
except data_completeness, which has no per-run/per-intent meaning at all
-- it's brand-level ground-truth coverage, not a property of any one AI
answer, so it's computed once and attached unscoped. The brief calls this
"the existing coverage metric" -- reuses report.compute_coverage()
directly rather than reimplementing it.

None (not 0.0) whenever a metric's denominator is zero -- "no data for
this intent" and "0%" are different findings, and this codebase never
conflates them anywhere else (claims/spec_attributes/page_spec_visibility
all follow the same rule). A per-intent bucket simply doesn't appear in
the dict at all when nothing fell into it -- no fabricated 0/0 entries.

presence_rate reuses benchmark.detect_mention() directly (same
case-insensitive substring check the old system uses) against
visibility_answers.response_text -- the plain "does the brand's name show
up in the raw answer" signal.

share_of_voice and spec_accuracy both depend on Part 4's answer_claims,
NOT on presence_rate's raw-text signal -- there's no canonical list of
competitor brand names to search for in text, so answer_claims (the
LLM's own extraction of which brands it discussed) is the only
structured source for "which OTHER brands got mentioned". This means
BOTH require `claims extract --run <id>` to have already been run;
compute_scorecard() surfaces this as claims_extracted_for_run rather than
silently showing misleading zeros when it hasn't.

share_of_voice counts DISTINCT (answer, brand) pairs, not raw claim
count -- a brand mentioned with five claims in one answer and a
competitor mentioned with one claim in a different answer should not be
5x vs 1x for "how many conversations was each brand visible in", which is
what share of voice actually means.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import db
from .benchmark import detect_mention
from .report import compute_coverage

_VERIFIABLE_VERDICTS = {"correct", "incorrect", "likely_confusion"}


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return (numerator / denominator) if denominator else None


@dataclass
class IntentScore:
    overall: Optional[float]
    per_intent: dict[str, Optional[float]] = field(default_factory=dict)


@dataclass
class ScoreCard:
    run_id: str
    brand_name: str
    presence_rate: IntentScore
    share_of_voice: IntentScore
    spec_accuracy: IntentScore
    data_completeness: Optional[float]
    claims_extracted_for_run: bool


def compute_presence_rate(conn, *, run_id: str, brand_name: str, brand_id: str | None = None) -> IntentScore:
    # Sub-brand names (HeartFelt, PareauLux, Luxalon, ...) count as a real
    # mention too -- see detect_mention's own docstring for why. brand_id
    # is optional so a caller without one (or a test) still gets the
    # pre-existing, brand-name-only behaviour, not an error.
    aliases = [pl["name"] for pl in db.get_product_lines_for_brand(conn, brand_id)] if brand_id else []
    per_intent: dict[str, list[int]] = {}
    overall = [0, 0]
    for row in db.get_answers_with_intent_for_run(conn, run_id):
        mentioned, _ = detect_mention(row["response_text"], brand_name, aliases)
        bucket = per_intent.setdefault(row["intent"], [0, 0])
        bucket[1] += 1
        overall[1] += 1
        if mentioned:
            bucket[0] += 1
            overall[0] += 1
    return IntentScore(_rate(*overall), {k: _rate(*v) for k, v in per_intent.items()})


def compute_share_of_voice(conn, *, run_id: str) -> IntentScore:
    per_intent: dict[str, list[int]] = {}
    overall = [0, 0]
    for row in db.get_distinct_brand_mentions_for_run(conn, run_id):
        bucket = per_intent.setdefault(row["intent"], [0, 0])
        bucket[1] += 1
        overall[1] += 1
        if not row["is_competitor"]:
            bucket[0] += 1
            overall[0] += 1
    return IntentScore(_rate(*overall), {k: _rate(*v) for k, v in per_intent.items()})


def compute_spec_accuracy(conn, *, run_id: str) -> IntentScore:
    per_intent: dict[str, list[int]] = {}
    overall = [0, 0]
    for row in db.get_own_brand_claim_verdicts_for_run(conn, run_id):
        if row["verdict"] not in _VERIFIABLE_VERDICTS:
            continue  # unverifiable_product/unverifiable_spec never count as an error -- excluded, not 0
        bucket = per_intent.setdefault(row["intent"], [0, 0])
        bucket[1] += 1
        overall[1] += 1
        if row["verdict"] == "correct":
            bucket[0] += 1
            overall[0] += 1
    return IntentScore(_rate(*overall), {k: _rate(*v) for k, v in per_intent.items()})


def compute_scorecard(conn, *, run_id: str) -> ScoreCard:
    # Note: BrandCoverage.completeness (report.py) returns 0.0, not None,
    # when a brand has zero products/applicable specs -- that's the
    # existing function's own established behaviour, left exactly as-is
    # per this reuse (not silently changed to fit this module's own
    # None-means-no-data convention elsewhere).
    run = db.get_visibility_run(conn, run_id)
    if run is None:
        raise ValueError(f"visibility_runs {run_id} not found")

    return ScoreCard(
        run_id=run_id,
        brand_name=run["brand_name"],
        presence_rate=compute_presence_rate(conn, run_id=run_id, brand_name=run["brand_name"], brand_id=run["brand_id"]),
        share_of_voice=compute_share_of_voice(conn, run_id=run_id),
        spec_accuracy=compute_spec_accuracy(conn, run_id=run_id),
        data_completeness=compute_coverage(conn, run["brand_id"]).completeness,
        claims_extracted_for_run=db.has_any_answer_claims_for_run(conn, run_id),
    )
