from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from wildwatch_server.db import init_db
from wildwatch_server.routes import admin, health, photos, stats, thumbnails, ui


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="WildWatch server", version="0.4.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(photos.router)
    app.include_router(stats.router)
    app.include_router(admin.router)
    app.include_router(thumbnails.router)
    app.include_router(ui.router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("wildwatch_server.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
