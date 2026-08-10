"""Visibility engine v2 — MVP brief Part 3.

Runs `prompts` (not prompt_library -- see 0014_prompts.sql, Part 2's
decision) against all three real consumer-facing AI products, WITH their
real web-search/grounding tool turned on: OpenAI Responses API +
`web_search`, Anthropic Messages API + the `web_search` server tool,
Gemini + Google Search grounding. Deliberately not benchmark.py's plain
chat-completion call -- the brief is explicit that weights-only API
numbers don't match what the customer actually sees in the chat product,
and that mismatch kills the report's credibility.

make_*_caller factories return a plain str -> AnswerResult callable, same
testability shape as benchmark.py's BenchmarkCaller: run_visibility's own
logic (persistence, resumability, cost/answer ceilings) is fully testable
with a fake caller, no network or SDK mocking required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import psycopg
from psycopg.types.json import Json, Jsonb


@dataclass
class AnswerResult:
    response_text: str
    resolved_model_version: Optional[str]
    citations: list[str] = field(default_factory=list)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


VisibilityCaller = Callable[[str], AnswerResult]


# ---------------------------------------------------------------------
# Cost estimation -- approximate, per-1M-token USD pricing. Deliberately
# rough: real provider pricing changes faster than this file will be
# revisited. Used for (a) the --dry-run projection before any call is
# made, as a pre-call ceiling check, and (b) the per-answer cost_usd
# recorded after the fact when actual token usage isn't available.
# CHECK THESE ARE STILL CURRENT before relying on either for a real
# budget decision -- same caveat as the model defaults in .env.example.
# ---------------------------------------------------------------------

PRICING_USD_PER_1M_TOKENS: dict[str, dict[str, float]] = {
    "openai": {"input": 2.50, "output": 10.00},
    "anthropic": {"input": 3.00, "output": 15.00},
    "gemini": {"input": 0.10, "output": 0.40},
}

# Used for the pre-call cost-ceiling check and the --dry-run projection,
# before any real usage numbers exist for a given answer. Web-search/
# grounding tool calls typically inject several thousand tokens of
# retrieved page content ahead of the model's own answer -- deliberately
# higher than a plain, non-grounded chat completion's usual footprint.
ESTIMATED_TOKENS_PER_ANSWER = {"input": 3000, "output": 800}


def estimate_cost_usd(engine: str, input_tokens: int, output_tokens: int) -> float:
    pricing = PRICING_USD_PER_1M_TOKENS[engine]
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing["output"]


def estimate_answer_cost_usd(engine: str) -> float:
    return estimate_cost_usd(
        engine, ESTIMATED_TOKENS_PER_ANSWER["input"], ESTIMATED_TOKENS_PER_ANSWER["output"]
    )


# ---------------------------------------------------------------------
# Engine callers -- each does exactly one real API call with that
# provider's real web-search/grounding tool enabled, and normalizes the
# result to AnswerResult. Kept intentionally thin, same rule as
# benchmark.py's make_chatgpt_caller/make_gemini_caller.
# ---------------------------------------------------------------------

def make_openai_caller(api_key: str, model: str) -> VisibilityCaller:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    def _call(prompt_text: str) -> AnswerResult:
        response = client.responses.create(
            model=model,
            input=prompt_text,
            tools=[{"type": "web_search"}],
        )
        citations: list[str] = []
        for item in getattr(response, "output", None) or []:
            for content in getattr(item, "content", None) or []:
                for annotation in getattr(content, "annotations", None) or []:
                    url = getattr(annotation, "url", None)
                    if url:
                        citations.append(url)
        usage = getattr(response, "usage", None)
        return AnswerResult(
            response_text=response.output_text or "",
            resolved_model_version=getattr(response, "model", None),
            citations=citations,
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
        )

    return _call


def make_anthropic_caller(api_key: str, model: str) -> VisibilityCaller:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    def _call(prompt_text: str) -> AnswerResult:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt_text}],
        )
        text_parts: list[str] = []
        citations: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
                for citation in getattr(block, "citations", None) or []:
                    url = getattr(citation, "url", None)
                    if url:
                        citations.append(url)
        usage = getattr(response, "usage", None)
        return AnswerResult(
            response_text="".join(text_parts),
            resolved_model_version=getattr(response, "model", None),
            citations=citations,
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
        )

    return _call


def make_gemini_caller(api_key: str, model: str) -> VisibilityCaller:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    def _call(prompt_text: str) -> AnswerResult:
        response = client.models.generate_content(
            model=model,
            contents=prompt_text,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
        citations: list[str] = []
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            grounding = getattr(candidates[0], "grounding_metadata", None)
            for chunk in getattr(grounding, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None) if web else None
                if uri:
                    citations.append(uri)
        usage = getattr(response, "usage_metadata", None)
        return AnswerResult(
            response_text=response.text or "",
            resolved_model_version=getattr(response, "model_version", None),
            citations=citations,
            input_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            output_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
        )

    return _call


ENGINE_CALLER_FACTORIES: dict[str, Callable[[str, str], VisibilityCaller]] = {
    "openai": make_openai_caller,
    "anthropic": make_anthropic_caller,
    "gemini": make_gemini_caller,
}


# ---------------------------------------------------------------------
# Planning -- used by --dry-run, and answers the "would this exceed
# max_answers_per_run" question before any calls are made.
# ---------------------------------------------------------------------

@dataclass
class VisibilityPlan:
    engine: str
    prompt_count: int
    replicates: int
    already_answered: int  # existing rows if resuming run_id, else 0

    @property
    def planned_answers(self) -> int:
        return max(self.prompt_count * self.replicates - self.already_answered, 0)

    @property
    def estimated_cost_usd(self) -> float:
        return self.planned_answers * estimate_answer_cost_usd(self.engine)


def plan_run(
    conn: psycopg.Connection, *, category_id: str, engine: str, replicates: int = 3,
    resume_run_id: Optional[str] = None,
) -> VisibilityPlan:
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) as n from prompts where category_id = %s and is_active", (category_id,)
        )
        prompt_count = cur.fetchone()["n"]

        already_answered = 0
        if resume_run_id:
            cur.execute(
                "select count(*) as n from visibility_answers where run_id = %s", (resume_run_id,)
            )
            already_answered = cur.fetchone()["n"]

    return VisibilityPlan(
        engine=engine, prompt_count=prompt_count, replicates=replicates, already_answered=already_answered
    )


# ---------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------

class CostCeilingReached(Exception):
    """Stops a run mid-way without marking it 'failed' -- hitting the
    ceiling is an intentional stop, not an error. The run is left in
    'running' status so a later --resume-run can continue it, optionally
    after raising VISIBILITY_COST_CEILING_USD."""


class MaxAnswersReached(Exception):
    """Same idea as CostCeilingReached but for the per-invocation
    answer-count hard-abort."""


@dataclass
class VisibilityStats:
    answers_attempted: int = 0  # this invocation only
    answers_succeeded: int = 0
    answers_failed: int = 0
    cost_usd_this_invocation: float = 0.0


def run_visibility(
    conn: psycopg.Connection, *, organization_id: str, brand_id: str, category_id: str,
    engine: str, requested_model: str, caller: VisibilityCaller, replicates: int = 3,
    max_answers_per_run: int, cost_ceiling_usd: float,
    resume_run_id: Optional[str] = None, triggered_by: Optional[str] = None,
    commit_each_answer: bool = False,
) -> tuple[str, VisibilityStats]:
    """Runs every active prompt in `category_id` through `caller`,
    `replicates` times each, skipping any (prompt_id, replicate_index)
    that already has a row under this run (resumability). Stops early,
    WITHOUT marking the run failed, if max_answers_per_run or
    cost_ceiling_usd would be exceeded by the next answer -- both are
    pre-call checks, so the ceiling is never overshot by more than one
    answer's estimated cost.

    A single failed API call does not fail the run or get persisted --
    it's simply absent from visibility_answers, so the run is left
    'running' (not 'succeeded') if any (prompt, replicate) pair is still
    missing at the end of the pass, ready for the next --resume-run to
    retry exactly the gaps.

    commit_each_answer: when True, commits the connection right after the
    run row itself is created/resumed, and again after each successfully-
    persisted answer -- this is what 0016_visibility_runs.sql's own
    comment on cost_usd promises ("updated as answers land ... so a crash
    mid-run leaves an accurate number behind for the cost-ceiling check on
    resume"). Without it nothing is durable until the CALLER commits,
    typically only once after this function fully returns -- a crash mid-
    run then loses every answer and every dollar of real spend back to
    that last caller-issued commit, and --resume-run re-spends money the
    ceiling was meant to protect against. Defaults to False so existing
    callers/tests that manage their own transaction boundaries (in
    particular this test suite's rollback-based isolation, see
    tests/conftest.py) are unaffected -- cli.py's real `benchmark` command
    passes True.
    """
    with conn.cursor() as cur:
        if resume_run_id:
            cur.execute("select * from visibility_runs where id = %s", (resume_run_id,))
            run = cur.fetchone()
            if run is None:
                raise LookupError(f"visibility_run {resume_run_id} not found")
            if run["status"] != "running":
                raise ValueError(f"visibility_run {resume_run_id} has status {run['status']!r}, not resumable")
            if run["engine"] != engine or str(run["category_id"]) != str(category_id) \
                    or str(run["brand_id"]) != str(brand_id):
                raise ValueError(
                    f"visibility_run {resume_run_id} was started with engine={run['engine']!r} "
                    f"category_id={run['category_id']} brand_id={run['brand_id']} -- doesn't match this call"
                )
            run_id = run["id"]
            cost_so_far = float(run["cost_usd"])
        else:
            cur.execute(
                """
                insert into visibility_runs
                  (organization_id, brand_id, category_id, engine, requested_model, replicates, triggered_by)
                values (%s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (organization_id, brand_id, category_id, engine, requested_model, replicates, triggered_by),
            )
            run_id = cur.fetchone()["id"]
            cost_so_far = 0.0

        cur.execute(
            "select id, text from prompts where category_id = %s and is_active order by intent, created_at",
            (category_id,),
        )
        prompts = cur.fetchall()

        cur.execute(
            "select prompt_id, replicate_index from visibility_answers where run_id = %s",
            (run_id,),
        )
        already = {(row["prompt_id"], row["replicate_index"]) for row in cur.fetchall()}

    if commit_each_answer:
        # Make the run row itself durable before any answer processing
        # starts -- otherwise, if the very first answer's insert fails
        # before any per-answer commit below ever runs, the rollback in
        # the exception handler would discard this INSERT too, and the
        # recovery UPDATE that follows would silently affect zero rows
        # (the run_id it's targeting would no longer exist).
        conn.commit()

    stats = VisibilityStats()
    try:
        for prompt in prompts:
            for replicate_index in range(replicates):
                if (prompt["id"], replicate_index) in already:
                    continue

                if stats.answers_attempted >= max_answers_per_run:
                    raise MaxAnswersReached(
                        f"max_answers_per_run ({max_answers_per_run}) reached this invocation "
                        f"-- resume with --resume-run {run_id} to continue"
                    )

                projected_cost = cost_so_far + estimate_answer_cost_usd(engine)
                if projected_cost > cost_ceiling_usd:
                    raise CostCeilingReached(
                        f"cost ceiling (${cost_ceiling_usd:.2f}) would be exceeded by the next answer "
                        f"(estimated running total ${projected_cost:.2f}) "
                        f"-- resume with --resume-run {run_id} after raising VISIBILITY_COST_CEILING_USD"
                    )

                stats.answers_attempted += 1
                try:
                    result = caller(prompt["text"])
                except Exception:
                    stats.answers_failed += 1
                    continue

                if result.input_tokens is not None and result.output_tokens is not None:
                    answer_cost = estimate_cost_usd(engine, result.input_tokens, result.output_tokens)
                else:
                    answer_cost = estimate_answer_cost_usd(engine)

                with conn.cursor() as insert_cur:
                    insert_cur.execute(
                        """
                        insert into visibility_answers
                          (run_id, prompt_id, replicate_index, response_text, resolved_model_version,
                           citations, input_tokens, output_tokens, cost_usd)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (run_id, prompt["id"], replicate_index, result.response_text,
                         result.resolved_model_version, Json(result.citations),
                         result.input_tokens, result.output_tokens, answer_cost),
                    )
                    insert_cur.execute(
                        "update visibility_runs set cost_usd = cost_usd + %s where id = %s",
                        (answer_cost, run_id),
                    )
                cost_so_far += answer_cost
                stats.cost_usd_this_invocation += answer_cost
                stats.answers_succeeded += 1
                if commit_each_answer:
                    conn.commit()

    except (MaxAnswersReached, CostCeilingReached) as stop_reason:
        with conn.cursor() as cur:
            cur.execute(
                "update visibility_runs set stats = stats || %s where id = %s",
                (Jsonb({"last_stop_reason": str(stop_reason)}), run_id),
            )
        if commit_each_answer:
            conn.commit()
        return run_id, stats
    except Exception as exc:
        # A DB-level failure (e.g. the insert above hitting a constraint)
        # leaves the connection in an aborted-transaction state -- any
        # further query, including the recovery UPDATE right below, would
        # itself raise InFailedSqlTransaction and mask the real error
        # without this rollback first. Safe even when the failure was NOT
        # DB-related (a plain Python exception leaves no transaction to
        # roll back, so this is a no-op in that case) and safe with
        # commit_each_answer=True (prior answers are already committed --
        # this only discards the current still-open, never-committed work).
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "update visibility_runs set status = 'failed', error = %s, finished_at = now() where id = %s",
                (str(exc), run_id),
            )
        if commit_each_answer:
            conn.commit()
        raise

    # The pass finished without hitting a ceiling -- but individual calls
    # may still have failed and be silently missing. Only 'succeeded' when
    # every (prompt, replicate) pair genuinely has a row; otherwise leave
    # 'running' so the gaps left by transient failures get retried on the
    # next --resume-run instead of being reported as done when they aren't.
    expected_total = len(prompts) * replicates
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from visibility_answers where run_id = %s", (run_id,))
        total_answers = cur.fetchone()["n"]

        if total_answers >= expected_total:
            cur.execute(
                "update visibility_runs set status = 'succeeded', finished_at = now(), "
                "stats = stats || %s where id = %s",
                (Jsonb({"total_answers": total_answers}), run_id),
            )
        else:
            cur.execute(
                "update visibility_runs set stats = stats || %s where id = %s",
                (Jsonb({"total_answers": total_answers,
                       "last_stop_reason": f"{expected_total - total_answers} answer(s) failed this pass "
                                           f"-- resume with --resume-run {run_id} to retry them"}), run_id),
            )

    if commit_each_answer:
        conn.commit()

    return run_id, stats
