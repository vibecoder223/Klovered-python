"""Knowledge-base documents — upload, list, delete. Port of
app/api/knowledge/{route,upload,[id]}.ts.

This is the corpus answers are drawn from. Uploads run ingestion inline (parse
-> chunk -> embed -> store) rather than fire-and-forget: the client shows an
upload progress UI, and a detached task was what previously left documents
stuck on STAGE:parsing forever.
"""

import re
import time

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .. import db, storage
from ..config import get_settings
from ..deps import GuestContext, require_guest
from ..pipeline.ingest import KnowledgeDoc, ingest_knowledge_document

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# Free-tier caps, mirroring the TS route.
MAX_DOCS = 10
MAX_TOTAL_PAGES = 200
_DOC_TYPES = {"past_proposal", "security_doc", "policy", "other"}


@router.get("")
async def list_knowledge(ctx: GuestContext = Depends(require_guest)) -> dict:
    with db.user_tx(ctx.user_id) as cur:
        cur.execute(
            "SELECT id, filename, doc_type, ingestion_status, page_count, file_size, "
            "created_at, error_message FROM knowledge_documents "
            "WHERE org_id = %s ORDER BY created_at DESC",
            (ctx.org_id,),
        )
        rows = cur.fetchall()
    return {
        "knowledge_documents": [
            {
                "id": str(r["id"]),
                "filename": r["filename"],
                "doc_type": r["doc_type"],
                "ingestion_status": r["ingestion_status"],
                "page_count": r["page_count"],
                "file_size": r["file_size"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "error_message": r["error_message"],
            }
            for r in rows
        ]
    }


@router.post("/upload")
async def upload_knowledge(
    file: UploadFile = File(...),
    doc_type: str = Form("other"),
    ctx: GuestContext = Depends(require_guest),
):
    data = await file.read()
    if len(data) > get_settings().max_upload_bytes:
        return JSONResponse(status_code=413, content={"error": "File too large (max 20 MB)"})

    # Enforce the free-tier caps on the RLS connection — a caller can only ever
    # count their own org's rows.
    with db.user_tx(ctx.user_id) as cur:
        cur.execute(
            "SELECT count(*) AS n, coalesce(sum(page_count), 0) AS pages "
            "FROM knowledge_documents WHERE org_id = %s",
            (ctx.org_id,),
        )
        stats = cur.fetchone()
    if stats["n"] >= MAX_DOCS:
        return JSONResponse(
            status_code=403,
            content={"error": f"Free limit: {MAX_DOCS} documents. Sign in to add more."},
        )
    if stats["pages"] >= MAX_TOTAL_PAGES:
        return JSONResponse(
            status_code=403,
            content={"error": f"Free limit: {MAX_TOTAL_PAGES} pages total. Sign in for more."},
        )

    filename = file.filename or "upload"
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    object_path = f"{ctx.org_id}/{int(time.time() * 1000)}-{safe_name}"
    storage.save(object_path, data)

    try:
        with db.user_tx(ctx.user_id) as cur:
            cur.execute(
                "INSERT INTO knowledge_documents (org_id, filename, file_path, file_size, "
                "mime_type, doc_type, ingestion_status, uploaded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s) RETURNING id",
                (
                    ctx.org_id,
                    filename,
                    object_path,
                    len(data),
                    file.content_type,
                    doc_type if doc_type in _DOC_TYPES else "other",
                    ctx.user_id,
                ),
            )
            kdoc_id = str(cur.fetchone()["id"])
    except Exception as e:  # noqa: BLE001 — don't leave an orphaned file behind
        storage.delete(object_path)
        return JSONResponse(status_code=500, content={"error": f"insert failed: {e}"})

    # Ingest inline so the row can't be left stranded mid-stage.
    try:
        result = await ingest_knowledge_document(
            KnowledgeDoc(
                id=kdoc_id,
                org_id=ctx.org_id,
                filename=filename,
                file_path=object_path,
                mime_type=file.content_type,
            )
        )
    except Exception as e:  # noqa: BLE001 — surface the failure on the row
        with db.admin_tx() as cur:
            cur.execute(
                "UPDATE knowledge_documents SET ingestion_status = 'failed', error_message = %s "
                "WHERE id = %s",
                (str(e)[:1000], kdoc_id),
            )
        return JSONResponse(status_code=500, content={"error": str(e)})

    return {
        "knowledge_document": {
            "id": kdoc_id,
            "filename": filename,
            "doc_type": doc_type if doc_type in _DOC_TYPES else "other",
            "ingestion_status": "ready",
            "chunk_count": result.chunk_count,
            "page_count": result.page_count,
            "dedup": result.dedup,
        }
    }


@router.get("/{kdoc_id}")
async def get_knowledge(kdoc_id: str, ctx: GuestContext = Depends(require_guest)):
    with db.user_tx(ctx.user_id) as cur:
        cur.execute(
            "SELECT id, ingestion_status, error_message, page_count "
            "FROM knowledge_documents WHERE id = %s LIMIT 1",
            (kdoc_id,),
        )
        row = cur.fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return {
        "id": str(row["id"]),
        "ingestion_status": row["ingestion_status"],
        "error_message": row["error_message"],
        "page_count": row["page_count"],
    }


@router.delete("/{kdoc_id}")
async def delete_knowledge(kdoc_id: str, ctx: GuestContext = Depends(require_guest)):
    # RLS scopes the select+delete to the caller's org, so a foreign id simply
    # resolves to nothing rather than deleting someone else's document.
    with db.user_tx(ctx.user_id) as cur:
        cur.execute("SELECT file_path FROM knowledge_documents WHERE id = %s LIMIT 1", (kdoc_id,))
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        # document_chunks cascade via the FK.
        cur.execute("DELETE FROM knowledge_documents WHERE id = %s", (kdoc_id,))

    storage.delete(row["file_path"])
    return {"ok": True}
