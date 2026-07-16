"""Postgres access — two connection pools mirroring the isolation split.

- ``user_tx(user_id)``  — connects as app_user (NOBYPASSRLS) and sets
  ``app.user_id`` for the transaction, so RLS scopes every row to the caller.
  This is the request path.
- ``admin_tx()`` — connects as the superuser (BYPASSRLS) for provisioning and
  worker code that must write across the tenant boundary (e.g. creating a new
  guest's org before they have any membership).
"""

import contextlib

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pools: dict[str, ConnectionPool] = {}


def _pool(kind: str) -> ConnectionPool:
    if kind not in _pools:
        s = get_settings()
        dsn = s.database_url if kind == "user" else s.admin_database_url
        pool = ConnectionPool(dsn, min_size=1, max_size=5, open=False)
        pool.open()
        _pools[kind] = pool
    return _pools[kind]


@contextlib.contextmanager
def user_tx(user_id: str):
    """Transaction on the RLS-enforced connection, scoped to `user_id`."""
    with _pool("user").connection() as conn:
        # is_local = true → the setting lasts only for this transaction.
        conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur


@contextlib.contextmanager
def admin_tx():
    """Transaction on the RLS-bypassing connection (provisioning / workers)."""
    with _pool("admin").connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur


def close_pools() -> None:
    for pool in _pools.values():
        pool.close()
    _pools.clear()
