"""Manual schema upgrades for V0.4 -> V0.5.

`SQLModel.metadata.create_all` adds new tables but does not add columns to
existing ones. This module checks the live schema with PRAGMA table_info
and runs the missing ALTER TABLE statements idempotently.

Usage on an existing database:

    python -m wildwatch_server.migrations

Or programmatically (also called from `init_db`):

    from wildwatch_server.migrations import upgrade_to_v05
    upgrade_to_v05(engine)
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def _existing_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def _table_exists(engine: Engine, table: str) -> bool:
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
            ),
            {"t": table},
        ).fetchone()
    return result is not None


def upgrade_to_v05(engine: Engine) -> dict[str, int]:
    """Idempotently bring the schema from V0.4 to V0.5.

    Returns a dict describing what was done -- helpful for logs and tests.
    Safe to call on a brand new database created via SQLModel.metadata.create_all
    (it just finds no work to do).
    """
    actions = {"columns_added": 0, "tables_created": 0}

    if not _table_exists(engine, "photos"):
        # Brand new database. Nothing to migrate; create_all already ran.
        return actions

    cols = _existing_columns(engine, "photos")
    with engine.begin() as conn:
        if "is_favorite" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE photos ADD COLUMN is_favorite "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            )
            actions["columns_added"] += 1
        if "share_token" not in cols:
            conn.execute(text("ALTER TABLE photos ADD COLUMN share_token TEXT"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_photos_share_token ON photos(share_token)"
                )
            )
            actions["columns_added"] += 1

        if not _table_exists(engine, "tags"):
            conn.execute(
                text(
                    """
                    CREATE TABLE tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        color TEXT
                    )
                    """
                )
            )
            actions["tables_created"] += 1

        if not _table_exists(engine, "photo_tags"):
            conn.execute(
                text(
                    """
                    CREATE TABLE photo_tags (
                        photo_id INTEGER NOT NULL,
                        tag_id   INTEGER NOT NULL,
                        PRIMARY KEY (photo_id, tag_id),
                        FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE,
                        FOREIGN KEY (tag_id)   REFERENCES tags(id)   ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_photo_tags_tag ON photo_tags(tag_id)"
                )
            )
            actions["tables_created"] += 1

    if actions["columns_added"] or actions["tables_created"]:
        log.info("V0.5 migration applied: %s", actions)
    return actions


def main() -> None:
    """CLI entry point: run the upgrade against the configured engine."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from wildwatch_server.db import get_engine, init_db

    init_db()  # ensures all tables exist via create_all first
    upgrade_to_v05(get_engine())


if __name__ == "__main__":
    main()
