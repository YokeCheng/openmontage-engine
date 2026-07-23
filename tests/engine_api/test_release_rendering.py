"""Release rendering routes AI video scenes through durable media receipts."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine_api.renderer import _materialize_media
from engine_api.store import EngineStore
from tools.base_tool import ToolResult


class _Available:
    value = "available"


class _FakeVideoSelector:
    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[dict] = []

    def get_status(self) -> _Available:
        return _Available()

    def estimate_cost(self, _inputs: dict) -> float:
        return 0.25

    def execute(self, inputs: dict) -> ToolResult:
        self.calls += 1
        self.inputs.append(dict(inputs))
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated-video")
        return ToolResult(
            success=True,
            data={
                "output": str(output),
                "selected_provider": "kling",
                "selected_tool": "kling_official_video",
                "task_id": "kling-task-1",
                "remote_url": "https://provider.invalid/result?token=do-not-save",
            },
            artifacts=[str(output)],
            cost_usd=0.25,
            model="kling-v2",
        )


def test_ai_video_release_uses_stable_receipt_and_hard_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.tool_registry import registry

    selector = _FakeVideoSelector()
    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(
        registry,
        "get",
        lambda name: selector if name == "video_selector" else None,
    )
    manifest = {
        "title": "AI video receipt",
        "media_policy": {
            "visual_source": "ai_video",
            "video_provider": "kling",
            "voice_provider": "none",
            "music_provider": "none",
        },
        "budget": {"maximum_usd": 1.0},
        "render": {"aspect_ratio": "16:9"},
        "scenes": [
            {
                "scene_id": "scene-01",
                "title": "Motion",
                "duration_seconds": 5,
                "visual": {
                    "type": "video",
                    "prompt": "A precise product transformation",
                },
            }
        ],
    }
    props = {"cuts": [{"id": "scene-01", "in_seconds": 0, "out_seconds": 5}]}
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-release",
        "request_id": "request-release",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
    }
    asset_dir = tmp_path / "job" / "assets"

    first = _materialize_media(manifest, props, asset_dir, store, job)
    second = _materialize_media(manifest, props, asset_dir, store, job)

    assert selector.calls == 1
    assert selector.inputs[0]["allowed_providers"] == ["kling"]
    assert selector.inputs[0]["output_path"] == str(asset_dir / "scene-01.mp4")
    assert first[0]["metadata"]["external_task_id"] == "kling-task-1"
    assert second[0]["metadata"]["provider_call"] is False
    receipts = list((asset_dir / ".receipts").glob("*.json"))
    assert len(receipts) == 1
    assert "do-not-save" not in receipts[0].read_text(encoding="utf-8")


def test_ambiguous_video_charge_pauses_release_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine_api.renderer import MediaActionRequired
    from tools.tool_registry import registry

    class AmbiguousSelector(_FakeVideoSelector):
        def execute(self, inputs: dict) -> ToolResult:
            self.calls += 1
            self.inputs.append(dict(inputs))
            return ToolResult(
                success=False,
                data={"selected_provider": "kling", "task_id": "kling-unknown"},
                error="provider timeout",
                retryable=True,
            )

    selector = AmbiguousSelector()
    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(
        registry,
        "get",
        lambda name: selector if name == "video_selector" else None,
    )
    manifest = {
        "media_policy": {
            "visual_source": "ai_video",
            "video_provider": "kling",
            "voice_provider": "none",
            "music_provider": "none",
            "fallback": "ask",
        },
        "budget": {"maximum_usd": 1.0},
        "render": {"aspect_ratio": "16:9"},
        "scenes": [
            {
                "scene_id": "scene-01",
                "title": "Motion",
                "duration_seconds": 5,
                "visual": {"type": "video", "prompt": "Product motion"},
            }
        ],
    }
    props = {"cuts": [{"id": "scene-01", "in_seconds": 0, "out_seconds": 5}]}
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-ambiguous",
        "request_id": "request-ambiguous",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
        "status": "rendering",
    }

    with pytest.raises(MediaActionRequired):
        _materialize_media(
            manifest,
            props,
            tmp_path / "job" / "assets",
            store,
            job,
        )
    with pytest.raises(MediaActionRequired):
        _materialize_media(
            manifest,
            props,
            tmp_path / "job" / "assets",
            store,
            job,
        )

    assert selector.calls == 1


def test_quality_revision_reuses_unaffected_video_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.tool_registry import registry

    selector = _FakeVideoSelector()
    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(
        registry,
        "get",
        lambda name: selector if name == "video_selector" else None,
    )
    job_dir = tmp_path / "job"
    reused = job_dir / "inputs" / "reuse-video-scene-01" / "scene-01.mp4"
    reused.parent.mkdir(parents=True)
    reused.write_bytes(b"verified-prior-video")
    manifest = {
        "title": "Partial quality revision",
        "media_policy": {
            "visual_source": "ai_video",
            "video_provider": "kling",
            "voice_provider": "none",
            "music_provider": "none",
        },
        "budget": {"maximum_usd": 1.0},
        "render": {"aspect_ratio": "16:9"},
        "reuse_assets": [
            {
                "kind": "video",
                "scene_id": "scene-01",
                "source_scene_id": "scene-01",
                "asset_id": "reuse-video-scene-01",
                "platform_artifact_id": "artifact-video-scene-01",
            }
        ],
        "scenes": [
            {
                "scene_id": "scene-01",
                "title": "Unchanged shot",
                "duration_seconds": 5,
                "visual": {"type": "video", "prompt": "Keep prior shot"},
            }
        ],
    }
    props = {"cuts": [{"id": "scene-01"}]}
    job = {
        "job_id": "job-quality-reuse",
        "request_id": "request-quality-reuse",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
        "inputs": [
            {
                "asset_id": "reuse-video-scene-01",
                "storage_name": "inputs/reuse-video-scene-01/scene-01.mp4",
                "media_type": "video/mp4",
            }
        ],
    }

    assets = _materialize_media(
        manifest,
        props,
        job_dir / "assets",
        EngineStore(tmp_path / "runtime"),
        job,
    )

    assert selector.calls == 0
    assert props["cuts"][0]["backgroundVideo"] == (
        "inputs/reuse-video-scene-01/scene-01.mp4"
    )
    assert assets[0]["kind"] == "video"
    assert assets[0]["metadata"]["reused"] is True
