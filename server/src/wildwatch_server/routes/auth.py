"""Login form, login submit, and logout."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from wildwatch_server.auth_web import (
    clear_session_cookie,
    is_auth_enabled,
    issue_session_cookie,
    verify_credentials,
)
from wildwatch_server.rate_limit import LOGIN_LIMIT, limiter

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto == "https"


def _safe_next_url(value: str | None) -> str:
    """Restrict redirects to local paths to avoid open redirects."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/gallery"
    return value


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None) -> HTMLResponse:
    if not is_auth_enabled():
        return RedirectResponse(url="/gallery", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "next": _safe_next_url(next)},
    )


@router.post("/login", response_class=HTMLResponse)
@limiter.limit(LOGIN_LIMIT)
def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/gallery"),
):
    if not is_auth_enabled():
        return RedirectResponse(url="/gallery", status_code=302)

    if not verify_credentials(username, password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Invalid username or password.",
                "next": _safe_next_url(next),
            },
            status_code=401,
        )

    response = RedirectResponse(url=_safe_next_url(next), status_code=302)
    issue_session_cookie(response, username, secure=_is_https(request))
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=302)
    clear_session_cookie(response)
    return response
