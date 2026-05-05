"""Tests for the thumbnail generation module."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from wildwatch_server import thumbnails


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point WILDWATCH_PHOTOS_DIR at a tmp dir, return the photos root."""
    photos = tmp_path / "photos"
    photos.mkdir()
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(photos))
    return photos


def _write_source_image(photos_root: Path, relative: str, size: tuple[int, int] = (1600, 1200)) -> None:
    """Create a synthetic JPEG at <photos>/<relative>."""
    target = photos_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=(120, 200, 80))
    img.save(target, "JPEG", quality=70)


def test_generate_one_creates_jpeg(isolated_dirs: Path) -> None:
    _write_source_image(isolated_dirs, "2026/05/04/a.jpg")
    out = thumbnails.generate_one("2026/05/04/a.jpg", 400)

    assert out.exists()
    assert out.suffix == ".jpg"
    with Image.open(out) as img:
        # 1600x1200 source fitted into 400x400 box → 400x300 (preserves ratio)
        assert max(img.size) == 400


def test_generate_one_is_idempotent(isolated_dirs: Path) -> None:
    _write_source_image(isolated_dirs, "x.jpg")
    first = thumbnails.generate_one("x.jpg", 150)
    mtime1 = first.stat().st_mtime_ns

    second = thumbnails.generate_one("x.jpg", 150)
    assert second == first
    assert second.stat().st_mtime_ns == mtime1  # not regenerated


def test_generate_one_rejects_unsupported_size(isolated_dirs: Path) -> None:
    _write_source_image(isolated_dirs, "x.jpg")
    with pytest.raises(ValueError):
        thumbnails.generate_one("x.jpg", 999)


def test_generate_one_raises_when_source_missing(isolated_dirs: Path) -> None:
    with pytest.raises(FileNotFoundError):
        thumbnails.generate_one("does/not/exist.jpg", 400)


def test_generate_all_creates_three_files(isolated_dirs: Path) -> None:
    _write_source_image(isolated_dirs, "2026/05/04/y.jpg")
    paths = thumbnails.generate_all("2026/05/04/y.jpg")

    assert len(paths) == 3
    for p in paths:
        assert p.exists()
        # path layout: <thumb_root>/<size>/2026/05/04/y.jpg
        assert "thumbnails" in str(p)
    # The first path component after thumbnails/ is the size folder.
    size_folders = {p.relative_to(thumbnails.thumbnail_dir()).parts[0] for p in paths}
    assert size_folders == {"150", "400", "800"}


def test_generate_all_swallows_errors(isolated_dirs: Path, caplog: pytest.LogCaptureFixture) -> None:
    """generate_all must not propagate exceptions (it runs in BackgroundTasks)."""
    paths = thumbnails.generate_all("missing.jpg")
    assert paths == []
    assert "Failed to generate thumbnail" in caplog.text


def test_ensure_thumbnail_lazy_creates_when_missing(isolated_dirs: Path) -> None:
    _write_source_image(isolated_dirs, "z.jpg")
    target = thumbnails.thumbnail_path("z.jpg", 800)
    assert not target.exists()

    out = thumbnails.ensure_thumbnail("z.jpg", 800)
    assert out.exists()
    assert out == target


def test_ensure_thumbnail_returns_cached_when_present(isolated_dirs: Path) -> None:
    _write_source_image(isolated_dirs, "z.jpg")
    first = thumbnails.ensure_thumbnail("z.jpg", 400)
    mtime1 = first.stat().st_mtime_ns

    second = thumbnails.ensure_thumbnail("z.jpg", 400)
    assert second.stat().st_mtime_ns == mtime1
