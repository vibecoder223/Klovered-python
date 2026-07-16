"""Stateless parse probe + real document upload.

- ``POST /api/pipeline/parse`` parses an uploaded file in memory and returns the
  block structure plus a server-side timing — used for speed benchmarking of the
  Python parser without touching the database.
- ``POST /api/pipeline/documents/upload`` is the real port of the TS upload
  route: enforces the one-RFP-per-session cap, writes to Supabase Storage, and
  inserts the ``documents`` row under the guest's RLS context.
"""

import re
import time

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from ..deps import GuestContext, require_guest
from ..pipeline.parse import parse_document
from ..supabase_rest import StorageClient, user_client

router = APIRouter(prefix="/api/pipeline", tags=["documents"])


@router.post("/parse")
async def parse_probe(
    file: UploadFile = File(...),
    ctx: GuestContext = Depends(require_guest),
) -> dict:
    data = await file.read()
    start = time.perf_counter()
    parsed = parse_document(data, file.content_type, file.filename or "upload")
    elapsed_ms = (time.perf_counter() - start) * 1000
    return {
        "filename": file.filename,
        "bytes": len(data),
        "page_count": parsed.page_count,
        "block_count": len(parsed.blocks),
        "chars": len(parsed.raw_text),
        "parse_ms": round(elapsed_ms, 1),
        "blocks_preview": [b.to_dict() for b in parsed.blocks[:5]],
    }


@router.post("/documents/upload")
async def documents_upload(
    file: UploadFile = File(...),
    deal_id: str = Form(...),
    ctx: GuestContext = Depends(require_guest),
):
    supabase = user_client(ctx.token)

    deal = supabase.get("deals", {"select": "id,org_id", "id": f"eq.{deal_id}", "limit": "1"})
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Deal not found"})

    existing = supabase.get("documents", {"select": "id", "deal_id": f"eq.{deal_id}"})
    if len(existing) >= 1:
        return JSONResponse(
            status_code=403,
            content={"error": "Free limit: one RFP per session. Delete the current one first."},
        )

    filename = file.filename or "upload"
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    object_path = f"{deal_id}/{int(time.time() * 1000)}-{safe_name}"
    data = await file.read()

    storage = StorageClient()
    try:
        storage.upload("documents", object_path, data, file.content_type or "")
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"upload failed: {e}"})

    try:
        inserted = supabase.insert(
            "documents",
            [
                {
                    "deal_id": deal_id,
                    "filename": filename,
                    "file_path": object_path,
                    "file_size": len(data),
                    "mime_type": file.content_type or None,
                    "processing_status": "uploaded",
                }
            ],
        )
    except Exception as e:  # noqa: BLE001
        storage.remove("documents", object_path)
        return JSONResponse(status_code=500, content={"error": f"insert failed: {e}"})

    return {"document": inserted[0] if inserted else None}
