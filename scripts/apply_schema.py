"""Apply db/schema.sql to the admin database. Idempotent (all CREATE ... IF NOT
EXISTS / OR REPLACE). Used by CI and manual bootstraps.

    python scripts/apply_schema.py
"""

import os
import pathlib

import psycopg
from psycopg import sql

from app.config import get_settings


def main() -> None:
    schema_sql = pathlib.Path("db/schema.sql").read_text(encoding="utf-8")
    dsn = get_settings().admin_database_url
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute(schema_sql)
        # Set app_user's password from the environment so no real secret lives
        # in the committed schema (which only carries the local-dev default).
        app_pw = os.getenv("APP_USER_PASSWORD")
        if app_pw:
            conn.execute(sql.SQL("ALTER ROLE app_user WITH PASSWORD {}").format(sql.Literal(app_pw)))
    print("schema applied")


if __name__ == "__main__":
    main()
