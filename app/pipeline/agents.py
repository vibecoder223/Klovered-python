"""Agent pipeline. Port of lib/agents.ts.

Ingestion -> chunking (page-aware) -> requirement extraction -> structuring
(compliance matrix + questions). Response generation is handled separately by
rag.py. Every stage runs on the admin (BYPASSRLS) connection — this is worker
code, not a request path scoped by RLS — and records an `agent_runs` entry
with token usage + estimated cost.
"""

import asyncio
import re
import time
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from .. import db, storage
from .chunk import ProducedChunk, chunk_blocks
from .embeddings import embed_texts, has_embeddings
from .llm import MODEL, call_mistral_json, estimate_cost
from .parse import ParsedDoc, parse_document

_EXTRACTION_BATCH_SIZE = 12

_EXTRACTION_SYSTEM = """You are an expert RFP analyst. Extract every distinct requirement, question, or compliance item from ALL sections provided. Be exhaustive but de-duplicate within the batch.

Return a JSON array. Each item:
{
  "requirement_id": "Q2.3" | "R-4.1" | "REQ-N",
  "section": "4.2" | "Section 4.2 Security",
  "text": "<the full requirement text, paraphrased if needed>",
  "classification": "must" | "should" | "info",
  "topic": "security" | "legal" | "pricing" | "technical" | "commercial",
  "source_page": <integer page number, or null>
}

Return ONLY the JSON array. No prose, no markdown fences."""


@dataclass
class Doc:
    id: str
    deal_id: str
    filename: str
    file_path: str
    mime_type: str | None
    extracted_text: str | None = None


@dataclass
class ExtractedRequirement:
    requirement_id: str
    section: str | None
    text: str
    classification: str
    topic: str
    source_page: int | None


# ---------- bookkeeping ----------


def _set_status(document_id: str, status: str, error_message: str | None = None) -> None:
    with db.admin_tx() as cur:
        cur.execute(
            "UPDATE documents SET processing_status = %s, error_message = %s WHERE id = %s",
            (status, error_message, document_id),
        )


def record_run(
    *,
    document_id: str,
    agent_type: str,
    status: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error_message: str | None = None,
    result: dict | None = None,
    started_at: float,
) -> None:
    cost = (
        estimate_cost(input_tokens, output_tokens)
        if input_tokens is not None and output_tokens is not None
        else None
    )
    with db.admin_tx() as cur:
        cur.execute(
            "INSERT INTO agent_runs (document_id, agent_type, status, input_tokens, output_tokens, "
            "cost, error_message, result, started_at, completed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), now())",
            (
                document_id,
                agent_type,
                status,
                input_tokens,
                output_tokens,
                cost,
                error_message,
                Jsonb(result) if result is not None else None,
                started_at,
            ),
        )


# ============================================================
# Agent 1: Ingestion — read from local storage, parse to typed blocks.
# ============================================================


async def run_ingestion_agent(doc: Doc) -> ParsedDoc:
    started_at = time.time()
    _set_status(doc.id, "extracting")
    try:
        data = storage.read(doc.file_path)
        parsed = parse_document(data, doc.mime_type, doc.filename)
        if not parsed.blocks:
            raise ValueError("No content extracted from this document.")
        with db.admin_tx() as cur:
            cur.execute(
                "UPDATE documents SET extracted_text = %s WHERE id = %s", (parsed.raw_text, doc.id)
            )
        record_run(
            document_id=doc.id,
            agent_type="ingestion",
            status="completed",
            result={"chars": len(parsed.raw_text), "pages": parsed.page_count, "blocks": len(parsed.blocks)},
            started_at=started_at,
        )
        return parsed
    except Exception as e:  # noqa: BLE001
        record_run(
            document_id=doc.id,
            agent_type="ingestion",
            status="failed",
            error_message=str(e),
            started_at=started_at,
        )
        raise


# ============================================================
# Agent 2: Chunking — token-aware, page-aware. Embeds + writes into
# document_chunks.
# ============================================================


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


async def run_chunking_agent(doc: Doc, parsed: ParsedDoc) -> list[ProducedChunk]:
    started_at = time.time()
    _set_status(doc.id, "chunked")
    try:
        chunks = chunk_blocks(parsed.blocks, doc.filename)

        with db.admin_tx() as cur:
            cur.execute("SELECT org_id FROM deals WHERE id = %s", (doc.deal_id,))
            row = cur.fetchone()
        org_id = row["org_id"] if row else None

        embeddings: list[list[float]] = []
        if has_embeddings() and chunks:
            embeddings = await embed_texts([c.text_for_embedding for c in chunks], "document")

        with db.admin_tx() as cur:
            cur.execute("DELETE FROM document_chunks WHERE document_id = %s", (doc.id,))
            if chunks:
                rows = [
                    (
                        doc.id,
                        org_id,
                        i,
                        c.section_path,
                        c.section_path,
                        c.page_start,
                        c.page_end,
                        c.text,
                        c.text,
                        c.text_for_embedding,
                        _vector_literal(embeddings[i]) if embeddings else None,
                        c.sparse_terms,
                    )
                    for i, c in enumerate(chunks)
                ]
                for i in range(0, len(rows), 50):
                    cur.executemany(
                        "INSERT INTO document_chunks (document_id, org_id, chunk_index, section_title, "
                        "section_path, page_start, page_end, raw_text, cleaned_text, text_for_embedding, "
                        "embedding, sparse_terms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
                        rows[i : i + 50],
                    )

        record_run(
            document_id=doc.id,
            agent_type="chunking",
            status="completed",
            result={"chunk_count": len(chunks)},
            started_at=started_at,
        )
        return chunks
    except Exception as e:  # noqa: BLE001
        record_run(
            document_id=doc.id,
            agent_type="chunking",
            status="failed",
            error_message=str(e),
            started_at=started_at,
        )
        raise


# ============================================================
# Agent 3: Requirement extraction (LLM, manually validated, with retry).
# ============================================================


def _normalize_classification(v: object) -> str:
    if not isinstance(v, str):
        return "must"
    s = v.strip().lower()
    if s in ("must", "must-have", "mandatory", "required", "high"):
        return "must"
    if s in ("should", "should-have", "desired", "medium"):
        return "should"
    if s in ("info", "informational", "optional", "low"):
        return "info"
    return "must"


def _normalize_topic(v: object) -> str:
    if not isinstance(v, str):
        return "technical"
    s = v.strip().lower()
    if s in ("security", "legal", "pricing", "technical", "commercial"):
        return s
    if "secur" in s:
        return "security"
    if "legal" in s or "compli" in s:
        return "legal"
    if "price" in s or "cost" in s:
        return "pricing"
    if "tech" in s:
        return "technical"
    return "technical"


def _priority_for(classification: str) -> str:
    if classification == "must":
        return "high"
    if classification == "should":
        return "medium"
    return "low"


def _parse_requirement(item: object) -> ExtractedRequirement | None:
    if not isinstance(item, dict):
        return None
    requirement_id = item.get("requirement_id")
    requirement_id = str(requirement_id).strip() if requirement_id is not None else ""
    if not requirement_id:
        return None
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    section = item.get("section")
    section = None if section in (None, "") else str(section)
    source_page = item.get("source_page")
    if source_page in (None, ""):
        source_page = None
    else:
        try:
            source_page = int(source_page)
        except (TypeError, ValueError):
            source_page = None
    return ExtractedRequirement(
        requirement_id=requirement_id,
        section=section,
        text=text,
        classification=_normalize_classification(item.get("classification")),
        topic=_normalize_topic(item.get("topic")),
        source_page=source_page,
    )


def _validate_requirements(data: object) -> list[ExtractedRequirement] | None:
    if not isinstance(data, list):
        return None
    out: list[ExtractedRequirement] = []
    for item in data:
        req = _parse_requirement(item)
        if req is None:
            return None
        out.append(req)
    return out


def _batch_user_text(batch: list[ProducedChunk]) -> str:
    parts = []
    for i, c in enumerate(batch):
        pages = f"page {c.page_start}"
        if c.page_end != c.page_start:
            pages += f"–{c.page_end}"
        parts.append(f"--- Section {i + 1}: {c.section_path or 'Body'} ({pages}) ---\n{c.text}")
    return "\n\n".join(parts)


async def _run_extraction_batch(batch: list[ProducedChunk]) -> tuple[list[ExtractedRequirement], int, int]:
    user = _batch_user_text(batch)
    parsed: list[ExtractedRequirement] | None = None
    last_err: str | None = None
    in_tok = out_tok = 0
    for attempt in range(3):
        try:
            data, usage = await call_mistral_json(
                system=_EXTRACTION_SYSTEM,
                user=(
                    user
                    if attempt == 0
                    else f"{user}\n\n[Previous attempt failed: {last_err}. Return ONLY a JSON array.]"
                ),
                # Bigger batches need more room for the requirements list; the
                # salvage-truncated-array path in llm.py still recovers a
                # partial list if this cap is hit.
                max_tokens=8192,
                model=MODEL,
                mode="text",
            )
            in_tok += usage["input_tokens"]
            out_tok += usage["output_tokens"]
            validated = _validate_requirements(data)
            if validated is not None:
                parsed = validated
                break
            last_err = "response did not match the expected requirement array shape"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)

    if parsed is None:
        # An API-level failure (rate limit, network, server) should bubble up
        # so the orchestrator marks the doc as failed. A validation-only
        # failure (LLM produced unusable JSON) degrades to empty so a doc
        # that legitimately has no requirements in this batch still proceeds.
        if last_err and re.search(r"rate.?limit|429|^LLM |timeout|abort", last_err, re.IGNORECASE):
            raise RuntimeError(f"Extraction batch failed: {last_err}")
        return [], in_tok, out_tok

    for r in parsed:
        if r.source_page is None:
            r.source_page = batch[0].page_start
    return parsed, in_tok, out_tok


def _persist_requirements(document_id: str, reqs: list[ExtractedRequirement]) -> None:
    with db.admin_tx() as cur:
        cur.execute("DELETE FROM extracted_requirements WHERE document_id = %s", (document_id,))
        if reqs:
            cur.executemany(
                "INSERT INTO extracted_requirements (document_id, requirement_id, title, description, "
                "category, priority, is_mandatory, section, source_page, classification, topic) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        document_id,
                        r.requirement_id,
                        r.text[:120],
                        r.text,
                        r.topic,
                        _priority_for(r.classification),
                        r.classification == "must",
                        r.section,
                        r.source_page,
                        r.classification,
                        r.topic,
                    )
                    for r in reqs
                ],
            )


async def run_extraction_agent(doc: Doc, chunks: list[ProducedChunk]) -> list[ExtractedRequirement]:
    started_at = time.time()
    _set_status(doc.id, "analyzing")
    total_in = 0
    total_out = 0
    try:
        batches = [
            chunks[i : i + _EXTRACTION_BATCH_SIZE] for i in range(0, len(chunks), _EXTRACTION_BATCH_SIZE)
        ]

        # Fire all batches in parallel — the per-model rate gate in llm.py
        # handles RPM/TPM pacing.
        results = await asyncio.gather(*(_run_extraction_batch(b) for b in batches))

        all_reqs: list[ExtractedRequirement] = []
        for reqs, in_tok, out_tok in results:
            total_in += in_tok
            total_out += out_tok
            all_reqs.extend(reqs)

        seen: set[str] = set()
        deduped: list[ExtractedRequirement] = []
        for r in all_reqs:
            key = f"{r.requirement_id}::{r.text[:100]}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)

        _persist_requirements(doc.id, deduped)

        record_run(
            document_id=doc.id,
            agent_type="extraction",
            status="completed",
            input_tokens=total_in,
            output_tokens=total_out,
            result={"count": len(deduped)},
            started_at=started_at,
        )
        return deduped
    except Exception as e:  # noqa: BLE001
        record_run(
            document_id=doc.id,
            agent_type="extraction",
            status="failed",
            input_tokens=total_in,
            output_tokens=total_out,
            error_message=str(e),
            started_at=started_at,
        )
        raise


# ============================================================
# Agent 4: Structuring — build compliance matrix + questions.
# ============================================================


async def run_structuring_agent(doc: Doc, reqs: list[ExtractedRequirement]) -> None:
    started_at = time.time()
    _set_status(doc.id, "structured")
    try:
        with db.admin_tx() as cur:
            cur.execute("DELETE FROM compliance_matrix WHERE document_id = %s", (doc.id,))
            cur.execute("DELETE FROM questions WHERE document_id = %s", (doc.id,))
            if reqs:
                cur.executemany(
                    "INSERT INTO compliance_matrix (document_id, requirement_id, our_capability, "
                    "compliance_status) VALUES (%s, %s, NULL, 'pending')",
                    [(doc.id, r.requirement_id) for r in reqs],
                )
                cur.executemany(
                    "INSERT INTO questions (document_id, requirement_id, question_text, category, "
                    "priority, status) VALUES (%s, %s, %s, %s, %s, 'todo')",
                    [
                        (doc.id, r.requirement_id, r.text, r.topic, _priority_for(r.classification))
                        for r in reqs
                    ],
                )

        record_run(
            document_id=doc.id,
            agent_type="structuring",
            status="completed",
            result={"count": len(reqs)},
            started_at=started_at,
        )
    except Exception as e:  # noqa: BLE001
        record_run(
            document_id=doc.id,
            agent_type="structuring",
            status="failed",
            error_message=str(e),
            started_at=started_at,
        )
        raise
