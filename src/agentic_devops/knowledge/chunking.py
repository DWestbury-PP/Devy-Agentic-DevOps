"""Structural Markdown chunking.

Split on Markdown headings to keep semantically-coherent sections together, then
size-cap each section (with overlap) so chunks fit comfortably in an embedding
window. Every chunk carries its ``heading_path`` (e.g. "Runbook > Mitigation")
so retrieval can cite *where* in a doc an answer came from.

This is the simple-but-good version. The seam for smarter semantic chunking is
the ``chunk_markdown`` signature — swap the implementation without touching the
store, embedder, or ingest pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")  # fenced code block delimiter


@dataclass
class Chunk:
    """One unit of retrievable text plus the metadata that lets us cite it."""

    text: str
    heading_path: str = ""  # "H1 > H2 > H3" trail to this chunk's section
    index: int = 0  # position within the source document
    extra: dict = field(default_factory=dict)


def _split_oversized(body: str, max_chars: int, overlap: int) -> list[str]:
    """Window a too-long section on paragraph boundaries, with char overlap."""
    body = body.strip()
    if len(body) <= max_chars:
        return [body] if body else []

    paras = re.split(r"\n\s*\n", body)
    windows: list[str] = []
    current = ""
    for para in paras:
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            windows.append(current)
        # A single paragraph longer than the cap: hard-split it.
        if len(para) > max_chars:
            for i in range(0, len(para), max_chars - overlap):
                windows.append(para[i : i + max_chars])
            current = ""
        else:
            current = para
    if current:
        windows.append(current)

    # Stitch a little trailing context from each window onto the next.
    if overlap and len(windows) > 1:
        stitched = [windows[0]]
        for prev, nxt in zip(windows, windows[1:]):
            tail = prev[-overlap:]
            stitched.append(f"{tail}\n\n{nxt}" if not nxt.startswith(tail) else nxt)
        windows = stitched
    return windows


def chunk_markdown(
    text: str, max_chars: int = 8000, overlap: int = 200, split_level: int = 2
) -> list[Chunk]:
    """Chunk Markdown into heading-scoped, size-capped sections.

    Splits only on headings at or above ``split_level`` (default 2 → split on
    ``#``/``##``): each chunk is a whole section, with deeper subsections (``###``
    and below) kept **inline** so related content stays together for embedding.
    The ``heading_path`` is anchored at the split level (e.g. "Runbook >
    Mitigation"); deeper heading lines remain in the chunk text. Chunks are
    variable-sized; ``max_chars`` is only a safety cap (well under the embedding
    token limit) that windows a pathologically long section, with ``overlap``
    carrying a little context across the cut.
    """
    lines = text.splitlines()
    heading_stack: list[tuple[int, str]] = []  # (level, title) for levels <= split_level
    sections: list[tuple[str, str]] = []  # (heading_path, body)
    buffer: list[str] = []
    in_fence = False  # inside a ``` / ~~~ code block — never treat lines as headings

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            path = " > ".join(title for _, title in heading_stack)
            sections.append((path, body))
        buffer.clear()

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            buffer.append(line)  # keep the fence + code in the chunk text
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m and len(m.group(1)) <= split_level:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
        else:
            # Body text — or a deeper (sub-split-level) heading, kept inline so
            # the subsection stays with its parent section.
            buffer.append(line)
    flush()

    chunks: list[Chunk] = []
    idx = 0
    for heading_path, body in sections:
        for window in _split_oversized(body, max_chars, overlap):
            if not window.strip():
                continue
            chunks.append(Chunk(text=window, heading_path=heading_path, index=idx))
            idx += 1
    return chunks
