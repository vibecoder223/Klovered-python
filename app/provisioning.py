"""Workspace provisioning — shared by every account path (guest, password
signup, Google). One place so the three flows can't drift apart.

All functions take an admin (BYPASSRLS) cursor: a brand-new user has no
membership yet, so RLS would refuse these writes.
"""


def provision_workspace(cur, user_id: str, org_name: str, slug: str, deal_name: str) -> tuple:
    """Create the org + owner membership + first deal for a user."""
    cur.execute(
        "INSERT INTO organizations (name, slug) VALUES (%s, %s) RETURNING id", (org_name, slug)
    )
    org_id = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO team_members (org_id, user_id, role, email, name) "
        "VALUES (%s, %s, 'owner', '', 'Guest')",
        (org_id, user_id),
    )
    cur.execute("INSERT INTO org_settings (org_id) VALUES (%s) ON CONFLICT DO NOTHING", (org_id,))
    cur.execute(
        "INSERT INTO deals (org_id, name, status, owner_id) "
        "VALUES (%s, %s, 'in_progress', %s) RETURNING id",
        (org_id, deal_name, user_id),
    )
    return org_id, cur.fetchone()["id"]


def first_workspace(cur, user_id: str) -> tuple:
    """The user's org + most recent deal, for a login/callback response.
    Returns (None, None) if the user has no membership (e.g. workspace purged)."""
    cur.execute("SELECT org_id FROM team_members WHERE user_id = %s LIMIT 1", (user_id,))
    row = cur.fetchone()
    if not row:
        return None, None
    org_id = row["org_id"]
    cur.execute(
        "SELECT id FROM deals WHERE org_id = %s ORDER BY created_at DESC LIMIT 1", (org_id,)
    )
    deal = cur.fetchone()
    return org_id, (deal["id"] if deal else None)
