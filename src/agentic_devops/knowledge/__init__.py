"""Knowledge subsystem: a content-agnostic retrieval pipeline.

Sweep a directory of docs → chunk them structurally → embed each chunk →
persist to a vector store → expose retrieval as a ``search_knowledge`` tool the
agent discovers via ``find_tools``.

The pipeline doesn't care what it ingests (repo docs, runbooks, postmortems);
the demo ships two corpora — the repo itself (dogfood) and a fictional SRE
knowledge base (``corpora/acme-sre``). See docs/JOURNEY.md.
"""

from __future__ import annotations

from agentic_devops.knowledge.chunking import Chunk, chunk_markdown
from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.retrieval import build_search_knowledge_tool
from agentic_devops.knowledge.store import PgVectorStore, StoredChunk, VectorStore

__all__ = [
    "Chunk",
    "chunk_markdown",
    "Embedder",
    "build_search_knowledge_tool",
    "PgVectorStore",
    "StoredChunk",
    "VectorStore",
]
