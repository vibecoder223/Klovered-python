"""Whoami probe, stateless parse (speed probe), and the real document upload.

Upload runs entirely on the RLS-enforced connection: a guest can only see their
own deal, so a missing scope check cannot leak another tenant's rows — the
database refuses them.
"""

import re
import threading
import time

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .. import db, storage
from ..config import get_settings
from ..deps import GuestContext, require_guest
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
