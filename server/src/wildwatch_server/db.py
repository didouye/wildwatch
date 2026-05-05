"""SQLite engine + session lifecycle.

The DB URL comes from `WILDWATCH_DB_URL`. When unset, we default to a SQLite
file alongside the photos directory: `<photos_dir parent>/wildwatch.db`. In
the tests, both `WILDWATCH_PHOTOS_DIR` and `WILDWATCH_DB_URL` get pointed at
a tmp_path, so each test runs against a clean DB.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine


def _default_db_url() -> str:
    photos_dir = os.environ.get("WILDWATCH_PHOTOS_DIR")
    if photos_dir:
        # Place the sqlite file next to the data dir so it stays with the photos.
        db_path = Path(photos_dir).resolve().parent / "wildwatch.db"
    else:
        db_path = Path(__file__).resolve().parents[3] / "data" / "wildwatch.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = os.environ.get("WILDWATCH_DB_URL") or _default_db_url()
    # check_same_thread=False is required by FastAPI's threadpool routing.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, echo=False, connect_args=connect_args)


def reset_engine_cache() -> None:
    """Clear the engine cache. Used by tests that need a fresh engine."""
    get_engine.cache_clear()


def init_db() -> None:
    """Create tables if they do not exist, then apply V0.5 column upgrades.

    SQLModel.metadata.create_all handles fresh installs (creates every
    declared table). Existing V0.4 databases get the missing columns added
    by `upgrade_to_v05`. Both paths are idempotent.
    """
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    # Imported here to avoid a circular import (migrations imports db.get_engine).
    from wildwatch_server.migrations import upgrade_to_v05

    upgrade_to_v05(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a session bound to the active engine."""
    with Session(get_engine()) as session:
        yield session
