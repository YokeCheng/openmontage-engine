from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine_api.app import create_app
from engine_api.renderer import MediaActionRequired, _materialize_media, _render_contract
from engine_api.store import EngineStore, new_id, utc_now
from tools.base_tool import ToolResult


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


def test_render_contract_routes_canonical_edit_decisions_to_existing_explainer() -> None:
    payload = manifest()
    payload["pipeline_artifacts"] = {
        "edit_decisions": {
            "version": "1.0",
            "render_runtime": "remotion",
            "cuts": [
                {
                    "id": "scene-01",
                    "source": "",
                    "in_seconds": 0,
                    "out_seconds": 1,
                    "type": "hero_title",
                    "text": "真实场景",
                }
            ],
        }
    }
    composition_id, props = _render_contract(payload)
    assert composition_id == "Explainer"
    assert props["cuts"][0]["type"] == "hero_title"
    assert props["render"]["width"] == 640


def test_render_contract_keeps_legacy_manifest_fallback() -> None:
    composition_id, props = _render_contract(manifest())
    assert composition_id == "CouncilForgePlatform"
    assert props["title"] == "CouncilForge"


def test_approved_image_policy_executes_registry_selector_and_updates_explainer_props(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Available:
        value = "available"

    class FakeImageSelector:
        def get_status(self) -> Available:
            return Available()

        def execute(self, inputs: dict) -> ToolResult:
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"generated-image")
            return ToolResult(
                success=True,
                data={"output": str(output), "selected_provider": "test-provider"},
                artifacts=[str(output)],
            )

    from tools.tool_registry import registry

    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(
        registry,
        "get",
        lambda name: FakeImageSelector() if name == "image_selector" else None,
    )
    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "ai_image",
        "image_provider": "test-provider",
        "voice_provider": "none",
        "music_provider": "none",
    }
    props = {
        "cuts": [
            {
                "id": "scene-01",
                "source": "",
                "in_seconds": 0,
                "out_seconds": 1,
                "type": "hero_title",
                "text": "Scene",
            }
        ]
    }
    store = EngineStore(tmp_path / "runtime")
    job = {"job_id": "job-media", "tenant_id": "tenant-a", "correlation_id": "corr"}
    assets = _materialize_media(payload, props, tmp_path / "assets", store, job)
    assert props["cuts"][0]["backgroundImage"].endswith("scene-01.png")
    assert assets[0]["provider"] == "test-provider"
    assert store.events("job-media")[0]["type"] == "media.asset_ready"


def test_unavailable_approved_media_pauses_for_fallback_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unavailable:
        value = "unavailable"

    class FakeImageSelector:
        def get_status(self) -> Unavailable:
            return Unavailable()

    from tools.tool_registry import registry

    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(registry, "get", lambda name: FakeImageSelector())
    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "ai_image",
        "image_provider": "test-provider",
        "voice_provider": "none",
        "music_provider": "none",
        "fallback": "ask",
    }
    props = {"cuts": [{"id": "scene-01", "in_seconds": 0, "out_seconds": 1}]}
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-media",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
        "status": "rendering",
        "stage": "media",
        "progress": {"percent": 52, "message": "media", "updated_at": utc_now()},
    }
    store.save_job(job)
    with pytest.raises(MediaActionRequired):
        _materialize_media(payload, props, tmp_path / "assets", store, job)
    paused = store.load_job("job-media")
    assert paused is not None
    assert paused["status"] == "waiting_action"
    assert paused["actions"][0]["recommended_resolution"] == "use_motion_graphics"


def request_body(
    title: str = "CouncilForge",
    tenant: str = "tenant-a",
    execution_mode: str = "engine_managed",
) -> dict:
    return {
        "schema_version": "1.0",
        "request_id": f"request-{title}",
        "tenant_id": tenant,
        "created_by": "user-1",
        "pipeline": {"name": "councilforge-platform", "version": "1.0"},
        "input": manifest(title),
        "config_version": "video-v1",
        "execution_mode": execution_mode,
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


def test_platform_managed_job_skips_duplicate_engine_approval(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "platform-approved.mp4"
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x14232D:s=320x180:d=0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(fixture)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("OPENMONTAGE_ENGINE_RENDER_MODE", "fixture-copy")
    monkeypatch.setenv("OPENMONTAGE_ENGINE_FIXTURE_VIDEO", str(fixture))
    response = client.post(
        "/v1/jobs",
        json=request_body(execution_mode="platform_managed"),
        headers=headers(),
    )
    assert response.status_code == 202
    job = response.json()
    assert job["approval"] is None
    assert job["execution_mode"] == "platform_managed"
    assert job["status"] in {"running", "rendering", "succeeded"}


def test_idempotency_replays_same_body_and_rejects_conflict(client: TestClient) -> None:
    first = create(client)
    replay = client.post("/v1/jobs", json=request_body(), headers=headers()).json()
    assert replay["job_id"] == first["job_id"]
    conflict = client.post("/v1/jobs", json=request_body("Different"), headers=headers())
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED"


def test_registered_upstream_pipeline_is_accepted_and_unknown_pipeline_is_rejected(
    client: TestClient,
) -> None:
    upstream = request_body(execution_mode="platform_managed")
    upstream["pipeline"] = {"name": "animated-explainer", "version": "2.0"}
    accepted = client.post(
        "/v1/jobs",
        json=upstream,
        headers=headers(key="upstream-pipeline"),
    )
    assert accepted.status_code == 202
    assert accepted.json()["pipeline"]["name"] == "animated-explainer"

    unknown = request_body()
    unknown["pipeline"] = {"name": "not-a-real-pipeline", "version": "1.0"}
    rejected = client.post(
        "/v1/jobs",
        json=unknown,
        headers=headers(key="unknown-pipeline"),
    )
    assert rejected.status_code == 400
    assert rejected.json()["error_code"] == "PIPELINE_NOT_REGISTERED"


def test_provider_catalog_and_runtime_configuration_are_registry_backed_and_ephemeral(
    client: TestClient,
) -> None:
    catalog = client.get("/v1/providers")
    assert catalog.status_code == 200
    providers = catalog.json()["providers"]
    pixabay = next(item for item in providers if item["provider"] == "pixabay")
    assert "image_generation" in pixabay["capabilities"]
    assert "PIXABAY_API_KEY" in pixabay["credential_fields"]

    try:
        configured = client.put(
            "/v1/runtime/config",
            json={"values": {"PIXABAY_API_KEY": "runtime-only-secret"}},
        )
        assert configured.status_code == 200
        assert configured.json()["configured_fields"] == ["PIXABAY_API_KEY"]
        runtime_text = "\n".join(
            path.read_text(errors="ignore")
            for path in client.app.state.store.root.rglob("*")
            if path.is_file()
        )
        assert "runtime-only-secret" not in runtime_text
    finally:
        import os

        from tools.tool_registry import registry

        os.environ.pop("PIXABAY_API_KEY", None)
        registry.clear()

    rejected = client.put("/v1/runtime/config", json={"values": {"NOT_A_PROVIDER_FIELD": "secret"}})
    assert rejected.status_code == 422
    assert rejected.json()["error_code"] == "RUNTIME_CONFIG_FIELD_UNSUPPORTED"


def test_pipeline_bundle_exposes_manifest_and_stage_director_instructions(client: TestClient) -> None:
    response = client.get("/v1/pipelines/animation/bundle")
    assert response.status_code == 200
    bundle = response.json()
    assert bundle["manifest"]["name"] == "animation"
    assert bundle["stages"]
    assert any(stage["instruction"] for stage in bundle["stages"])
    assert client.get("/v1/pipelines/../../secrets/bundle").status_code == 404


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
    subtitle = next(item for item in current["artifacts"] if item["kind"] == "subtitle")
    subtitle_response = client.get(
        f"/v1/jobs/{job['job_id']}/artifacts/{subtitle['artifact_id']}/content",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert subtitle_response.status_code == 200
    assert subtitle_response.headers["content-type"].startswith("application/x-subrip")
    assert "CouncilForge plans" in subtitle_response.text
    assert subtitle["metadata"]["cue_count"] == 1
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


def create_workspace(
    client: TestClient,
    *,
    tenant: str = "tenant-a",
    key: str = "workspace-1",
    title: str = "Agent-hosted explainer",
) -> dict:
    response = client.post(
        "/v1/workspaces",
        headers=headers(tenant, key),
        json={
            "request_id": "video-task-1",
            "title": title,
            "pipeline": "animated-explainer",
            "metadata": {"platform_task_id": "video-task-1"},
        },
    )
    assert response.status_code == 201
    return response.json()


def test_capability_workspace_is_idempotent_tenant_isolated_and_exposes_stage_skill(client: TestClient) -> None:
    workspace = create_workspace(client)
    replay = client.post(
        "/v1/workspaces",
        headers=headers(key="workspace-1"),
        json={
            "request_id": "video-task-1",
            "title": "Agent-hosted explainer",
            "pipeline": "animated-explainer",
            "metadata": {"platform_task_id": "video-task-1"},
        },
    )
    assert replay.status_code == 200
    assert replay.json()["workspace_id"] == workspace["workspace_id"]

    conflict = client.post(
        "/v1/workspaces",
        headers=headers(key="workspace-1"),
        json={
            "request_id": "video-task-1",
            "title": "Different title",
            "pipeline": "animated-explainer",
            "metadata": {},
        },
    )
    assert conflict.status_code == 409
    assert client.get(
        f"/v1/workspaces/{workspace['workspace_id']}",
        headers={"X-Tenant-ID": "tenant-b"},
    ).status_code == 404

    context = client.get(
        f"/v1/workspaces/{workspace['workspace_id']}/stages/assets/context",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert context.status_code == 200
    payload = context.json()
    assert payload["stage"]["name"] == "assets"
    assert "asset" in payload["instruction"].lower()
    assert {tool["name"] for tool in payload["tools"]} >= {"diagram_gen", "image_selector", "tts_selector"}
    assert payload["artifact_schemas"]["asset_manifest"]["title"]


def test_capability_gateway_executes_only_stage_tools_and_serves_artifacts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = create_workspace(client, key="workspace-execution")
    workspace_id = workspace["workspace_id"]

    from tools.tool_registry import registry

    registry.ensure_discovered()
    diagram = registry.get("diagram_gen")
    assert diagram is not None

    def fake_execute(inputs: dict) -> ToolResult:
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"agent-hosted-openmontage-artifact")
        return ToolResult(
            success=True,
            data={
                "output_path": str(output),
                "audio_url": "https://media.example.test/audio.wav?Expires=123&OSSAccessKeyId=test&Signature=secret",
                "nested": {"api_token": "must-not-persist"},
            },
            artifacts=[str(output)],
        )

    monkeypatch.setattr(diagram, "execute", fake_execute)
    rejected = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="not-allowed"),
        json={"stage": "research", "tool_name": "diagram_gen", "inputs": {}},
    )
    assert rejected.status_code == 403

    escaped = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="escaped"),
        json={
            "stage": "assets",
            "tool_name": "diagram_gen",
            "inputs": {"output_path": "../../outside.png"},
        },
    )
    assert escaped.status_code == 422
    assert escaped.json()["error_code"] == "TOOL_PATH_OUTSIDE_WORKSPACE"

    nested_escape = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="nested-escaped"),
        json={
            "stage": "assets",
            "tool_name": "diagram_gen",
            "inputs": {"options": {"output_path": "../../nested-outside.png"}},
        },
    )
    assert nested_escape.status_code == 422
    assert nested_escape.json()["error_code"] == "TOOL_PATH_OUTSIDE_WORKSPACE"

    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="diagram-1"),
        json={
            "stage": "assets",
            "tool_name": "diagram_gen",
            "inputs": {
                "diagram_type": "boxes",
                "boxes": [{"label": "CouncilForge"}, {"label": "OpenMontage"}],
                "output_path": "assets/images/architecture.png",
            },
        },
    )
    assert submitted.status_code == 202
    execution_id = submitted.json()["execution_id"]
    deadline = time.time() + 3
    while time.time() < deadline:
        execution = client.get(
            f"/v1/workspaces/{workspace_id}/executions/{execution_id}",
            headers={"X-Tenant-ID": "tenant-a"},
        ).json()
        if execution["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)
    assert execution["status"] == "succeeded"
    assert execution["artifacts"]
    assert execution["artifacts"][0]["checksum"]
    assert "metadata" in execution["artifacts"][0]
    persisted_result = json.dumps(execution["result"], ensure_ascii=False)
    assert "must-not-persist" not in persisted_result
    assert "OSSAccessKeyId" not in persisted_result
    assert "Signature=" not in persisted_result
    assert execution["result"]["data"]["audio_url"] == "[redacted-signed-url]"
    assert execution["result"]["data"]["nested"]["api_token"] == "[redacted]"
    artifact_id = execution["artifacts"][0]["artifact_id"]
    ranged = client.get(
        f"/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content",
        headers={"X-Tenant-ID": "tenant-a", "Range": "bytes=0-4"},
    )
    assert ranged.status_code == 206
    assert ranged.content == b"agent"

    replay = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="diagram-1"),
        json={
            "stage": "assets",
            "tool_name": "diagram_gen",
            "inputs": {
                "diagram_type": "boxes",
                "boxes": [{"label": "CouncilForge"}, {"label": "OpenMontage"}],
                "output_path": "assets/images/architecture.png",
            },
        },
    )
    assert replay.status_code == 200
    assert replay.json()["execution_id"] == execution_id


def test_capability_workspace_cancel_is_tenant_scoped_and_idempotent(client: TestClient) -> None:
    workspace = create_workspace(client, key="workspace-cancel")
    workspace_id = workspace["workspace_id"]
    foreign = client.post(
        f"/v1/workspaces/{workspace_id}/cancel",
        headers={"X-Tenant-ID": "tenant-b"},
    )
    assert foreign.status_code == 404
    cancelled = client.post(
        f"/v1/workspaces/{workspace_id}/cancel",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    replay = client.post(
        f"/v1/workspaces/{workspace_id}/cancel",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert replay.status_code == 200
    current = client.get(
        f"/v1/workspaces/{workspace_id}",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert current.json()["status"] == "cancelled"


def test_capability_gateway_recovery_records_retryable_failure_event(client: TestClient) -> None:
    workspace = create_workspace(client, key="workspace-recovery")
    workspace_id = workspace["workspace_id"]
    gateway = client.app.state.capability_gateway
    execution_id = "execution_interrupted"
    execution_path = gateway._execution_path(workspace_id, execution_id)
    execution_path.parent.mkdir(parents=True, exist_ok=True)
    execution_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "execution_id": execution_id,
                "workspace_id": workspace_id,
                "tenant_id": "tenant-a",
                "pipeline": {"name": "animated-explainer", "version": "2.0"},
                "stage": "assets",
                "tool_name": "diagram_gen",
                "status": "running",
                "inputs_digest": "digest",
                "result": None,
                "error": None,
                "artifacts": [],
                "created_at": utc_now(),
                "started_at": utc_now(),
                "finished_at": None,
                "updated_at": utc_now(),
            }
        ),
        encoding="utf-8",
    )

    gateway.recover()

    recovered = gateway.get_execution(workspace_id, "tenant-a", execution_id)
    assert recovered["status"] == "failed"
    assert recovered["error"] == {
        "code": "ENGINE_RESTARTED",
        "message": "The tool worker restarted before this execution completed.",
        "retryable": True,
    }
    assert gateway.events(workspace_id, "tenant-a")[-1]["type"] == "execution.failed"


def test_agent_layer_three_skill_checkpoint_and_workspace_event_sequence(client: TestClient) -> None:
    workspace = create_workspace(client, key="workspace-checkpoint")
    workspace_id = workspace["workspace_id"]
    skill = client.get("/v1/agent-skills/remotion-best-practices")
    assert skill.status_code == 200
    assert skill.json()["content"]
    assert client.get("/v1/agent-skills/../secrets").status_code == 404

    checkpoint = client.put(
        f"/v1/workspaces/{workspace_id}/checkpoint",
        headers={"X-Tenant-ID": "tenant-a"},
        json={
            "stage": "research",
            "status": "in_progress",
            "artifacts": {},
            "metadata": {"agent_run_id": "run-1"},
        },
    )
    assert checkpoint.status_code == 200
    latest = client.get(
        f"/v1/workspaces/{workspace_id}/checkpoint",
        headers={"X-Tenant-ID": "tenant-a"},
    ).json()["checkpoint"]
    assert latest["stage"] == "research"
    assert latest["status"] == "in_progress"
    events = client.get(
        f"/v1/workspaces/{workspace_id}/events",
        headers={"X-Tenant-ID": "tenant-a"},
    ).json()["events"]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
