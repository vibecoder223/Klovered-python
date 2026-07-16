from fastapi import APIRouter, Depends

from ..deps import GuestContext, require_guest

router = APIRouter(prefix="/api/pipeline", tags=["probe"])


@router.get("/whoami")
async def whoami(ctx: GuestContext = Depends(require_guest)) -> dict:
    return {"user_id": ctx.user_id, "org_id": ctx.org_id, "is_anonymous": ctx.is_anonymous}
