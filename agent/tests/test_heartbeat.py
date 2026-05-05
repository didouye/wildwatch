"""Build payload and POST it -- httpx is mocked via respx."""

from __future__ import annotations

import httpx
import respx

from wildwatch_agent.heartbeat import HeartbeatClient, build_payload


def test_build_payload_shape() -> None:
    sys_info = {
        "cpu_temp_c": 47.0,
        "memory_avail_mb": 234,
        "memory_total_mb": 512,
        "load_avg_1min": 0.5,
        "disk_avail_mb": 1024,
        "queue_size": 0,
    }
    capture_status = {
        "service_active": True,
        "current_config": {"rotation": 0},
        "last_capture_at": None,
        "last_detection_at": None,
        "error_count": 0,
        "uptime_s": 60,
    }
    payload = build_payload(
        agent_version="1.2.0",
        agent_uptime_s=120,
        system=sys_info,
        capture_status=capture_status,
        status_age_s=1.0,
        preview_age_s=2.0,
    )
    assert payload["agent"] == {"version": "1.2.0", "uptime_s": 120}
    assert payload["system"] == sys_info
    assert payload["reported_config"] == {"rotation": 0}
    assert payload["capture"]["service_active"] is True
    assert payload["capture"]["status_age_s"] == 1.0


@respx.mock
def test_post_heartbeat_with_preview(tmp_path) -> None:
    route = respx.post("https://example.test/api/cameras/agent/heartbeat").mock(
        return_value=httpx.Response(200, json={"desired_config": None, "commands": []})
    )
    client = HeartbeatClient(server_url="https://example.test", token="abc", timeout=5.0)
    res = client.send(payload={"agent": {"version": "1.2.0"}}, preview=b"\xff\xd8data")
    assert res == {"desired_config": None, "commands": []}
    assert route.called
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer abc"
    # multipart contains the JSON `status` field + `preview` file.
    body = request.content
    assert b'name="status"' in body
    assert b'name="preview"' in body


@respx.mock
def test_post_heartbeat_403_returns_none() -> None:
    respx.post("https://example.test/api/cameras/agent/heartbeat").mock(
        return_value=httpx.Response(403, json={"detail": "Camera is pending"})
    )
    client = HeartbeatClient(server_url="https://example.test", token="abc", timeout=5.0)
    res = client.send(payload={"agent": {}}, preview=None)
    assert res is None  # caller must back off
