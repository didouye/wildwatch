from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile

API_KEY = os.environ.get("WILDWATCH_API_KEY") or None
PHOTOS_DIR_OVERRIDE = os.environ.get("WILDWATCH_PHOTOS_DIR")
PHOTOS_DIR = (
    Path(PHOTOS_DIR_OVERRIDE).resolve()
    if PHOTOS_DIR_OVERRIDE
    else Path(__file__).resolve().parents[3] / "data" / "photos"
)

app = FastAPI(title="WildWatch server", version="0.3.0")


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """Dépendance FastAPI qui vérifie l'en-tête `Authorization: Bearer <key>`.

    Si `WILDWATCH_API_KEY` n'est pas définie côté serveur, l'auth est désactivée
    (utile en dev local). Sinon, exige l'en-tête exact ou répond 401.
    """
    if API_KEY is None:
        return
    expected = f"Bearer {API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/photos", dependencies=[Depends(require_api_key)])
async def upload_photo(
    file: UploadFile = File(...),
    captured_at: str | None = Form(default=None),
    metadata: str | None = Form(default=None),
) -> dict[str, str]:
    if file.content_type not in {"image/jpeg", "image/png"}:
        raise HTTPException(status_code=415, detail=f"Unsupported media type: {file.content_type}")

    now = datetime.now(timezone.utc)
    target_dir = PHOTOS_DIR / f"{now:%Y/%m/%d}"
    target_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename or "photo.jpg").suffix or ".jpg"
    target_path = target_dir / f"{now:%Y%m%dT%H%M%S%f}{suffix}"

    contents = await file.read()
    target_path.write_bytes(contents)

    if metadata:
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="metadata must be valid JSON") from None
        meta_path = target_path.with_suffix(target_path.suffix + ".meta.json")
        meta_path.write_text(json.dumps(parsed, sort_keys=True))

    relative = target_path.relative_to(PHOTOS_DIR.parent.parent) if not PHOTOS_DIR_OVERRIDE else (
        Path("data/photos") / target_path.relative_to(PHOTOS_DIR)
    )
    return {
        "stored_path": str(relative),
        "size_bytes": str(len(contents)),
        "received_at": now.isoformat(),
        "captured_at": captured_at or "",
    }


def main() -> None:
    import uvicorn

    uvicorn.run("wildwatch_server.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
