"""Whoami probe, stateless parse (speed probe), and the real document upload.

Upload runs entirely on the RLS-enforced connection: a guest can only see their
own deal, so a missing scope check cannot leak another tenant's rows — the
database refuses them.
"""

import re
import threading
import time

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .. import db, storage
from ..config import get_settings
from ..deps import GuestContext, require_guest
from ..pipeline import jobs as job_queue
from ..pipeline.llm import has_llm_key
from ..pipeline.parse import parse_document

router = APIRouter(prefix="/api/pipeline", tags=["documents"])

# Concurrency guard: cap simultaneous heavy work so a burst of uploads can't
# stack PDF-parse memory spikes past the box's RAM.
_upload_gate = threading.Semaphore(get_settings().max_concurrent_uploads)


@router.get("/whoami")
async def whoami(ctx: GuestContext = Depends(require_guest)) -> dict:
    return {"user_id": ctx.user_id, "org_id": ctx.org_id, "is_anonymous": ctx.is_anonymous}


@router.post("/parse")
async def parse_probe(
    file: UploadFile = File(...),
    ctx: GuestContext = Depends(require_guest),
) -> dict:
    data = await file.read()
    start = time.perf_counter()
    with _upload_gate:
        parsed = parse_document(data, file.content_type, file.filename or "upload")
    return {
        "filename": file.filename,
        "bytes": len(data),
        "page_count": parsed.page_count,
        "block_count": len(parsed.blocks),
        "chars": len(parsed.raw_text),
        "parse_ms": round((time.perf_counter() - start) * 1000, 1),
    }


@router.post("/documents/upload")
async def documents_upload(
    file: UploadFile = File(...),
    deal_id: str = Form(...),
    ctx: GuestContext = Depends(require_guest),
):
    data = await file.read()
    if len(data) > get_settings().max_upload_bytes:
        return JSONResponse(status_code=413, content={"error": "File too large (max 20 MB)"})

    filename = file.filename or "upload"
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    object_path = f"{deal_id}/{int(time.time() * 1000)}-{safe_name}"

    # One RLS transaction: verify the deal is the caller's, enforce the cap,
    # then insert. RLS makes all three tenant-safe.
    with db.user_tx(ctx.user_id) as cur:
        cur.execute("SELECT id FROM deals WHERE id = %s LIMIT 1", (deal_id,))
        if not cur.fetchone():
            return JSONResponse(status_code=404, content={"error": "Deal not found"})

        cur.execute("SELECT count(*) AS n FROM documents WHERE deal_id = %s", (deal_id,))
        if cur.fetchone()["n"] >= 1:
            return JSONResponse(
                status_code=403,
                content={"error": "Free limit: one RFP per session. Delete the current one first."},
            )

        storage.save(object_path, data)
        try:
            cur.execute(
                "INSERT INTO documents (deal_id, filename, file_path, file_size, mime_type, processing_status) "
                "VALUES (%s, %s, %s, %s, %s, 'uploaded') RETURNING *",
                (deal_id, filename, object_path, len(data), file.content_type),
            )
            doc = cur.fetchone()
        except Exception as e:  # noqa: BLE001 — roll back the orphaned file
            storage.delete(object_path)
            return JSONResponse(status_code=500, content={"error": f"insert failed: {e}"})

    return {"document": {k: str(v) for k, v in doc.items()}}


@router.post("/documents/process")
async def documents_process(
    background_tasks: BackgroundTasks,
    document_id: str = Form(...),
    ctx: GuestContext = Depends(require_guest),
):
    """Enqueue-only. The pipeline runs asynchronously: this just queues the
    first stage and returns immediately. Also serves as "retry": it clears
    prior job rows and re-queues from the top.
    """
    with db.user_tx(ctx.user_id) as cur:
        cur.execute("SELECT id FROM documents WHERE id = %s LIMIT 1", (document_id,))
        if not cur.fetchone():
            return JSONResponse(status_code=404, content={"error": "Not found"})

    if not has_llm_key():
        with db.user_tx(ctx.user_id) as cur:
            cur.execute(
                "UPDATE documents SET processing_status = 'uploaded', error_message = %s WHERE id = %s",
                (
                    "No LLM API key configured. The file is stored, but the AI pipeline is "
                    "disabled until LLM_API_KEY or MISTRAL_API_KEY is set.",
                    document_id,
                ),
            )
        return {"ok": True, "skipped": True, "reason": "llm_key_missing"}

    # Clean slate for (re)processing: drop any prior job rows, then queue ingest.
    with db.admin_tx() as cur:
        cur.execute("DELETE FROM jobs WHERE document_id = %s", (document_id,))
    job_queue.enqueue_ingest(document_id, ctx.org_id)
    with db.user_tx(ctx.user_id) as cur:
        cur.execute(
            "UPDATE documents SET processing_status = 'queued', error_message = NULL WHERE id = %s",
            (document_id,),
        )

    # Kick the drain immediately (in-process — no HTTP hop, unlike the
    # original serverless deployment) so the pipeline starts now instead of
    # waiting for the next scheduled drain tick. The scheduled tick stays as
    # the recovery net.
    background_tasks.add_task(job_queue.drain_once)

    return {"ok": True, "queued": True}
