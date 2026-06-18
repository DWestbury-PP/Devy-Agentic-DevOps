"""Embeddings via LiteLLM — provider-agnostic, with a test seam.

Anthropic has no embeddings endpoint, so embeddings are configured separately
from the chat *tiers*. The default is OpenAI ``text-embedding-3-small``; a local
Ollama model (``ollama/nomic-embed-text``) or Voyage works with a one-line config
swap. The ``embed_fn`` seam mirrors ``ProviderClient.completion_fn`` so tests can
inject deterministic vectors without a network call.
"""

from __future__ import annotations

from typing import Callable, Optional

EmbedFn = Callable[[list[str], str, Optional[str]], list[list[float]]]


def _default_embed_fn(texts: list[str], model: str, api_base: Optional[str]) -> list[list[float]]:
    # Imported lazily so unit tests using the seam don't require litellm.
    import litellm

    kwargs: dict = {"model": model, "input": texts}
    if api_base:
        kwargs["api_base"] = api_base
    resp = litellm.embedding(**kwargs)
    data = resp.data if hasattr(resp, "data") else resp["data"]
    out: list[list[float]] = []
    for item in data:
        vec = item["embedding"] if isinstance(item, dict) else item.embedding
        out.append(list(vec))
    return out


class Embedder:
    """Turns text into vectors in batches. One model for both ingest and query."""

    def __init__(
        self,
        model: str = "openai/text-embedding-3-small",
        api_base: Optional[str] = None,
        batch_size: int = 64,
        embed_fn: Optional[EmbedFn] = None,
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.batch_size = max(1, batch_size)
        self._embed_fn = embed_fn or _default_embed_fn

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            vectors.extend(self._embed_fn(batch, self.model, self.api_base))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]
