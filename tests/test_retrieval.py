"""Unit tests for retrieval's pure logic (BM25 scoring, merge/rank,
keyword extraction) — no database, always run."""

import inspect

from app.pipeline import retrieval
from app.pipeline.retrieval import (
    Candidate,
    _extract_keywords,
    _merge_rank,
    _score_bm25,
    _terms_from_keywords,
    is_no_source,
)


def test_sparse_search_queries_only_knowledge_base_chunks():
    """Regression guard. Retrieval must ground answers in the org's knowledge
    base (chunks with knowledge_document_id set) — NOT in the RFP being
    answered, whose chunks share document_chunks with document_id set instead.

    An earlier port dropped this filter and joined `documents`, which would
    have answered every RFP question using the RFP's own text as the source.
    """
    sql = inspect.getsource(retrieval._fetch_sparse_candidates)
    assert "knowledge_document_id IS NOT NULL" in sql
    assert "JOIN knowledge_documents" in sql
    # Must not fall back to the RFP's own documents table for the filename.
    assert "JOIN documents" not in sql


def _row(id_, terms, **extra):
    return {"id": id_, "sparse_terms": terms, "cleaned_text": f"text-{id_}", **extra}


def test_extract_keywords_filters_short_words_and_caps_at_eight():
    kws = _extract_keywords("Is an API to be a go to fix now for enterprise auth flows here")
    assert "is" not in kws  # 2 chars, filtered
    assert "an" not in kws  # 2 chars, filtered
    assert "to" not in kws  # 2 chars, filtered
    assert len(kws) <= 8
    assert all(len(k) >= 3 for k in kws)


def test_terms_from_keywords_splits_and_cleans():
    terms = _terms_from_keywords(["Single Sign-On", "RBAC"])
    assert "single" in terms
    assert "rbac" in terms
    # hyphen kept, punctuation stripped
    assert any("sign-on" in t or "sign" in t for t in terms)


def test_score_bm25_ranks_more_relevant_doc_higher():
    rows = [
        _row("a", ["security", "audit", "compliance", "encryption"]),
        _row("b", ["cooking", "recipe", "kitchen"]),
    ]
    terms = ["security", "audit"]
    scored = _score_bm25(rows, terms)
    assert [c.chunk_id for c in scored] == ["a"]
    assert scored[0].score > 0


def test_score_bm25_empty_rows_returns_empty():
    assert _score_bm25([], ["security"]) == []


def test_score_bm25_no_matching_terms_returns_empty():
    rows = [_row("a", ["cooking", "recipe"])]
    assert _score_bm25(rows, ["security"]) == []


def test_is_no_source_below_threshold():
    assert is_no_source(0.3, 5) is True
    assert is_no_source(0.9, 5) is False


def test_is_no_source_no_candidates():
    assert is_no_source(0.0, 0) is True


def test_merge_rank_dedups_keeping_first_and_sorts_desc():
    dense = [
        Candidate("1", "t1", None, None, None, "f.pdf", 0.7),
        Candidate("2", "t2", None, None, None, "f.pdf", 0.9),
    ]
    sparse = [
        Candidate("2", "t2-sparse", None, None, None, "f.pdf", 0.4),  # dup, dense wins (first)
        Candidate("3", "t3", None, None, None, "f.pdf", 0.95),
    ]
    ranked = _merge_rank(dense, sparse, top_k=10)
    assert [c.chunk_id for c in ranked] == ["3", "2", "1"]
    # chunk 2 keeps its dense score/text since dense was listed first
    assert next(c for c in ranked if c.chunk_id == "2").score == 0.9


def test_merge_rank_respects_top_k():
    dense = [Candidate(str(i), "t", None, None, None, "f", score=i / 10) for i in range(10)]
    ranked = _merge_rank(dense, [], top_k=3)
    assert len(ranked) == 3
    assert ranked[0].chunk_id == "9"
