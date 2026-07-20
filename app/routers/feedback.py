"""Product feedback — the "how did we do?" card the tool shows once, after an
RFP has been answered.

One endpoint, POST /api/feedback. Works for guests and signed-in accounts alike
(the free tool is guest-first). The row is keyed unique on user_id, so a repeat
submit updates the existing row instead of duplicating — "leave feedback once"
holds server-side even if the client's localStorage guard is cleared.

Each submission also fires a best-effort email notification to
settings.feedback_to_email (default info@klovered.com) via Resend, as a
background task so the response isn't blocked and email trouble never fails the
save.
"""

import html

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
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


async def _notify(rating: int, comment: str, reply_to: str, is_anonymous: bool, org_id: str) -> None:
    """Email the feedback to the team. Best-effort: any failure is swallowed —
    the feedback is already persisted; the email is a courtesy notification."""
    s = get_settings()
    if not s.resend_api_key:
        return
    stars = "★" * rating + "☆" * (5 - rating)
    who = "guest" if is_anonymous else "signed-in account"
    body = {
        "from": s.resend_from,
        "to": [s.feedback_to_email],
        "subject": f"Klovered feedback — {rating}/5",
        "html": (
            f"<h2>New feedback · {html.escape(stars)} ({rating}/5)</h2>"
            f"<p><b>Comment:</b><br>{html.escape(comment).replace(chr(10), '<br>') if comment else '<i>(none)</i>'}</p>"
            f"<p><b>From:</b> {who}</p>"
            f"<p><b>Reply-to:</b> {html.escape(reply_to) if reply_to else '<i>(not provided)</i>'}</p>"
            f"<p style='color:#888'><b>Org:</b> {html.escape(org_id)}</p>"
        ),
    }
    if reply_to and "@" in reply_to:
        body["reply_to"] = reply_to
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {s.resend_api_key}"},
                json=body,
            )
    except httpx.HTTPError:
        pass


@router.post("/feedback")
async def submit_feedback(
    body: FeedbackIn,
    background: BackgroundTasks,
    ctx: GuestContext = Depends(require_guest),
) -> JSONResponse:
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

    # Fire the notification after the row is committed, off the response path.
    background.add_task(_notify, body.rating, comment, email, ctx.is_anonymous, ctx.org_id)
    return JSONResponse(content={"ok": True})
