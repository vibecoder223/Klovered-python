"""Product feedback — the "how did we do?" card the tool shows once, after an
RFP has been answered.

One endpoint, POST /api/feedback. Works for guests and signed-in accounts alike
(the free tool is guest-first). The row is keyed unique on user_id, so a repeat
submit updates the existing row instead of duplicating — "leave feedback once"
holds server-side even if the client's localStorage guard is cleared.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import db
from ..deps import GuestContext, require_guest

router = APIRouter(prefix="/api", tags=["feedback"])

_MAX_COMMENT = 2000
_MAX_EMAIL = 320


class FeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = ""
    email: str = ""
    # Which run this was about. Advisory and validated against the caller's own
    # deals below, so a forged id can't attach feedback to someone else's deal.
    deal_id: str | None = None


@router.post("/feedback")
async def submit_feedback(body: FeedbackIn, ctx: GuestContext = Depends(require_guest)) -> JSONResponse:
    comment = (body.comment or "").strip()[:_MAX_COMMENT]
    email = (body.email or "").strip()[:_MAX_EMAIL]

    with db.user_tx(ctx.user_id) as cur:
        deal_id = None
        if body.deal_id:
            # RLS scopes this to the caller's org; a foreign deal_id just resolves
            # to None rather than linking (or leaking) across tenants.
            cur.execute("SELECT id FROM deals WHERE id = %s LIMIT 1", (body.deal_id,))
            row = cur.fetchone()
            if row:
                deal_id = row["id"]

        cur.execute(
            "INSERT INTO feedback (org_id, user_id, deal_id, rating, comment, email) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "  rating = excluded.rating, comment = excluded.comment, "
            "  email = excluded.email, deal_id = excluded.deal_id, updated_at = now()",
            (ctx.org_id, ctx.user_id, deal_id, body.rating, comment, email),
        )

    return JSONResponse(content={"ok": True})
