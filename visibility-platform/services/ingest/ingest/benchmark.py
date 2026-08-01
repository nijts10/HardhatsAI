"""Benchmark runner — the actual "AI visibility" measurement.

Runs the active prompt library against a real consumer-facing AI model
with a neutral prompt: no system message biasing toward the brand, no
retrieval/grounding/tools that would artificially inject it. Just what a
real person asking that question would see, then a plain check for
whether the brand's name shows up in the organic response.

make_chatgpt_caller/make_gemini_caller do the actual API calls and are
intentionally thin — run_benchmark takes a caller as a plain
str -> str callable, so its logic (persistence, mention detection, run
bookkeeping) is fully testable with a fake caller, no network or SDK
mocking required.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import psycopg
from psycopg.types.json import Json

BenchmarkCaller = Callable[[str], str]


def detect_mention(response_text: str, brand_name: str) -> tuple[bool, int | None]:
    """Case-insensitive substring match. Deliberately simple and honest —
    no fuzzy matching that could inflate the rate, no claim of true rank,
    just: does the brand's name appear, and where."""
    needle = brand_name.strip().lower()
    if not needle:
        return False, None
    offset = response_text.lower().find(needle)
    return (offset != -1), (offset if offset != -1 else None)


def make_chatgpt_caller(api_key: str, model: str) -> BenchmarkCaller:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    def _call(prompt_text: str) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt_text},
            ],
        )
        return response.choices[0].message.content or ""

    return _call


def make_gemini_caller(api_key: str, model: str) -> BenchmarkCaller:
    from google import genai

    client = genai.Client(api_key=api_key)

    def _call(prompt_text: str) -> str:
        response = client.models.generate_content(model=model, contents=prompt_text)
        return response.text or ""

    return _call


@dataclass
class BenchmarkStats:
    prompts_run: int = 0
    mentions: int = 0

    @property
    def mention_rate(self) -> float:
        return self.mentions / self.prompts_run if self.prompts_run else 0.0


def run_benchmark(
    conn: psycopg.Connection, *, organization_id: str, brand_id: str, brand_name: str,
    model: str, caller: BenchmarkCaller, triggered_by: str | None = None,
) -> tuple[str, BenchmarkStats]:
    with conn.cursor() as cur:
        cur.execute(
            "insert into benchmark_runs (organization_id, brand_id, model, triggered_by) "
            "values (%s, %s, %s, %s) returning id",
            (organization_id, brand_id, model, triggered_by),
        )
        run_id = cur.fetchone()["id"]

        cur.execute("select id, prompt_text from prompt_library where is_active order by use_case")
        prompts = cur.fetchall()

    stats = BenchmarkStats()
    try:
        for prompt in prompts:
            response_text = caller(prompt["prompt_text"])
            mentioned, offset = detect_mention(response_text, brand_name)
            with conn.cursor() as cur:
                cur.execute(
                    "insert into benchmark_results "
                    "(benchmark_run_id, prompt_id, response_text, brand_mentioned, mention_offset) "
                    "values (%s, %s, %s, %s, %s)",
                    (run_id, prompt["id"], response_text, mentioned, offset),
                )
            stats.prompts_run += 1
            if mentioned:
                stats.mentions += 1
    except Exception as exc:
        with conn.cursor() as cur:
            cur.execute(
                "update benchmark_runs set status = 'failed', error = %s, finished_at = now() where id = %s",
                (str(exc), run_id),
            )
        raise

    with conn.cursor() as cur:
        cur.execute(
            "update benchmark_runs set status = 'succeeded', finished_at = now(), stats = %s where id = %s",
            (Json({
                "prompts_run": stats.prompts_run,
                "mentions": stats.mentions,
                "mention_rate": stats.mention_rate,
            }), run_id),
        )

    return run_id, stats
