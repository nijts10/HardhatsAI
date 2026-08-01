import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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

    # text-embedding-3-small's native output size. document_chunks.embedding
    # is hardcoded to vector(1536) — changing this means a migration and a
    # full re-embed.
    embedding_dimensions: int = 1536


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
    )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value
