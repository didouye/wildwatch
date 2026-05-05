from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from slowapi.errors import RateLimitExceeded

from wildwatch_server.auth_web import _RedirectToLogin, redirect_to_login
from wildwatch_server.db import init_db
from wildwatch_server.rate_limit import limiter, rate_limit_handler
from wildwatch_server.routes import (
    admin,
    agent,
    auth,
    cameras,
    health,
    photos,
    share,
    stats,
    tags,
    thumbnails,
    ui,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="WildWatch server", version="1.0.0", lifespan=lifespan)

    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

    # Auth: convert _RedirectToLogin into a 302
    @app.exception_handler(_RedirectToLogin)
    async def _login_redirect(request: Request, exc: _RedirectToLogin):
        return redirect_to_login(exc.next_url)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(photos.router)
    app.include_router(stats.router)
    app.include_router(admin.router)
    app.include_router(thumbnails.router)
    app.include_router(tags.router)
    app.include_router(share.router)
    app.include_router(cameras.public_router)
    app.include_router(cameras.admin_router)
    app.include_router(agent.router)
    app.include_router(ui.router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("wildwatch_server.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
