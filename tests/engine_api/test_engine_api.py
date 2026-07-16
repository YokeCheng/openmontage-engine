from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine_api.app import create_app
from engine_api.store import new_id, utc_now


def manifest(title: str = "CouncilForge") -> dict:
    return {
        "schema_version": "1.0",
        "title": title,
        "objective": "Explain how one platform plans and renders a video",
        "format": "product_intro",
        "language": "zh-CN",
        "script": {"sections": [{"title": "Opening", "narration": "Hello"}]},
        "scenes": [
            {
                "scene_id": "scene-01",
                "title": "One creative brain",
                "duration_seconds": 1,
                "narration": "CouncilForge plans and OpenMontage executes.",
                "visual": {"type": "motion_graphics", "prompt": "Platform flow"},
            }
        ],
        "audio": {"voice": "none", "music": "none"},
        "render": {"aspect_ratio": "16:9", "width": 640, "height": 360, "fps": 24, "duration_seconds": 1},
        "budget": {"maximum_usd": 0},
        "fallback_policy": {"video_generation": ["motion_graphics"]},
    }


def request_body(title: str = "CouncilForge", tenant: str = "tenant-a") -> dict:
    return {
        "schema_version": "1.0",
        "request_id": f"request-{title}",
        "tenant_id": tenant,
        "created_by": "user-1",
        "pipeline": {"name": "councilforge-platform", "version": "1.0"},
        "input": manifest(title),
        "config_version": "video-v1",
        "credential_grants": {"provider_secret": "must-never-persist"},
    }


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / ".engine-runtime"))


def headers(tenant: str = "tenant-a", key: str = "create-1") -> dict[str, str]:
    return {"X-Tenant-ID": tenant, "Idempotency-Key": key}


def create(client: TestClient, *, title: str = "CouncilForge", tenant: str = "tenant-a", key: str = "create-1") -> dict:
    response = client.post("/v1/jobs", json=request_body(title, tenant), headers=headers(tenant, key))
    assert response.status_code == 202
    return response.json()


def test_create_waits_for_script_and_budget_approval_and_never_persists_credentials(client: TestClient) -> None:
    job = create(client)
    assert job["status"] == "waiting_approval"
    assert job["approval"]["status"] == "pending"
    runtime_text = "\n".join(path.read_text(errors="ignore") for path in client.app.state.store.root.rglob("*") if path.is_file())
    assert "must-never-persist" not in runtime_text
    assert "credential_grants" not in runtime_text


def test_idempotency_replays_same_body_and_rejects_conflict(client: TestClient) -> None:
    first = create(client)
    replay = client.post("/v1/jobs", json=request_body(), headers=headers()).json()
    assert replay["job_id"] == first["job_id"]
    conflict = client.post("/v1/jobs", json=request_body("Different"), headers=headers())
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED"


def test_tenant_isolation_and_invalid_approval_transition(client: TestClient) -> None:
    job = create(client)
    assert client.get(f"/v1/jobs/{job['job_id']}", headers={"X-Tenant-ID": "tenant-b"}).status_code == 404
    invalid = client.post(
        f"/v1/jobs/{job['job_id']}/approve",
        headers={"X-Tenant-ID": "tenant-a"},
        json={"approval_id": "wrong", "decision": "approved", "decided_by": "user-1"},
    )
    assert invalid.status_code == 409


def test_approval_is_idempotent_and_render_registers_range_artifact(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "fixture.mp4"
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x14232D:s=320x180:d=0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(fixture)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("OPENMONTAGE_ENGINE_RENDER_MODE", "fixture-copy")
    monkeypatch.setenv("OPENMONTAGE_ENGINE_FIXTURE_VIDEO", str(fixture))
    job = create(client)
    body = {"approval_id": job["approval"]["approval_id"], "decision": "approved", "decided_by": "user-1"}
    first = client.post(f"/v1/jobs/{job['job_id']}/approve", headers={"X-Tenant-ID": "tenant-a"}, json=body)
    assert first.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        current = client.get(f"/v1/jobs/{job['job_id']}", headers={"X-Tenant-ID": "tenant-a"}).json()
        if current["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert current["status"] == "succeeded"
    assert current["artifacts"][0]["checksum"]["algorithm"] == "sha256"
    artifact_id = current["artifacts"][0]["artifact_id"]
    ranged = client.get(
        f"/v1/jobs/{job['job_id']}/artifacts/{artifact_id}/content",
        headers={"X-Tenant-ID": "tenant-a", "Range": "bytes=0-15"},
    )
    assert ranged.status_code == 206
    assert len(ranged.content) == 16
    replay = client.post(f"/v1/jobs/{job['job_id']}/approve", headers={"X-Tenant-ID": "tenant-a"}, json=body)
    assert replay.status_code == 200


def test_action_resolution_cancel_event_sequence_and_restart_recovery(client: TestClient) -> None:
    job = create(client)
    stored = client.app.state.store.load_job(job["job_id"])
    assert stored is not None
    action_id = new_id("action")
    stored["status"] = "waiting_action"
    stored["actions"] = [
        {
            "action_id": action_id,
            "job_id": job["job_id"],
            "type": "provider_fallback",
            "status": "pending",
            "summary": "Use local motion graphics",
            "created_at": utc_now(),
        }
    ]
    client.app.state.store.save_job(stored)
    resolved = client.post(
        f"/v1/jobs/{job['job_id']}/actions/{action_id}/resolve",
        headers={"X-Tenant-ID": "tenant-a"},
        json={"resolution": "use_local", "resolved_by": "user-1"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "running"
    cancelled = client.post(f"/v1/jobs/{job['job_id']}/cancel", headers={"X-Tenant-ID": "tenant-a"}, json={})
    assert cancelled.status_code in {200, 202}
    events = client.get(f"/v1/jobs/{job['job_id']}/events", headers={"X-Tenant-ID": "tenant-a"}).json()["events"]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))

    restarted = TestClient(create_app(client.app.state.store.root))
    recovered = restarted.get(f"/v1/jobs/{job['job_id']}", headers={"X-Tenant-ID": "tenant-a"})
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "cancelled"
