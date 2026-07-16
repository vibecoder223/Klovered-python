"""Hybrid retrieval: query expansion -> dense (pgvector, mistral-embed) +
sparse (BM25) -> merge/dedup -> top-K. Port of lib/retrieval.ts.

Runs on the admin (BYPASSRLS) connection like the rest of the worker pipeline
(agents.py/jobs.py) — match_chunks is SECURITY DEFINER, so the org_id passed
into it and into the sparse query IS the isolation boundary, not RLS. Every
caller must pass the verified caller's own org_id, never a client-supplied one.
"""

import math
import os
import re
from dataclasses import dataclass

from .. import db
from .embeddings import embed_texts, has_embeddings
from .llm import MODEL, call_mistral_json, has_llm_key

# Calibrated to mistral-embed cosine similarity: genuinely relevant passages
# on this corpus commonly score 0.65-0.95; unrelated chunks fall below 0.5.
# 0.55 keeps real matches while filtering noise.
NO_SOURCE_THRESHOLD = 0.55

_DENSE_MATCH_COUNT = 20
_SPARSE_CANDIDATE_LIMIT = 200
_BM25_K1 = 1.5
_BM25_B = 0.75


@dataclass
class Candidate:
    chunk_id: str
    text: str
    section_path: str | None
    page_start: int | None
    page_end: int | None
    document_filename: str
    score: float  # 0..1 — cosine similarity (dense) or normalized BM25 (sparse)


@dataclass
class QueryExpansion:
    paraphrases: list[str]
    keywords: list[str]


@dataclass
class RetrievalResult:
    candidates: list[Candidate]
    top_score: float
    query_expansion: QueryExpansion | None
    usage: dict  # {"input_tokens": int, "output_tokens": int}


def is_no_source(top_score: float, candidate_count: int) -> bool:
    return candidate_count == 0 or top_score < NO_SOURCE_THRESHOLD


async def retrieve_for_query(org_id: str, query: str, top_k: int = 6) -> RetrievalResult:
    usage = {"input_tokens": 0, "output_tokens": 0}

    # 1. Query expansion. Off by default — costs an extra LLM call per question
    # and the recall gain is small; embeddings already capture paraphrase
    # similarity. Set RAG_USE_QUERY_EXPANSION=1 to re-enable.
    expansion: QueryExpansion | None = None
    if os.getenv("RAG_USE_QUERY_EXPANSION") == "1" and has_llm_key():
        try:
            data, call_usage = await call_mistral_json(
                system=(
                    "You expand RFP requirement queries for retrieval.\n"
                    'Return JSON:\n{ "paraphrases": [<2 short paraphrases of the requirement>],\n'
                    '  "keywords":    [<5 likely keywords or phrases that would appear in a relevant past document>] }\n'
                    "No prose, no fences."
                ),
                user=query,
                max_tokens=400,
                # Quality model — the fast model returns schema descriptors
                # instead of real data under json_object mode.
                model=MODEL,
            )
            if isinstance(data, dict) and isinstance(data.get("paraphrases"), list) and isinstance(
                data.get("keywords"), list
            ):
                expansion = QueryExpansion(
                    paraphrases=data["paraphrases"][:2], keywords=data["keywords"][:5]
                )
            usage["input_tokens"] += call_usage["input_tokens"]
            usage["output_tokens"] += call_usage["output_tokens"]
        except Exception:  # noqa: BLE001 — non-fatal, proceed with the bare query
            pass

    # 2. Dense retrieval (only if an embedding provider is configured).
    dense: list[Candidate] = []
    if has_embeddings():
        queries = [query, *(expansion.paraphrases if expansion else [])]
        embeds = await embed_texts(queries, "query")
        dense_map: dict[str, Candidate] = {}
        for e in embeds:
            for c in _dense_search(org_id, e, _DENSE_MATCH_COUNT):
                existing = dense_map.get(c.chunk_id)
                if not existing or c.score > existing.score:
                    dense_map[c.chunk_id] = c
        dense = list(dense_map.values())

    # 3. Sparse retrieval (BM25), in-memory over the workspace's candidate
    # chunks — the corpus per workspace is small in v1.
    keywords = expansion.keywords if expansion else _extract_keywords(query)
    sparse = _sparse_search(org_id, keywords, top_k=20)

    candidates = _merge_rank(dense, sparse, top_k)
    return RetrievalResult(
        candidates=candidates,
        top_score=candidates[0].score if candidates else 0.0,
        query_expansion=expansion,
        usage=usage,
    )


async def retrieve_for_queries(
    org_id: str,
    queries: list[str],
    top_k: int = 6,
    embeddings: list[list[float]] | None = None,
) -> list[RetrievalResult]:
    """Batched retrieval for many queries at once.

    The only rate-limited step in retrieval is query embedding (dense
    match_chunks and sparse BM25 are DB-only). Embedding every query in one
    embed_texts call (which internally batches + gates) collapses ~N network
    calls to ~1, instead of one embed call per question. Query expansion is
    intentionally skipped here — per-query LLM expansion would defeat the
    batching and is off by default anyway.

    Results are aligned 1:1 with the input `queries` order. Pass
    `embeddings` to reuse embeddings the caller already computed (e.g. a
    prior library lookup) instead of embedding the same texts twice.
    """
    if not queries:
        return []

    embeds = embeddings or []
    if not embeds and has_embeddings():
        try:
            embeds = await embed_texts(queries, "query")
        except Exception:  # noqa: BLE001 — degrade to sparse-only on embed failure
            embeds = []

    results: list[RetrievalResult] = []
    for i, query in enumerate(queries):
        dense: list[Candidate] = []
        emb = embeds[i] if i < len(embeds) else None
        if emb:
            dense = _dense_search(org_id, emb, _DENSE_MATCH_COUNT)

        sparse = _sparse_search(org_id, _extract_keywords(query), top_k=20)
        candidates = _merge_rank(dense, sparse, top_k)
        results.append(
            RetrievalResult(
                candidates=candidates,
                top_score=candidates[0].score if candidates else 0.0,
                query_expansion=None,
                usage={"input_tokens": 0, "output_tokens": 0},
            )
        )
    return results


# ---------- merge / rank ----------


def _merge_rank(dense: list[Candidate], sparse: list[Candidate], top_k: int) -> list[Candidate]:
    merged: dict[str, Candidate] = {}
    for c in [*dense, *sparse]:
        if c.chunk_id not in merged:
            merged[c.chunk_id] = c
    ranked = sorted(merged.values(), key=lambda c: c.score, reverse=True)
    return ranked[:top_k]


# ---------- dense (pgvector) ----------


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


def _dense_search(org_id: str, embedding: list[float], match_count: int) -> list[Candidate]:
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT * FROM match_chunks(%s, %s::vector, %s)",
            (org_id, _vector_literal(embedding), match_count),
        )
        rows = cur.fetchall()
    return [
        Candidate(
            chunk_id=str(r["chunk_id"]),
            text=r["text"],
            section_path=r["section_path"],
            page_start=r["page_start"],
            page_end=r["page_end"],
            document_filename=r["document_filename"],
            score=r["similarity"],
        )
        for r in rows
    ]


# ---------- sparse (BM25) ----------


def _extract_keywords(q: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9\s\-]", " ", q.lower())
    return [t for t in cleaned.split() if len(t) >= 3][:8]


def _fetch_sparse_candidates(org_id: str, terms: list[str]) -> list[dict]:
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT c.id, c.raw_text, c.cleaned_text, c.section_path, c.page_start, c.page_end, "
            "c.sparse_terms, coalesce(d.filename, '(unknown)') AS document_filename "
            "FROM document_chunks c LEFT JOIN documents d ON d.id = c.document_id "
            "WHERE c.org_id = %s AND c.sparse_terms && %s LIMIT %s",
            (org_id, terms, _SPARSE_CANDIDATE_LIMIT),
        )
        return cur.fetchall()


def _sparse_search(org_id: str, keywords: list[str], top_k: int) -> list[Candidate]:
    terms = _terms_from_keywords(keywords)
    if not terms:
        return []
    rows = _fetch_sparse_candidates(org_id, terms)
    if not rows:
        return []
    return _score_bm25(rows, terms)[:top_k]


def _terms_from_keywords(keywords: list[str]) -> list[str]:
    terms: list[str] = []
    for k in keywords:
        for word in k.lower().split():
            cleaned = re.sub(r"[^a-z0-9\-]", "", word)
            if len(cleaned) >= 3:
                terms.append(cleaned)
    return terms


def _score_bm25(rows: list[dict], terms: list[str]) -> list[Candidate]:
    """BM25-score `rows` (each with a `sparse_terms` list) against `terms`.

    Pure function — no DB access — so it's directly unit-testable.
    """
    n = len(rows)
    term_set = set(terms)
    doc_freq: dict[str, int] = {}
    for r in rows:
        row_terms = set(r.get("sparse_terms") or [])
        for term in term_set:
            if term in row_terms:
                doc_freq[term] = doc_freq.get(term, 0) + 1

    lengths = [len(r.get("sparse_terms") or []) for r in rows]
    avgdl = (sum(lengths) / n) if n else 0.0

    scored: list[Candidate] = []
    for r, dl in zip(rows, lengths):
        tf: dict[str, int] = {}
        for term in r.get("sparse_terms") or []:
            tf[term] = tf.get(term, 0) + 1

        score = 0.0
        for term in terms:
            f = tf.get(term)
            if not f:
                continue
            df = doc_freq.get(term) or 0.5
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 0))
            score += idf * ((f * (_BM25_K1 + 1)) / denom)

        norm = min(1.0, score / 12)
        if norm <= 0:
            continue
        scored.append(
            Candidate(
                chunk_id=str(r["id"]),
                text=r.get("cleaned_text") or r.get("raw_text") or "",
                section_path=r.get("section_path"),
                page_start=r.get("page_start"),
                page_end=r.get("page_end"),
                document_filename=r.get("document_filename") or "(unknown)",
                score=norm,
            )
        )
    return sorted(scored, key=lambda c: c.score, reverse=True)
