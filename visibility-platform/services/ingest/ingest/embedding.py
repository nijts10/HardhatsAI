"""Stage 5: embeddings.

OpenAI text-embedding-3-small, chosen because it outputs 1536 dimensions
natively — matching document_chunks.embedding vector(1536) and both search
functions with no truncation or padding. See CLAUDE.md: changing the model
means a migration and a full re-embed of the corpus.
"""
from __future__ import annotations

from openai import OpenAI

BATCH_SIZE = 96  # comfortably under OpenAI's per-request item/token limits


class Embedder:
    def __init__(self, api_key: str, model: str, expected_dimensions: int = 1536):
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._expected_dimensions = expected_dimensions

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start:start + BATCH_SIZE]
            response = self._client.embeddings.create(model=self._model, input=batch)
            for item in response.data:
                if len(item.embedding) != self._expected_dimensions:
                    raise ValueError(
                        f"embedding model returned {len(item.embedding)} dims, "
                        f"schema expects {self._expected_dimensions}"
                    )
                vectors.append(item.embedding)
        return vectors
