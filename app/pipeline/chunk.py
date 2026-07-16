"""Token-aware chunker over parsed blocks. Port of lib/chunk.ts.

Produces 400-600 token chunks that never split mid-paragraph and never split a
list across chunks. Carries section_path and page_start/end.
"""

import re
from dataclasses import dataclass, field

from .parse import Block

TARGET_MIN = 400
TARGET_MAX = 600
CHAR_PER_TOKEN = 4

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with", "at", "by",
    "is", "are", "was", "were", "be", "been", "being", "this", "that", "these", "those",
    "it", "its", "as", "from", "into", "than", "then", "so", "such", "not", "no", "do",
    "does", "did", "done", "has", "have", "had", "will", "would", "should", "could", "may",
    "might", "must", "can", "shall", "we", "you", "they", "i", "he", "she", "our", "your",
    "their", "my", "his", "her", "us", "them", "also", "more", "most", "any", "all", "each",
}


@dataclass
class ProducedChunk:
    text: str
    text_for_embedding: str
    section_path: str
    page_start: int
    page_end: int
    sparse_terms: list[str]


def _approx_tokens(s: str) -> int:
    return -(-len(s) // CHAR_PER_TOKEN)


def _tokenize_for_sparse(text: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9\s\-]", " ", text.lower())
    toks = [t for t in cleaned.split() if len(t) >= 3 and t not in _STOPWORDS]
    seen: set[str] = set()
    out: list[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:200]


@dataclass
class _Accum:
    parts: list[str] = field(default_factory=list)
    section_path: str = ""
    page_start: int = 1
    page_end: int = 1
    tokens: int = 0
    in_list: bool = False


def chunk_blocks(blocks: list[Block], filename: str) -> list[ProducedChunk]:
    heading_stack: list[tuple[int, str]] = []
    chunks: list[ProducedChunk] = []
    acc: _Accum | None = None

    def section_path() -> str:
        return " > ".join(h[1] for h in heading_stack)

    def push():
        nonlocal acc
        if acc is None:
            return
        text = "\n".join(acc.parts).strip()
        section = acc.section_path
        page_start, page_end = acc.page_start, acc.page_end
        acc = None
        if not text:
            return
        header = f"[{filename} > {section or 'Body'}, p.{page_start}]"
        chunks.append(
            ProducedChunk(
                text=text,
                text_for_embedding=f"{header}\n{text}",
                section_path=section or "Body",
                page_start=page_start,
                page_end=page_end,
                sparse_terms=_tokenize_for_sparse(text),
            )
        )

    def ensure(page: int):
        nonlocal acc
        if acc is not None:
            return
        acc = _Accum(section_path=section_path(), page_start=page, page_end=page)

    for b in blocks:
        if b.type == "heading":
            level = b.level or 1
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, b.text))
            push()
            continue

        is_list = b.type == "list_item"
        segment = f"• {b.text}" if is_list else b.text
        seg_tok = _approx_tokens(segment)

        if acc and acc.in_list and not is_list and acc.tokens >= TARGET_MIN:
            push()

        ensure(b.page)
        assert acc is not None
        if not acc.section_path:
            acc.section_path = section_path()

        if acc.tokens + seg_tok > TARGET_MAX and acc.tokens >= TARGET_MIN:
            push()
            ensure(b.page)
            assert acc is not None

        acc.parts.append(segment)
        acc.tokens += seg_tok
        acc.page_end = max(acc.page_end, b.page)
        acc.in_list = is_list

        if acc.tokens > TARGET_MAX * 1.5:
            push()

    push()
    return chunks
