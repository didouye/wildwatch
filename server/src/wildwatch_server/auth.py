"""API key authentication dependency.

Reads `WILDWATCH_API_KEY` lazily from the environment at request time.
Reading at import time would break tests that set the variable in fixtures,
because the import happens before pytest sets the env var.
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected_key = os.environ.get("WILDWATCH_API_KEY") or None
    if expected_key is None:
        return
    if authorization != f"Bearer {expected_key}":
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
