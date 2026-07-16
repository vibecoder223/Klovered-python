"""Guest session bootstrap — the port of Supabase anon sign-in + /api/session.

``POST /api/auth/guest`` mints a token AND provisions the guest's throwaway
workspace (org + membership + one deal) in a single admin transaction, so the
returned token immediately resolves an org on the request path.
"""

from fastapi import APIRouter

from .. import db
from ..auth import mint_guest_token, new_user_id

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/guest")
async def create_guest() -> dict:
    user_id = new_user_id()
    # Provisioning writes across the tenant boundary (a brand-new guest has no
    # membership yet), so it runs on the admin (BYPASSRLS) connection.
    with db.admin_tx() as cur:
        cur.execute(
            "INSERT INTO users (id, is_anonymous) VALUES (%s, true)", (user_id,)
        )
        cur.execute(
            "INSERT INTO organizations (name, slug) VALUES ('Guest workspace', %s) RETURNING id",
            (f"guest-{user_id}",),
        )
        org_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO team_members (org_id, user_id, role, email, name) "
            "VALUES (%s, %s, 'owner', '', 'Guest')",
            (org_id, user_id),
        )
        cur.execute(
            "INSERT INTO org_settings (org_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (org_id,),
        )
        cur.execute(
            "INSERT INTO deals (org_id, name, status, owner_id) "
            "VALUES (%s, 'Free tool session', 'in_progress', %s) RETURNING id",
            (org_id, user_id),
        )
        deal_id = cur.fetchone()["id"]

    return {
        "access_token": mint_guest_token(user_id),
        "user_id": user_id,
        "org_id": str(org_id),
        "deal_id": str(deal_id),
    }
