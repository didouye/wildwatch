from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

PHOTOS_DIR = Path(__file__).resolve().parents[3] / "data" / "photos"

app = FastAPI(title="WildWatch server", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/photos")
async def upload_photo(
    file: UploadFile = File(...),
    captured_at: str | None = Form(default=None),
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

    return {
        "stored_path": str(target_path.relative_to(PHOTOS_DIR.parent.parent)),
        "size_bytes": str(len(contents)),
        "received_at": now.isoformat(),
        "captured_at": captured_at or "",
    }


def main() -> None:
    import uvicorn

    uvicorn.run("wildwatch_server.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
