"""Knowledge-base ingestion: read -> parse -> chunk -> embed -> store.
Port of lib/ingest.ts.

This is the source side of RAG: the org's past proposals, security docs and
policies. Retrieval searches ONLY these chunks (`document_chunks` rows with
`knowledge_document_id` set) — never the RFP being answered.

Runs on the admin (BYPASSRLS) connection like the rest of the pipeline.
"""

import hashlib
from dataclasses import dataclass

from .. import db, storage
from .chunk import chunk_blocks
from .embeddings import embed_texts, has_embeddings
from .parse import parse_document

_CHUNK_INSERT_BATCH = 50


@dataclass
class KnowledgeDoc:
    id: str
    org_id: str
    filename: str
    file_path: str
    mime_type: str | None


@dataclass
class IngestResult:
    chunk_count: int
    page_count: int
    dedup: bool


def _set_stage(kdoc_id: str, stage: str) -> None:
    """Stage updates land in error_message with a STAGE: prefix so the UI can
    poll progress without a schema change (same convention as the TS)."""
    with db.admin_tx() as cur:
        cur.execute(
            "UPDATE knowledge_documents SET ingestion_status = 'processing', "
            "error_message = %s WHERE id = %s",
            (f"STAGE:{stage}", kdoc_id),
        )


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


async def ingest_knowledge_document(doc: KnowledgeDoc) -> IngestResult:
    _set_stage(doc.id, "reading")
    data = storage.read(doc.file_path)

    _set_stage(doc.id, "parsing")
    parsed = parse_document(data, doc.mime_type, doc.filename)
    if not parsed.blocks:
        raise ValueError("No content extracted from document.")

    # Dedup: identical text already ingested for this org is skipped rather
    # than re-chunked (embedding is the expensive part).
    text_hash = hashlib.sha256(parsed.raw_text.encode("utf-8")).hexdigest()
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT id, ingestion_status FROM knowledge_documents "
            "WHERE org_id = %s AND text_hash = %s AND id <> %s LIMIT 1",
            (doc.org_id, text_hash, doc.id),
        )
        existing = cur.fetchone()

    if existing and existing["ingestion_status"] == "ready":
        # Don't write text_hash — the unique (org_id, text_hash) index is
        # already held by the ready row.
        with db.admin_tx() as cur:
            cur.execute(
                "UPDATE knowledge_documents SET ingestion_status = 'ready', page_count = %s, "
                "error_message = %s WHERE id = %s",
                (
                    parsed.page_count,
                    "Deduplicated against a previously ingested document with identical text.",
                    doc.id,
                ),
            )
        return IngestResult(chunk_count=0, page_count=parsed.page_count, dedup=True)

    _set_stage(doc.id, "chunking")
    chunks = chunk_blocks(parsed.blocks, doc.filename)
    if not chunks:
        raise ValueError("Chunker produced 0 chunks (document may be empty).")

    _set_stage(doc.id, "embedding")
    embeddings: list[list[float]] = []
    if has_embeddings():
        embeddings = await embed_texts([c.text_for_embedding for c in chunks], "document")

    _set_stage(doc.id, "storing")
    with db.admin_tx() as cur:
        # Idempotent re-ingest: drop this doc's prior chunks first.
        cur.execute("DELETE FROM document_chunks WHERE knowledge_document_id = %s", (doc.id,))
        rows = [
            (
                doc.id,
                doc.org_id,
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
        for i in range(0, len(rows), _CHUNK_INSERT_BATCH):
            cur.executemany(
                "INSERT INTO document_chunks (knowledge_document_id, org_id, chunk_index, "
                "section_title, section_path, page_start, page_end, raw_text, cleaned_text, "
                "text_for_embedding, embedding, sparse_terms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
                rows[i : i + _CHUNK_INSERT_BATCH],
            )

        cur.execute(
            "UPDATE knowledge_documents SET ingestion_status = 'ready', page_count = %s, "
            "text_hash = %s, error_message = %s WHERE id = %s",
            (
                parsed.page_count,
                text_hash,
                None
                if has_embeddings()
                else "Stored without embeddings — set MISTRAL_API_KEY and re-ingest to enable retrieval.",
                doc.id,
            ),
        )

    return IngestResult(chunk_count=len(chunks), page_count=parsed.page_count, dedup=False)
