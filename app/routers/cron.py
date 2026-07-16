"""Guest-data cleanup cron. Port of app/api/cron/cleanup/route.ts.

Purges guest orgs older than the retention window whose members are ALL still
anonymous — any org where a member upgraded off the guest flow would be
exempt. No such upgrade path exists yet in this self-issued-JWT backend (no
Google OAuth), so today every guest org qualifies once it ages out; the check
stays in place for when one is added, mirroring the TS source.
"""

from fastapi import APIRouter, Header, HTTPException

from .. import db, storage
from ..config import get_settings

router = APIRouter(prefix="/api/cron", tags=["cron"])

_RETENTION_HOURS = 48


def _authorized(x_cron_secret: str, authorization: str) -> bool:
    secret = get_settings().cron_secret
    if not secret:
        return False
    return x_cron_secret == secret or authorization == f"Bearer {secret}"


async def _run_cleanup() -> dict:
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT id, slug FROM organizations WHERE slug LIKE 'guest-%' "
            "AND created_at < now() - make_interval(hours => %s)",
            (_RETENTION_HOURS,),
        )
        orgs = cur.fetchall()

    purged = 0
    skipped_upgraded = 0
    files_removed = 0
    errors: list[str] = []

    for org in orgs:
        org_id = org["id"]
        try:
            with db.admin_tx() as cur:
                cur.execute(
                    "SELECT u.id, u.is_anonymous FROM team_members tm "
                    "JOIN users u ON u.id = tm.user_id WHERE tm.org_id = %s",
                    (org_id,),
                )
                members = cur.fetchall()

            if any(not m["is_anonymous"] for m in members):
                skipped_upgraded += 1
                continue

            with db.admin_tx() as cur:
                cur.execute("SELECT id FROM deals WHERE org_id = %s", (org_id,))
                deals = cur.fetchall()
            for deal in deals:
                files_removed += storage.delete_dir(str(deal["id"]))

            with db.admin_tx() as cur:
                # organizations cascade clears team_members, deals, documents,
                # chunks, questions, responses, citations, agent_runs, jobs.
                # users is NOT a child of organizations, so delete it last —
                # after the cascade has already removed the deals.owner_id
                # references that would otherwise block it.
                cur.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
                for m in members:
                    cur.execute("DELETE FROM users WHERE id = %s", (m["id"],))
            purged += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{org['slug']}: {e}")

    result = {
        "ok": True,
        "scanned": len(orgs),
        "purged": purged,
        "skipped_upgraded": skipped_upgraded,
        "files_removed": files_removed,
    }
    if errors:
        result["errors"] = errors
    return result


# Two authorized entry points, same body:
#   POST — cron / systemd timer / manual curl: shared secret in x-cron-secret.
#   GET  — a scheduler that can only send a bearer token (mirrors the TS
#          Vercel Cron path: Authorization: Bearer $CRON_SECRET).
# A blank CRON_SECRET disables both — never run an unauthenticated purge.


@router.post("/cleanup")
async def cleanup_post(
    x_cron_secret: str = Header(default=""), authorization: str = Header(default="")
) -> dict:
    if not _authorized(x_cron_secret, authorization):
        raise HTTPException(status_code=403, detail="Forbidden")
    return await _run_cleanup()


@router.get("/cleanup")
async def cleanup_get(
    x_cron_secret: str = Header(default=""), authorization: str = Header(default="")
) -> dict:
    if not _authorized(x_cron_secret, authorization):
        raise HTTPException(status_code=403, detail="Forbidden")
    return await _run_cleanup()
