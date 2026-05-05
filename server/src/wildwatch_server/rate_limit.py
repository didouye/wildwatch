"""Rate limiting via slowapi.

The limits below are tuned for a single RPi camera trap pushing bursts of
3 photos per detection (V0.2 default). Dev/test runs that want to exercise
the throttle can override the values via the env vars listed at the top of
this module.
"""

from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


def _env_limit(var: str, default: str) -> str:
    return os.environ.get(var, default)


# Defaults (overridable for tests).
DEFAULT_LIMIT = _env_limit("WILDWATCH_RATE_DEFAULT", "200/minute")
LOGIN_LIMIT = _env_limit("WILDWATCH_RATE_LOGIN", "5/minute")
UPLOAD_LIMIT = _env_limit("WILDWATCH_RATE_UPLOAD", "30/minute")
ENROLL_LIMIT = _env_limit("WILDWATCH_RATE_ENROLL", "10/hour")
HEARTBEAT_LIMIT = _env_limit("WILDWATCH_RATE_HEARTBEAT", "120/minute")


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[DEFAULT_LIMIT],
    headers_enabled=True,
)


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )
