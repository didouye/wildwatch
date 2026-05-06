"""Tests for V1.2: schema migration + heartbeat infrastructure."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import SQLModel

from wildwatch_server import db as db_module
from wildwatch_server.models import Camera


def _reload_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WILDWATCH_API_KEY", "admin-key")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")
    for var in ("ENROLL", "UPLOAD", "LOGIN", "DEFAULT", "HEARTBEAT"):
        monkeypatch.setenv(f"WILDWATCH_RATE_{var}", "1000/minute")
    db_module.reset_engine_cache()
    import wildwatch_server.rate_limit as rl
    importlib.reload(rl)
    rl.limiter.reset()
    import wildwatch_server.routes.cameras as cameras_routes
    import wildwatch_server.routes.photos as photos_routes
    importlib.reload(photos_routes)
    importlib.reload(cameras_routes)
    import wildwatch_server.main as main_module
    importlib.reload(main_module)
    SQLModel.metadata.create_all(db_module.get_engine())
    return main_module.app, db_module.get_engine()


def test_migration_v11_to_v12_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine = _reload_app(tmp_path, monkeypatch)

    # Drop the new columns so we simulate a pre-v12 schema.
    with engine.begin() as conn:
        for col in (
            "desired_config",
            "last_heartbeat",
            "agent_last_seen_at",
            "pending_reorient_delta",
        ):
            try:
                conn.execute(text(f"ALTER TABLE cameras DROP COLUMN {col}"))
            except Exception:
                pass

    from wildwatch_server.migrations import upgrade_to_v12

    result1 = upgrade_to_v12(engine)
    result2 = upgrade_to_v12(engine)

    assert result1["columns_added"] == 4
    assert result2["columns_added"] == 0  # second call is a no-op

    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(cameras)")).fetchall()}
    assert {"desired_config", "last_heartbeat", "agent_last_seen_at", "pending_reorient_delta"} <= cols


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app, _ = _reload_app(tmp_path, monkeypatch)
    return TestClient(app)


def _enroll(client: TestClient, hostname: str = "rpi") -> dict:
    return client.post("/api/cameras/enroll", json={"hostname": hostname}).json()


def _approve(client: TestClient, camera_id: int) -> None:
    res = client.patch(
        f"/api/cameras/{camera_id}",
        headers={"Authorization": "Bearer admin-key"},
        json={"status": "approved"},
    )
    assert res.status_code == 200


def _heartbeat(client: TestClient, token: str, status: dict, preview: bytes | None = None):
    files: dict = {"status": (None, json.dumps(status))}
    if preview is not None:
        files["preview"] = ("preview.jpg", preview, "image/jpeg")
    return client.post(
        "/api/cameras/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
    )


def test_heartbeat_requires_camera_token(client: TestClient) -> None:
    res = client.post("/api/cameras/agent/heartbeat", files={"status": (None, "{}")})
    assert res.status_code == 401


def test_heartbeat_returns_403_when_pending(client: TestClient) -> None:
    cam = _enroll(client)
    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}})
    assert res.status_code == 403


def test_heartbeat_stores_blob_and_timestamp(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 42},
        "system": {"cpu_temp_c": 45.0, "memory_avail_mb": 200, "memory_total_mb": 512,
                    "load_avg_1min": 0.3, "disk_avail_mb": 1024, "queue_size": 0},
        "capture": {"service_active": True, "status_age_s": 1, "preview_age_s": 2,
                     "last_capture_at": None, "last_detection_at": None,
                     "error_count": 0, "apply_error_observed": False},
        "reported_config": {"rotation": 0, "capture_width": 2304},
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200, res.text

    # Sanity: the admin list endpoint still works after the heartbeat write.
    assert client.get(
        "/api/cameras",
        headers={"Authorization": "Bearer admin-key"},
    ).status_code == 200
    # Re-fetch via direct DB session for the new fields (they aren't in CameraRead yet).
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.agent_last_seen_at is not None
        stored = json.loads(row.last_heartbeat)
        assert stored["agent"]["version"] == "1.2.0"
        assert stored["reported_config"]["rotation"] == 0


def test_heartbeat_stores_preview_to_disk(client: TestClient, tmp_path: Path) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"x" * 100
    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}}, preview=fake_jpeg)
    assert res.status_code == 200, res.text

    from wildwatch_server.storage import previews_dir
    p = previews_dir() / f"{cam['id']}.jpg"
    assert p.exists() and p.read_bytes() == fake_jpeg


def test_heartbeat_without_preview_does_not_create_file(client: TestClient, tmp_path: Path) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}})
    assert res.status_code == 200
    from wildwatch_server.storage import previews_dir
    assert not (previews_dir() / f"{cam['id']}.jpg").exists()


def test_get_preview_returns_jpeg(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    blob = b"\xff\xd8\xff\xe0jpegdata"
    _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}}, preview=blob)

    res = client.get(f"/preview/{cam['id']}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == blob


def test_get_preview_returns_404_when_missing(client: TestClient) -> None:
    res = client.get("/preview/9999")
    assert res.status_code == 404


def test_camera_card_renders_status(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 60},
        "system": {"cpu_temp_c": 47.3, "memory_avail_mb": 234, "memory_total_mb": 512,
                    "load_avg_1min": 0.5, "disk_avail_mb": 1024, "queue_size": 0},
        "capture": {"service_active": True, "status_age_s": 1, "preview_age_s": 2,
                     "last_capture_at": None, "last_detection_at": None,
                     "error_count": 0, "apply_error_observed": False},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.get(f"/cameras/{cam['id']}/card")
    assert res.status_code == 200
    body = res.text
    # The fragment carries the running config + agent badge.
    assert "47.3" in body
    assert "Agent" in body
    assert "Capture" in body
    assert "rotation" in body and "0" in body  # current value


def test_cameras_page_uses_card_fragment(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    res = client.get("/cameras")
    assert res.status_code == 200
    # The polling URL must be present (sanity that the new fragment was rendered).
    assert f'hx-get="/cameras/{cam["id"]}/card"' in res.text


def test_camera_card_includes_rename_and_metadata(client: TestClient) -> None:
    cam = _enroll(client, "rpi-1")
    _approve(client, cam["id"])
    res = client.get(f"/cameras/{cam['id']}/card")
    assert res.status_code == 200
    html = res.text
    # Rename form must be reachable from the card.
    assert f'action="/cameras/{cam["id"]}/rename"' in html
    assert 'name="display_name"' in html
    # Hostname is shown so the operator can identify the device.
    assert "rpi-1" in html
    # photo_count rendered (0 photos at this point).
    assert "0 photo" in html


def test_heartbeat_rejects_non_dict_status(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Send a list instead of a dict.
    files = {"status": (None, json.dumps([1, 2, 3]))}
    res = client.post(
        "/api/cameras/agent/heartbeat",
        headers={"Authorization": f"Bearer {cam['token']}"},
        files=files,
    )
    assert res.status_code == 400


def test_latest_agent_version_constant_exists() -> None:
    from wildwatch_server.versions import LATEST_AGENT_VERSION

    # Sanity: x.y.z, three integer parts.
    parts = LATEST_AGENT_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_card_context_agent_status_none_when_no_heartbeat(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    res = client.get(f"/cameras/{cam['id']}/card")
    assert res.status_code == 200
    assert "Install agent" in res.text  # CTA visible
    assert "Update available" not in res.text


def test_card_context_agent_status_current(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0", "uptime_s": 5}})
    res = client.get(f"/cameras/{cam['id']}/card")
    assert res.status_code == 200
    assert "up to date" in res.text.lower() or "✓" in res.text
    assert "Install agent" not in res.text
    assert "Update available" not in res.text


def test_card_context_agent_status_outdated(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    _heartbeat(client, cam["token"], {"agent": {"version": "0.9.0", "uptime_s": 5}})
    res = client.get(f"/cameras/{cam['id']}/card")
    assert res.status_code == 200
    assert "Update available" in res.text
    assert "0.9.0" in res.text and "1.2.0" in res.text


def test_cameras_page_includes_update_modal_when_outdated(client: TestClient) -> None:
    # Bare hostname (no dot) -- modal appends .local for mDNS.
    # Modal lives on the full /cameras page (outside the htmx-polled card),
    # so it survives card swaps without closing if the operator opened it.
    cam = _enroll(client, hostname="dietpi")
    _approve(client, cam["id"])
    _heartbeat(client, cam["token"], {"agent": {"version": "0.9.0", "uptime_s": 1}})

    # Modal is NOT in the polled card fragment anymore (Fix v1.2: htmx UX).
    card = client.get(f"/cameras/{cam['id']}/card")
    assert f'id="update-camera-modal-{cam["id"]}"' not in card.text
    # But the card itself still shows the "Update available" CTA + version numbers.
    assert "Update available" in card.text
    assert "0.9.0" in card.text and "1.2.0" in card.text

    # Modal lives on the full /cameras page.
    page = client.get("/cameras")
    assert f'id="update-camera-modal-{cam["id"]}"' in page.text
    assert "raw.githubusercontent.com/didouye/wildwatch/main/_recovery/update.sh" in page.text
    assert "dietpi@dietpi.local" in page.text


def test_update_modal_does_not_double_dot_local_when_hostname_already_qualified(
    client: TestClient,
) -> None:
    # Hostname already contains a dot (FQDN, IP, or pre-existing .local) -> keep as-is.
    cam = _enroll(client, hostname="dietpi.local")
    _approve(client, cam["id"])
    _heartbeat(client, cam["token"], {"agent": {"version": "0.9.0", "uptime_s": 1}})
    # Modal is rendered at the page level, not in the card fragment.
    res = client.get("/cameras")
    assert "dietpi@dietpi.local" in res.text
    assert "dietpi.local.local" not in res.text  # the bug we are guarding against


def test_heartbeat_returns_desired_config_when_pending(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Operator pre-sets a desired_config via the DB directly (POST /cameras/{id}/config
    # is implemented in Task 2 -- skip the route here).
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.desired_config = json.dumps({"rotation": 180})
        s.add(row)
        s.commit()

    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}})
    assert res.status_code == 200
    body = res.json()
    assert body["desired_config"] == {"rotation": 180}
    assert body["commands"] == []


def test_heartbeat_clears_desired_when_applied_and_reported_matches(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180, "capture_width": 2304})
        s.commit()

    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 5},
        "applied_at": "2026-05-06T00:00:00+00:00",
        "reported_config": {"rotation": 180, "capture_width": 2304},
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200
    assert res.json()["desired_config"] is None  # cleared on this very tick

    with SM(get_engine()) as s:
        assert s.get(Camera, cam["id"]).desired_config is None


def test_heartbeat_keeps_desired_and_flags_error_when_applied_but_mismatch(
    client: TestClient,
) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()

    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 5},
        "applied_at": "2026-05-06T00:00:00+00:00",
        "reported_config": {"rotation": 0},  # capture is still on the OLD config
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200
    assert res.json()["desired_config"] == {"rotation": 180}  # still pending

    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None
        stored = json.loads(row.last_heartbeat)
        assert stored["capture"]["apply_error_observed"] is True


def test_heartbeat_no_applied_at_keeps_desired_no_error_flag(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()

    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0},
        # applied_at omitted -- agent hasn't applied yet (first time receiving desired)
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.json()["desired_config"] == {"rotation": 180}
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None  # still pending
        # Don't flag error: agent is still in the process of applying.
        stored = json.loads(row.last_heartbeat)
        assert stored.get("capture", {}).get("apply_error_observed", False) is False


def test_post_camera_config_sets_minimal_diff(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Reported config: rotation=0, capture=2304x1296. Operator wants rotation=180, capture stays.
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "180", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5"},
    )
    assert res.status_code == 200, res.text
    # Returns the card fragment
    assert "Update pending" in res.text
    assert "rotation" in res.text and "180" in res.text

    # DB: desired_config holds ONLY the changed field
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None
        stored = json.loads(row.desired_config)
        assert stored == {"rotation": 180}  # capture_width unchanged → not in diff


def test_post_camera_config_no_diff_clears_pending(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 180, "capture_width": 2304, "capture_height": 1296},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "180", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5"},
    )
    assert res.status_code == 200
    # No diff = no pending desired_config
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        assert s.get(Camera, cam["id"]).desired_config is None


def test_post_camera_config_validates_rotation(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "45", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5"},
    )
    assert res.status_code == 400  # invalid rotation
    assert "rotation" in res.text.lower()


def test_post_camera_config_rejects_negative_warmup_frames(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    data = {"rotation": "0", "capture_width": "2304", "capture_height": "1296",
            "detection_width": "640", "detection_height": "480",
            "pixel_threshold": "25", "area_threshold": "0.02",
            "background_alpha": "0.05", "warmup_frames": "-1",
            "cooldown_seconds": "5.0", "burst_count": "3",
            "burst_interval_seconds": "0.5"}
    res = client.post(f"/cameras/{cam['id']}/config", data=data)
    assert res.status_code == 400
    assert "warmup_frames" in res.text


def test_post_camera_config_fresh_camera_no_reported_yet_stores_all_fields(client: TestClient) -> None:
    """When reported_config is missing entirely (no heartbeat yet), the form
    submission populates all 12 fields into desired_config."""
    cam = _enroll(client)
    _approve(client, cam["id"])
    # No heartbeat yet -> reported_config is None.
    data = {"rotation": "0", "capture_width": "2304", "capture_height": "1296",
            "detection_width": "640", "detection_height": "480",
            "pixel_threshold": "25", "area_threshold": "0.02",
            "background_alpha": "0.05", "warmup_frames": "30",
            "cooldown_seconds": "5.0", "burst_count": "3",
            "burst_interval_seconds": "0.5"}
    res = client.post(f"/cameras/{cam['id']}/config", data=data)
    assert res.status_code == 200, res.text
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None
        import json as _json
        stored = _json.loads(row.desired_config)
        assert len(stored) == 12  # all 12 fields stored when fresh


def test_post_cancel_clears_desired_config(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()

    res = client.post(f"/cameras/{cam['id']}/config/cancel")
    assert res.status_code == 200
    with SM(get_engine()) as s:
        assert s.get(Camera, cam["id"]).desired_config is None


def test_cameras_page_includes_edit_settings_modal(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.get("/cameras")
    assert res.status_code == 200
    assert f'id="edit-settings-modal-{cam["id"]}"' in res.text
    assert f'action="/cameras/{cam["id"]}/config"' in res.text
    # All 12 form fields rendered
    for field in ("rotation", "capture_width", "capture_height", "detection_width",
                  "detection_height", "pixel_threshold", "area_threshold",
                  "background_alpha", "warmup_frames", "cooldown_seconds",
                  "burst_count", "burst_interval_seconds"):
        assert f'name="{field}"' in res.text
    # Pre-filled with reported_config values
    assert 'value="0"' in res.text  # rotation
    assert 'value="2304"' in res.text  # capture_width


def test_camera_card_has_edit_settings_button(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"},
                                       "reported_config": {"rotation": 0}})
    res = client.get(f"/cameras/{cam['id']}/card")
    assert "Edit settings" in res.text
    assert f'edit-settings-modal-{cam["id"]}' in res.text  # references modal id


def test_card_shows_update_pending_banner_with_diff(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304},
    }
    _heartbeat(client, cam["token"], payload)
    # Set desired (just rotation differs)
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()

    res = client.get(f"/cameras/{cam['id']}/card")
    assert "Update pending" in res.text
    assert "rotation" in res.text
    assert "0" in res.text and "180" in res.text  # diff arrow
    assert f'/cameras/{cam["id"]}/config/cancel' in res.text  # cancel button


def test_card_shows_apply_error_banner(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Set desired
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()
    # Heartbeat with applied_at + reported NOT matching → flags apply_error_observed
    payload = {
        "agent": {"version": "1.2.0"},
        "applied_at": "2026-05-06T00:00:00+00:00",
        "reported_config": {"rotation": 0},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.get(f"/cameras/{cam['id']}/card")
    assert "Last apply" in res.text and "fail" in res.text.lower()
    assert "Cancel update" in res.text or "/cancel" in res.text


def test_post_config_with_reorient_sets_pending_delta(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "180", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5",
              "reorient_existing": "on"},
    )
    assert res.status_code == 200
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        # rotation changed 0 -> 180, delta = 180
        assert row.pending_reorient_delta == 180
        # And desired_config holds the rotation change (minimal diff)
        assert json.loads(row.desired_config) == {"rotation": 180}


def test_post_config_with_reorient_no_rotation_change_stores_no_delta(
    client: TestClient,
) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 90, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    # Submit same rotation but with reorient_existing checked. No delta.
    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "90", "capture_width": "1536", "capture_height": "864",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5",
              "reorient_existing": "on"},
    )
    assert res.status_code == 200
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        # capture_width/height changed but rotation stayed -> no delta
        assert row.pending_reorient_delta is None


def test_post_config_without_reorient_flag_skips_delta(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    # Rotation changes but checkbox NOT checked.
    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "180", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5"},
        # no reorient_existing field
    )
    assert res.status_code == 200
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.pending_reorient_delta is None
        # desired_config still set for the rotation change
        assert json.loads(row.desired_config) == {"rotation": 180}


def test_heartbeat_schedules_reorient_when_apply_succeeds_with_delta(
    client: TestClient, tmp_path: Path
) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Pre-set desired + pending_reorient_delta as Task 2 would.
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.desired_config = json.dumps({"rotation": 180})
        row.pending_reorient_delta = 180
        s.commit()

    # Heartbeat with applied_at + reported matching desired -> server clears
    # desired AND should schedule the reorient BG task.
    from unittest.mock import patch
    with patch("wildwatch_server.routes.agent.reorient.reorient_camera_photos") as mock_job:
        payload = {
            "agent": {"version": "1.2.0"},
            "applied_at": "2026-05-06T00:00:00+00:00",
            "reported_config": {"rotation": 180},
        }
        res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200

    # The mock should have been queued as a BG task and executed.
    mock_job.assert_called_once()
    args, kwargs = mock_job.call_args
    # camera_id passed as kwarg or positional; assert delta + ack_time present
    call_camera_id = kwargs.get("camera_id", args[0] if args else None)
    call_delta = kwargs.get("delta", args[1] if len(args) > 1 else None)
    assert call_camera_id == cam["id"]
    assert call_delta == 180


def test_heartbeat_does_not_schedule_reorient_when_no_delta(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.desired_config = json.dumps({"capture_width": 1536})  # no rotation change
        # no pending_reorient_delta
        s.commit()

    from unittest.mock import patch
    with patch("wildwatch_server.routes.agent.reorient.reorient_camera_photos") as mock_job:
        payload = {
            "agent": {"version": "1.2.0"},
            "applied_at": "2026-05-06T00:00:00+00:00",
            "reported_config": {"capture_width": 1536},
        }
        _heartbeat(client, cam["token"], payload)

    mock_job.assert_not_called()


def test_heartbeat_does_not_schedule_reorient_on_apply_error(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.desired_config = json.dumps({"rotation": 180})
        row.pending_reorient_delta = 180
        s.commit()

    from unittest.mock import patch
    with patch("wildwatch_server.routes.agent.reorient.reorient_camera_photos") as mock_job:
        payload = {
            "agent": {"version": "1.2.0"},
            "applied_at": "2026-05-06T00:00:00+00:00",
            "reported_config": {"rotation": 0},  # MISMATCH -> apply_error
        }
        _heartbeat(client, cam["token"], payload)

    # Apply error -> desired stays, pending_reorient_delta stays, NO bg task.
    mock_job.assert_not_called()
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None
        assert row.pending_reorient_delta == 180


def test_card_shows_reorient_in_progress(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Manually populate the in-memory progress dict.
    from wildwatch_server import reorient
    reorient.reorient_progress[cam["id"]] = (12, 50)
    try:
        res = client.get(f"/cameras/{cam['id']}/card")
        assert "Reorienting" in res.text
        assert "12" in res.text and "50" in res.text
    finally:
        reorient.reorient_progress.pop(cam["id"], None)


def test_card_shows_reorient_queued_when_pending_no_progress_yet(
    client: TestClient,
) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.pending_reorient_delta = 90
        s.commit()
    res = client.get(f"/cameras/{cam['id']}/card")
    # Either "queued" or "Reorienting" with 0/?
    assert "Reorient" in res.text
    assert "90" in res.text  # delta visible


def test_edit_modal_includes_reorient_checkbox(client: TestClient) -> None:
    cam = _enroll(client, hostname="rpi-1")
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.get("/cameras")
    body = res.text
    assert 'name="reorient_existing"' in body
    assert "existing photo" in body.lower() or "photo(s)" in body
