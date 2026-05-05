"""Web (cookie session) authentication.

Auth is enabled when both `WILDWATCH_WEB_PASSWORD_HASH` and
`WILDWATCH_SESSION_SECRET` are set. Otherwise the dependency is a no-op,
which preserves the V0.5 LAN/dev experience.
"""

from __future__ import annotations

import os
import time
from typing import Final

from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

COOKIE_NAME: Final[str] = "wildwatch_session"
COOKIE_MAX_AGE: Final[int] = 30 * 24 * 3600  # 30 days

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def is_auth_enabled() -> bool:
    return bool(os.environ.get("WILDWATCH_WEB_PASSWORD_HASH")) and bool(
        os.environ.get("WILDWATCH_SESSION_SECRET")
    )


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("WILDWATCH_SESSION_SECRET")
    if not secret:
        raise RuntimeError("WILDWATCH_SESSION_SECRET is not set")
    return URLSafeTimedSerializer(secret, salt="wildwatch-session")


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _pwd_context.verify(password, hashed)
    except ValueError:
        return False


def verify_credentials(username: str, password: str) -> bool:
    expected_user = os.environ.get("WILDWATCH_WEB_USER")
    expected_hash = os.environ.get("WILDWATCH_WEB_PASSWORD_HASH")
    if not expected_user or not expected_hash:
        return False
    if username != expected_user:
        return False
    return verify_password(password, expected_hash)


def issue_session_cookie(response: Response, username: str, *, secure: bool) -> None:
    payload = {"u": username, "iat": int(time.time())}
    token = _serializer().dumps(payload)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def current_user(request: Request) -> str | None:
    """Return the username if the cookie is valid, else None.

    Returns None silently for any tampering / expiration so middleware can
    redirect cleanly without leaking error details.
    """
    if not is_auth_enabled():
        return None
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        payload = _serializer().loads(raw, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return payload.get("u")


def require_web_session(request: Request) -> None:
    """FastAPI dependency that gates UI routes behind the session cookie.

    When auth is disabled (env var unset), it is a no-op so the LAN dev
    mode keeps working. When enabled, missing/invalid cookies trigger a
    302 redirect to /login (with `next=` so the user lands on the page
    they originally requested after logging in).
    """
    if not is_auth_enabled():
        return
    if current_user(request) is not None:
        return
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    raise _RedirectToLogin(next_url)


class _RedirectToLogin(HTTPException):
    """Marker exception caught by an exception handler -> 302 to /login."""

    def __init__(self, next_url: str) -> None:
        super().__init__(status_code=307, detail="Login required")
        self.next_url = next_url


def redirect_to_login(next_url: str) -> RedirectResponse:
    target = "/login"
    if next_url and next_url not in {"/", "/login"}:
        from urllib.parse import quote

        target = f"/login?next={quote(next_url, safe='')}"
    return RedirectResponse(url=target, status_code=302)
