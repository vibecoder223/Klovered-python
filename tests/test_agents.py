"""Unit tests for agents' pure logic (requirement parsing/normalization) —
no database, always run."""

from app.pipeline.agents import (
    _batch_user_text,
    _normalize_classification,
    _normalize_topic,
    _parse_requirement,
    _priority_for,
    _validate_requirements,
)
from app.pipeline.chunk import ProducedChunk


def test_normalize_classification_maps_synonyms():
    assert _normalize_classification("must-have") == "must"
    assert _normalize_classification("HIGH") == "must"
    assert _normalize_classification("should") == "should"
    assert _normalize_classification("desired") == "should"
    assert _normalize_classification("optional") == "info"
    assert _normalize_classification("low") == "info"


def test_normalize_classification_defaults_to_must_for_unknown_or_non_string():
    assert _normalize_classification("banana") == "must"
    assert _normalize_classification(None) == "must"
    assert _normalize_classification(42) == "must"


def test_normalize_topic_exact_and_fuzzy_matches():
    assert _normalize_topic("security") == "security"
    assert _normalize_topic("Data Security Review") == "security"
    assert _normalize_topic("compliance") == "legal"
    # Exact match still works even with different casing.
    assert _normalize_topic("Pricing") == "pricing"
    # NOTE: matches lib/agents.ts's fuzzy check exactly — it tests for the
    # substring "price", which "pricing" itself does not contain, so a
    # non-exact fuzzy match like "Pricing model" falls through to the
    # "technical" default in both the TS source and this port.
    assert _normalize_topic("Pricing model") == "technical"
    assert _normalize_topic("Costs") == "pricing"
    assert _normalize_topic("technical architecture") == "technical"


def test_normalize_topic_defaults_to_technical():
    assert _normalize_topic("something else") == "technical"
    assert _normalize_topic(None) == "technical"


def test_priority_for_classification():
    assert _priority_for("must") == "high"
    assert _priority_for("should") == "medium"
    assert _priority_for("info") == "low"


def test_parse_requirement_happy_path():
    req = _parse_requirement(
        {
            "requirement_id": "Q2.3",
            "section": "4.2",
            "text": "Vendor must support SSO.",
            "classification": "must-have",
            "topic": "security",
            "source_page": "7",
        }
    )
    assert req is not None
    assert req.requirement_id == "Q2.3"
    assert req.section == "4.2"
    assert req.classification == "must"
    assert req.topic == "security"
    assert req.source_page == 7


def test_parse_requirement_coerces_numeric_requirement_id_and_section():
    req = _parse_requirement({"requirement_id": 4, "section": 4.2, "text": "text"})
    assert req is not None
    assert req.requirement_id == "4"
    assert req.section == "4.2"


def test_parse_requirement_rejects_missing_text():
    assert _parse_requirement({"requirement_id": "Q1", "text": ""}) is None
    assert _parse_requirement({"requirement_id": "Q1"}) is None


def test_parse_requirement_rejects_missing_requirement_id():
    assert _parse_requirement({"text": "hello"}) is None


def test_parse_requirement_null_source_page_stays_none():
    req = _parse_requirement({"requirement_id": "Q1", "text": "t", "source_page": None})
    assert req is not None
    assert req.source_page is None


def test_parse_requirement_invalid_source_page_falls_back_to_none():
    req = _parse_requirement({"requirement_id": "Q1", "text": "t", "source_page": "not-a-number"})
    assert req is not None
    assert req.source_page is None


def test_validate_requirements_rejects_non_list():
    assert _validate_requirements({"requirement_id": "Q1", "text": "t"}) is None


def test_validate_requirements_rejects_if_any_item_invalid():
    data = [{"requirement_id": "Q1", "text": "t"}, {"text": "missing id"}]
    assert _validate_requirements(data) is None


def test_validate_requirements_accepts_well_formed_array():
    data = [{"requirement_id": "Q1", "text": "t1"}, {"requirement_id": "Q2", "text": "t2"}]
    result = _validate_requirements(data)
    assert result is not None
    assert [r.requirement_id for r in result] == ["Q1", "Q2"]


def _chunk(section_path: str, page_start: int, page_end: int, text: str) -> ProducedChunk:
    return ProducedChunk(
        text=text,
        text_for_embedding=text,
        section_path=section_path,
        page_start=page_start,
        page_end=page_end,
        sparse_terms=[],
    )


def test_batch_user_text_includes_section_and_single_page():
    batch = [_chunk("Intro", 1, 1, "Hello world")]
    out = _batch_user_text(batch)
    assert "Section 1: Intro (page 1)" in out
    assert "Hello world" in out


def test_batch_user_text_includes_page_range_when_spanning_pages():
    batch = [_chunk("Body", 2, 4, "Some text")]
    out = _batch_user_text(batch)
    assert "page 2–4" in out


def test_batch_user_text_defaults_section_to_body_when_empty():
    batch = [_chunk("", 1, 1, "text")]
    out = _batch_user_text(batch)
    assert "Section 1: Body" in out
