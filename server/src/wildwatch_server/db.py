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
    """Create tables if they do not exist.

    For V0.4a we lean on SQLModel.metadata.create_all rather than running
    Alembic migrations from inside the app: the schema is small, the dev
    workflow is simpler, and migrations will only matter once the schema
    starts evolving (V0.5+). The Alembic setup is still in place so we can
    grow into it.
    """
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a session bound to the active engine."""
    with Session(get_engine()) as session:
        yield session
