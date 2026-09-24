"""Text chunking with overlap, preferring paragraph/sentence boundaries.

Works for both space-delimited and CJK text (no whitespace between sentences).
"""

from __future__ import annotations

import re

_BREAKS = "。！？!?；;\n"
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 80) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            window = text[start:end]
            cut = window.rfind("\n\n")
            if cut < int(chunk_size * 0.3):
                for m in _SENT_SPLIT.finditer(window):
                    if m.end() >= int(chunk_size * 0.5):
                        cut = m.end() - 1
                        break
                else:
                    cut = window.rfind(" ")
                    if cut < int(chunk_size * 0.3):
                        cut = -1
            if cut and cut > 0:
                end = start + cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks
