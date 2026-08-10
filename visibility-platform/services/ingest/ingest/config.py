import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# The ONE place this number is chosen. text-embedding-3-small's native
# output size, matching document_chunks.embedding's column type in
# 0003_extraction.sql (vector(1536)).
#
# That migration is already applied to the live project -- per this repo's
# rule (never hand-edit an applied migration) and Postgres's own
# requirement that a vector(n) column's dimension be a literal, not a
# variable, the SQL itself can't reference this constant directly. Every
# other place in the codebase that needs this number imports it from here
# instead of hardcoding it again, and tests/test_db.py has a test that
# inserts against the real column to prove the two stay in sync. Changing
# this constant means a new migration (ALTER COLUMN ... TYPE vector(N)) and
# a full re-embed of the corpus -- it is not just a config edit.
EMBEDDING_DIMENSIONS = 1536


@dataclass(frozen=True)
class Config:
    database_url: str
    supabase_url: str
    supabase_service_role_key: str
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_embedding_model: str
    openai_chat_model: str
    google_api_key: str
    gemini_model: str
    worker_name: str

    # Part 3's benchmark engine (visibility.py) gets its OWN model config,
    # deliberately separate from anthropic_model/openai_chat_model/
    # gemini_model above (decided with stijn 2026-08-10). Those exist for
    # extraction (Claude) and the old benchmark.py (ChatGPT/Gemini plain
    # chat completions); Part 3 is standing in for what a real end user
    # sees in each provider's actual chat product, tools/web-search wired
    # in, which is a different job that may reasonably want a different
    # model than extraction picks for cost/quality reasons -- pinning them
    # together would make that an accidental coupling, not a decision.
    visibility_anthropic_model: str
    visibility_openai_model: str
    visibility_gemini_model: str
    # Hard-abort knobs read once per `benchmark` run, not per-call — see
    # visibility.py's run_visibility().
    visibility_max_answers_per_run: int
    visibility_cost_ceiling_usd: float

    embedding_dimensions: int = EMBEDDING_DIMENSIONS


def load_config() -> Config:
    return Config(
        database_url=_require("DATABASE_URL"),
        supabase_url=os.environ.get("SUPABASE_URL", ""),
        supabase_service_role_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_embedding_model=os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        openai_chat_model=os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o"),
        google_api_key=os.environ.get("GOOGLE_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
        worker_name=os.environ.get("WORKER_NAME", "visibility-ingest-worker"),
        visibility_anthropic_model=os.environ.get("VISIBILITY_ANTHROPIC_MODEL", "claude-sonnet-5"),
        visibility_openai_model=os.environ.get("VISIBILITY_OPENAI_MODEL", "gpt-4o"),
        visibility_gemini_model=os.environ.get("VISIBILITY_GEMINI_MODEL", "gemini-2.0-flash"),
        visibility_max_answers_per_run=int(os.environ.get("VISIBILITY_MAX_ANSWERS_PER_RUN", "500")),
        visibility_cost_ceiling_usd=float(os.environ.get("VISIBILITY_COST_CEILING_USD", "50")),
    )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value
