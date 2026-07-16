"""Unit tests for rag's pure logic (citation extraction, marker stripping,
batch-response validation) — no database, always run."""

from app.pipeline.rag import (
    _validate_batch_answers,
    _validate_batch_scores,
    extract_citations,
    strip_markers,
)
from app.pipeline.retrieval import Candidate


def _candidate(chunk_id: str, text: str = "some text", **extra) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        text=text,
        section_path=extra.get("section_path"),
        page_start=extra.get("page_start", 1),
        page_end=extra.get("page_end", 1),
        document_filename=extra.get("document_filename", "f.pdf"),
        score=extra.get("score", 0.9),
    )


def test_extract_citations_finds_valid_marker():
    sources = [_candidate("a"), _candidate("b")]
    text = "We support SSO. [c:1] We also encrypt data at rest. [c:2]"
    cited = extract_citations(text, sources)
    assert [c.chunk_id for c in cited] == ["a", "b"]


def test_extract_citations_ignores_out_of_range_marker():
    sources = [_candidate("a")]
    text = "Some claim [c:5]."
    assert extract_citations(text, sources) == []


def test_extract_citations_dedups_repeated_marker():
    sources = [_candidate("a")]
    text = "First [c:1]. Second [c:1] again."
    cited = extract_citations(text, sources)
    assert len(cited) == 1


def test_extract_citations_handles_fullwidth_brackets():
    sources = [_candidate("a")]
    text = "Some claim 【c:1】."
    cited = extract_citations(text, sources)
    assert len(cited) == 1
    assert cited[0].chunk_id == "a"


def test_extract_citations_quote_falls_back_to_source_text_when_no_preceding_sentence():
    sources = [_candidate("a", text="Fallback source text used as quote.")]
    text = "[c:1] leading marker with nothing before it"
    cited = extract_citations(text, sources)
    assert cited[0].quote.startswith("Fallback source text")


def test_strip_markers_removes_citations_and_collapses_whitespace():
    text = "We support SSO. [c:1]   We also encrypt data. [c:2]"
    assert strip_markers(text) == "We support SSO. We also encrypt data."


def test_validate_batch_answers_accepts_well_formed_array():
    data = [{"q": 1, "answer": "A"}, {"q": "2", "answer": "B"}]
    result = _validate_batch_answers(data)
    assert result == {1: "A", 2: "B"}


def test_validate_batch_answers_rejects_non_list():
    assert _validate_batch_answers({"q": 1, "answer": "A"}) is None


def test_validate_batch_answers_rejects_missing_answer_field():
    assert _validate_batch_answers([{"q": 1}]) is None


def test_validate_batch_answers_rejects_non_string_answer():
    assert _validate_batch_answers([{"q": 1, "answer": 123}]) is None


def test_validate_batch_scores_accepts_well_formed_array():
    data = [{"q": 1, "score": 0.7}, {"q": "2", "score": "0.4"}]
    assert _validate_batch_scores(data) == {1: 0.7, 2: 0.4}


def test_validate_batch_scores_rejects_out_of_range_score():
    assert _validate_batch_scores([{"q": 1, "score": 1.5}]) is None


def test_validate_batch_scores_rejects_non_list():
    assert _validate_batch_scores("nope") is None
