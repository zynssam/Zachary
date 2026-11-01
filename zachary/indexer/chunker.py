"""Split extracted text into overlapping chunks for embedding and passages.

Chunks end at a paragraph or sentence boundary when one is near, so passages
shown to the user (Phase 6 Q&A) read naturally. Offsets refer to the extracted
text, not the raw file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHUNK_CHARS = 1000
OVERLAP_CHARS = 120
MAX_CHUNKS = 64  # with embed_max_kb=64 this is never reached by normal text

_BREAKS = (re.compile(r"\n\s*\n"), re.compile(r"(?<=[.!?])\s+"), re.compile(r"\n"))


@dataclass(frozen=True, slots=True)
class Chunk:
    ord: int
    start: int
    end: int
    text: str


def _cut(text: str, start: int, limit: int) -> int:
    """End index for a chunk starting at ``start``: the last good break before ``limit``."""
    if limit >= len(text):
        return len(text)
    window = text[start:limit]
    floor = len(window) // 2  # don't make chunks smaller than half the target
    for pattern in _BREAKS:
        ends = [m.end() for m in pattern.finditer(window) if m.end() > floor]
        if ends:
            return start + ends[-1]
    space = window.rfind(" ")
    return start + space if space > floor else limit


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[Chunk]:
    # Offsets refer to ``text`` as given: skip outer whitespace without stripping.
    stop = len(text.rstrip())
    start = len(text) - len(text.lstrip())
    if start >= stop:
        return []
    text = text[:stop]
    chunks: list[Chunk] = []
    while start < len(text) and len(chunks) < MAX_CHUNKS:
        end = _cut(text, start, start + size)
        body = text[start:end].strip()
        if body:
            chunks.append(Chunk(len(chunks), start, end, body))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        # Start the next chunk at a word boundary.
        while start < end and not text[start - 1].isspace():
            start += 1
    return chunks
