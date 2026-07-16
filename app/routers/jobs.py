"""Job-queue drain endpoint — the worker entrypoint. Port of
app/api/jobs/drain/route.ts.

A scheduler (systemd timer / cron) hits this on an interval as the recovery
net. It's also kicked in-process (no HTTP hop needed — this isn't serverless)
right after a document is queued, via documents.py's background task.
"""

from fastapi import APIRouter, Header, HTTPException

from ..config import get_settings
from ..pipeline.jobs import drain_once

router = APIRouter(prefix="/api/pipeline", tags=["jobs"])


@router.post("/jobs/drain")
async def drain(x_cron_secret: str = Header(default="")) -> dict:
    secret = get_settings().cron_secret
    if not secret:
        raise HTTPException(status_code=503, detail="CRON_SECRET not configured")
    if x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await drain_once()
