"""Script V0.1 minimal : capture une photo et l'envoie au serveur.

Ce script est volontairement simpliste. Il sert à valider la chaîne de bout en bout
(capture caméra → upload HTTP). Les versions suivantes ajouteront la détection de
mouvement, la file d'attente, le retry, etc.

Usage :
    BIRDY_SERVER_URL=http://<serveur>:8000 birdy-capture
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx


def capture_photo(output_path: Path) -> None:
    """Capture une photo via picamera2.

    picamera2 est fourni par le paquet système `python3-picamera2` sur DietPi.
    L'environnement uv doit être créé avec `--system-site-packages` pour y accéder.
    """
    try:
        from picamera2 import Picamera2
    except ImportError as exc:
        raise RuntimeError(
            "picamera2 introuvable. Installer le paquet système : "
            "`sudo apt install -y python3-picamera2`, puis recréer le venv uv "
            "avec --system-site-packages."
        ) from exc

    picam2 = Picamera2()
    config = picam2.create_still_configuration()
    picam2.configure(config)
    picam2.start()
    try:
        picam2.capture_file(str(output_path))
    finally:
        picam2.stop()
        picam2.close()


def upload_photo(photo_path: Path, server_url: str, captured_at: datetime) -> dict:
    url = f"{server_url.rstrip('/')}/api/photos"
    with photo_path.open("rb") as fp:
        files = {"file": (photo_path.name, fp, "image/jpeg")}
        data = {"captured_at": captured_at.isoformat()}
        response = httpx.post(url, files=files, data=data, timeout=30.0)
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="BirdyCapture V0.1 — capture + upload")
    parser.add_argument(
        "--server-url",
        default=os.environ.get("BIRDY_SERVER_URL", "http://localhost:8000"),
        help="URL du serveur BirdyServer (ou variable d'env BIRDY_SERVER_URL)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Conserver le fichier local après envoi",
    )
    args = parser.parse_args()

    captured_at = datetime.now(timezone.utc)
    with tempfile.NamedTemporaryFile(prefix="birdy_", suffix=".jpg", delete=False) as tmp:
        photo_path = Path(tmp.name)

    try:
        print(f"[birdy] Capture en cours -> {photo_path}", flush=True)
        capture_photo(photo_path)
        print(f"[birdy] Capture OK ({photo_path.stat().st_size} octets)", flush=True)

        print(f"[birdy] Envoi vers {args.server_url}", flush=True)
        result = upload_photo(photo_path, args.server_url, captured_at)
        print(f"[birdy] Serveur a répondu : {result}", flush=True)
    except Exception as exc:
        print(f"[birdy] ERREUR : {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        if not args.keep and photo_path.exists():
            photo_path.unlink()


if __name__ == "__main__":
    main()
