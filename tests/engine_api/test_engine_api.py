from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine_api import models as engine_models
from engine_api.app import create_app
from engine_api.renderer import (
    MediaActionRequired,
    _apply_bound_source_inputs,
    _fit_audio_to_timeline,
    _materialize_media,
    _media_artifact_metadata,
    _public_asset_src,
    _quality_report,
    _remotion_command,
    _render_contract,
)
from engine_api.store import EngineStore, new_id, utc_now
from tools.base_tool import RetryPolicy, ToolResult


def test_video_shot_request_requires_stable_execution_contract() -> None:
    assert hasattr(engine_models, "VideoShotRequest")
    request = engine_models.VideoShotRequest(
        scene_id="scene-01",
        operation="text_to_video",
        prompt="Kinetic typography reveals a single product benefit",
        duration_seconds=5,
        aspect_ratio="1:1",
        provider="kling",
        idempotency_key="job-1:final:scene-01",
        maximum_cost_usd=0.8,
    )

    assert request.provider == "kling"
    assert request.aspect_ratio == "1:1"
    assert request.idempotency_key == "job-1:final:scene-01"
    assert request.maximum_cost_usd == 0.8


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
        "render": {
            "aspect_ratio": "16:9",
            "width": 640,
            "height": 360,
            "fps": 24,
            "duration_seconds": 1,
        },
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


def test_engine_render_uses_pinned_non_interactive_remotion_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    cli = repo_root / "remotion-composer" / "node_modules" / ".bin" / "remotion"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setenv("OPENMONTAGE_REMOTION_CONCURRENCY", "8")
    browser = tmp_path / "Google Chrome"
    browser.write_text("", encoding="utf-8")
    monkeypatch.setenv(
        "OPENMONTAGE_REMOTION_BROWSER_EXECUTABLE",
        str(browser),
    )

    command = _remotion_command(
        repo_root,
        composition_id="Explainer",
        output_path=tmp_path / "final.mp4",
        props_path=tmp_path / "render-props.json",
        public_dir=tmp_path / "job",
    )

    assert command[0] == str(cli.resolve())
    assert command[1:4] == ["render", "src/index.tsx", "Explainer"]
    assert "npx" not in command
    assert f"--public-dir={tmp_path / 'job'}" in command
    assert "--concurrency=8" in command
    assert "--gl=angle" in command
    assert "--x264-preset=veryfast" in command
    assert f"--browser-executable={browser}" in command


def test_real_tts_artifact_metadata_includes_ffprobe_audio_details(tmp_path: Path) -> None:
    import subprocess

    audio = tmp_path / "narration.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.25",
            "-codec:a",
            "libmp3lame",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )

    metadata = _media_artifact_metadata(
        audio,
        {
            "kind": "audio",
            "provider": "dashscope",
            "tool": "tts_selector",
            "cost_usd": 0.01,
        },
    )

    assert metadata["provider"] == "dashscope"
    assert metadata["audio_codec"] == "mp3"
    assert 0.2 <= metadata["duration_seconds"] <= 0.4


def test_generated_image_metadata_includes_resolution_and_codec(tmp_path: Path) -> None:
    import subprocess

    image = tmp_path / "generated.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x49A38C:s=800x450",
            "-frames:v",
            "1",
            str(image),
        ],
        check=True,
        capture_output=True,
    )

    metadata = _media_artifact_metadata(
        image,
        {
            "kind": "image",
            "provider": "dashscope",
            "tool": "image_selector",
            "cost_usd": 0.02,
        },
    )

    assert metadata["width"] == 800
    assert metadata["height"] == 450
    assert metadata["image_codec"] == "png"


def test_quality_report_rejects_black_and_silent_narrated_delivery(tmp_path: Path) -> None:
    import subprocess

    output = tmp_path / "black-silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x360:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    payload = manifest()
    payload["audio"] = {"voice": "dashscope", "music": "none", "subtitles": True}
    report = _quality_report(
        output,
        payload,
        {
            "captionStyle": {
                "wordsPerPage": 1,
                "fontSize": 42,
                "maxWidthPercent": 72,
                "bottomMarginPercent": 6,
            }
        },
    )

    assert report["status"] == "failed"
    assert {"black_frames", "silence"}.issubset(
        {item["code"] for item in report["failed_checks"]}
    )


def test_remotion_media_is_served_from_the_tenant_job_public_directory(tmp_path: Path) -> None:
    public_dir = tmp_path / "job-a"
    narration = public_dir / "assets" / "narration.wav"
    narration.parent.mkdir(parents=True)
    narration.write_bytes(b"RIFF")

    assert _public_asset_src(narration, public_dir) == "assets/narration.wav"

    outside = tmp_path / "job-b" / "secret.wav"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"RIFF")
    with pytest.raises(ValueError, match="MEDIA_OUTSIDE_JOB_WORKSPACE"):
        _public_asset_src(outside, public_dir)


def test_real_narration_is_deterministically_fitted_without_truncating_words(tmp_path: Path) -> None:
    import subprocess

    source = tmp_path / "narration.wav"
    output = tmp_path / "narration-timeline.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1.2",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    fitted, metadata = _fit_audio_to_timeline(
        source,
        target_duration_seconds=0.9,
        output_path=output,
    )

    assert fitted == output.resolve()
    assert metadata["timeline_repaired"] is True
    assert metadata["timeline_fit_tool"] == "ffmpeg_atempo"
    assert metadata["original_duration_seconds"] >= 1.19
    assert metadata["fitted_duration_seconds"] <= 1.05
    assert 1.3 <= metadata["timeline_speed_factor"] <= 1.4


def test_real_tts_is_materialized_per_scene_on_the_approved_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Available:
        value = "available"

    class FakeTtsSelector:
        def __init__(self) -> None:
            self.inputs: list[dict] = []
            self.parallel_gate = threading.Barrier(2)

        def get_status(self) -> Available:
            return Available()

        def execute(self, inputs: dict) -> ToolResult:
            self.inputs.append(inputs)
            # Both requests must enter the provider concurrently. A sequential
            # implementation times out here and fails this regression test.
            self.parallel_gate.wait(timeout=1)
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"RIFF-per-scene")
            return ToolResult(
                success=True,
                data={"output_path": str(output), "selected_provider": "dashscope"},
                artifacts=[str(output)],
                cost_usd=0.001,
            )

    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "motion_graphics",
        "voice_provider": "dashscope",
        "music_provider": "none",
        "fallback": "ask",
    }
    payload["audio"] = {"voice": "auto", "voice_speed": 1, "music": "none"}
    payload["render"]["duration_seconds"] = 5
    payload["scenes"] = [
        {
            "scene_id": "scene-01",
            "title": "开场",
            "duration_seconds": 2,
            "narration": "第一句。",
            "visual": {"type": "motion_graphics", "prompt": "开场"},
        },
        {
            "scene_id": "scene-02",
            "title": "交付",
            "duration_seconds": 3,
            "narration": "第二句。",
            "visual": {"type": "motion_graphics", "prompt": "交付"},
        },
    ]
    props: dict = {"cuts": [{}, {}], "audio": {}}
    fake = FakeTtsSelector()

    from engine_api import renderer
    from tools.tool_registry import registry

    original_get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: fake if name == "tts_selector" else original_get(name))
    monkeypatch.setattr(
        renderer,
        "_fit_audio_to_timeline",
        lambda source_path, *, target_duration_seconds, output_path: (
            Path(source_path).resolve(),
            {
                "timeline_repaired": False,
                "original_duration_seconds": target_duration_seconds - 0.1,
                "fitted_duration_seconds": target_duration_seconds - 0.1,
                "timeline_speed_factor": 1.0,
            },
        ),
    )

    assets = _materialize_media(
        payload,
        props,
        tmp_path / "job" / "assets",
        EngineStore(tmp_path / "runtime"),
        {"job_id": "job-scene-audio"},
    )

    assert {item["text"] for item in fake.inputs} == {"第一句。", "第二句。"}
    assert props["audio"]["narration"]["segments"] == [
        {
            "src": "assets/narration-01.wav",
            "start_seconds": 0.0,
            "end_seconds": 2.0,
            "volume": 1,
        },
        {
            "src": "assets/narration-02.wav",
            "start_seconds": 2.0,
            "end_seconds": 5.0,
            "volume": 1,
        },
    ]
    assert [asset["metadata"]["scene_id"] for asset in assets] == ["scene-01", "scene-02"]

    retry_props: dict = {"cuts": [{}, {}], "audio": {}}
    retry_assets = _materialize_media(
        payload,
        retry_props,
        tmp_path / "job" / "assets",
        EngineStore(tmp_path / "runtime-retry"),
        {"job_id": "job-scene-audio-retry"},
    )

    assert len(fake.inputs) == 2
    assert [asset["cost_usd"] for asset in retry_assets] == [0.001, 0.001]
    assert (tmp_path / "job" / "assets" / "narration-01.receipt.json").is_file()


def test_shot_revision_reuses_unchanged_narration_and_calls_tts_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Available:
        value = "available"

    class FakeTtsSelector:
        def __init__(self) -> None:
            self.inputs: list[dict] = []

        def get_status(self) -> Available:
            return Available()

        def execute(self, inputs: dict) -> ToolResult:
            self.inputs.append(inputs)
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"RIFF-new-scene")
            return ToolResult(
                success=True,
                data={"output_path": str(output), "selected_provider": "dashscope"},
                artifacts=[str(output)],
                cost_usd=0.002,
            )

    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "motion_graphics",
        "voice_provider": "dashscope",
        "music_provider": "none",
        "fallback": "ask",
    }
    payload["audio"] = {"voice": "auto", "voice_speed": 1, "music": "none"}
    payload["render"]["duration_seconds"] = 5
    payload["scenes"] = [
        {
            "scene_id": "scene-01",
            "title": "保留",
            "duration_seconds": 2,
            "narration": "沿用第一句。",
            "visual": {"type": "motion_graphics", "prompt": "保留"},
        },
        {
            "scene_id": "scene-02",
            "title": "重做",
            "duration_seconds": 3,
            "narration": "这是修改后的第二句。",
            "visual": {"type": "motion_graphics", "prompt": "重做"},
        },
    ]
    payload["reuse_assets"] = [
        {
            "asset_id": "reuse-audio-01",
            "platform_artifact_id": "artifact-audio-v1",
            "scene_id": "scene-01",
            "kind": "narration",
        }
    ]
    job_dir = tmp_path / "job"
    reused_path = job_dir / "inputs" / "reuse-audio-01" / "narration.wav"
    reused_path.parent.mkdir(parents=True)
    reused_path.write_bytes(b"RIFF-reused-scene")
    job = {
        "job_id": "job-shot-redo",
        "tenant_id": "tenant-shot-redo",
        "inputs": [
            {
                "asset_id": "reuse-audio-01",
                "storage_name": "inputs/reuse-audio-01/narration.wav",
            }
        ],
    }
    props: dict = {"cuts": [{}, {}], "audio": {}}
    fake = FakeTtsSelector()

    from engine_api import renderer
    from tools.tool_registry import registry

    original_get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: fake if name == "tts_selector" else original_get(name))
    fit_calls: list[Path] = []

    def fit_once(source_path: Path, *, target_duration_seconds: float, output_path: Path):
        fit_calls.append(Path(source_path))
        return (
            Path(source_path).resolve(),
            {
                "timeline_repaired": False,
                "original_duration_seconds": target_duration_seconds,
                "fitted_duration_seconds": target_duration_seconds,
                "timeline_speed_factor": 1.0,
            },
        )

    monkeypatch.setattr(
        renderer,
        "_fit_audio_to_timeline",
        fit_once,
    )

    assets = _materialize_media(
        payload,
        props,
        job_dir / "assets",
        EngineStore(tmp_path / "runtime"),
        job,
    )

    assert [item["text"] for item in fake.inputs] == ["这是修改后的第二句。"]
    assert [asset["metadata"]["scene_id"] for asset in assets] == ["scene-01", "scene-02"]
    assert assets[0]["metadata"]["reused"] is True
    assert assets[0]["metadata"]["provider_call"] is False
    assert assets[0]["cost_usd"] == 0
    assert assets[1]["cost_usd"] == 0.002
    assert fit_calls == [job_dir / "assets" / "narration-02.wav"]


def test_shot_revision_reuses_unchanged_image_and_generates_only_changed_scene(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Available:
        value = "available"

    class FakeImageSelector:
        def __init__(self) -> None:
            self.inputs: list[dict] = []

        def get_status(self) -> Available:
            return Available()

        def execute(self, inputs: dict) -> ToolResult:
            self.inputs.append(dict(inputs))
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"new-image")
            return ToolResult(
                success=True,
                data={"output": str(output), "selected_provider": "dashscope"},
                artifacts=[str(output)],
                cost_usd=0.02,
            )

    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "ai_image",
        "image_provider": "dashscope",
        "voice_provider": "none",
        "music_provider": "none",
        "fallback": "ask",
    }
    payload["scenes"] = [
        {
            "scene_id": "scene-01",
            "title": "复用",
            "duration_seconds": 2,
            "narration": "复用",
            "visual": {"type": "image", "prompt": "保持原图"},
        },
        {
            "scene_id": "scene-02",
            "title": "重做",
            "duration_seconds": 3,
            "narration": "重做",
            "visual": {"type": "image", "prompt": "生成新图"},
        },
    ]
    payload["reuse_assets"] = [
        {
            "asset_id": "reuse-image-01",
            "platform_artifact_id": "artifact-image-v1",
            "scene_id": "scene-01",
            "source_scene_id": "scene-01",
            "kind": "image",
            "media_type": "image/png",
        }
    ]
    job_dir = tmp_path / "job"
    reused_path = job_dir / "inputs" / "reuse-image-01" / "scene-01.png"
    reused_path.parent.mkdir(parents=True)
    reused_path.write_bytes(b"reused-image")
    job = {
        "job_id": "job-image-redo",
        "tenant_id": "tenant-a",
        "inputs": [
            {
                "asset_id": "reuse-image-01",
                "storage_name": "inputs/reuse-image-01/scene-01.png",
                "media_type": "image/png",
            }
        ],
    }
    props = {"cuts": [{}, {}], "audio": {}}
    fake = FakeImageSelector()

    from tools.tool_registry import registry

    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(registry, "get", lambda name: fake if name == "image_selector" else None)

    assets = _materialize_media(
        payload,
        props,
        job_dir / "assets",
        EngineStore(tmp_path / "runtime"),
        job,
    )

    assert [item["scene_id"] for item in fake.inputs] == ["scene-02"]
    assert props["cuts"][0]["backgroundImage"] == "inputs/reuse-image-01/scene-01.png"
    assert props["cuts"][1]["backgroundImage"] == "assets/scene-02.png"
    assert assets[0]["metadata"] == {
        "scene_id": "scene-01",
        "prompt": "保持原图",
        "selection_reason": "",
        "ai_image_score": 0.0,
        "reused": True,
        "reused_from_artifact_id": "artifact-image-v1",
        "provider_call": False,
    }
    assert assets[0]["cost_usd"] == 0
    assert assets[1]["cost_usd"] == 0.02
    assert EngineStore(tmp_path / "runtime").events("job-image-redo")[0]["type"] == "media.asset_reused"


def test_shot_revision_rejects_cross_scene_narration_reuse(
    tmp_path: Path,
) -> None:
    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "motion_graphics",
        "voice_provider": "dashscope",
        "music_provider": "none",
        "fallback": "ask",
    }
    payload["audio"] = {"voice": "auto", "voice_speed": 1, "music": "none"}
    payload["scenes"] = [
        {
            "scene_id": "scene-01",
            "title": "第一镜头",
            "duration_seconds": 5,
            "narration": "第一镜头旁白。",
            "visual": {"type": "motion_graphics", "prompt": "第一镜头"},
        }
    ]
    payload["reuse_assets"] = [
        {
            "asset_id": "reuse-audio-02",
            "platform_artifact_id": "artifact-audio-v1-scene-02",
            "scene_id": "scene-01",
            "source_scene_id": "scene-02",
            "kind": "narration",
        }
    ]
    job_dir = tmp_path / "job"
    reused_path = job_dir / "inputs" / "reuse-audio-02" / "narration.wav"
    reused_path.parent.mkdir(parents=True)
    reused_path.write_bytes(b"RIFF-cross-scene")
    job = {
        "job_id": "job-cross-scene-reuse",
        "tenant_id": "tenant-shot-redo",
        "inputs": [
            {
                "asset_id": "reuse-audio-02",
                "storage_name": "inputs/reuse-audio-02/narration.wav",
            }
        ],
    }

    with pytest.raises(ValueError, match="REUSE_ASSET_SCENE_MISMATCH"):
        _materialize_media(
            payload,
            {"cuts": [{}], "audio": {}},
            job_dir / "assets",
            EngineStore(tmp_path / "runtime"),
            job,
        )
def test_approved_image_policy_executes_registry_selector_and_updates_explainer_props(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert props["cuts"][0]["backgroundImage"] == "assets/scene-01.png"
    assert assets[0]["provider"] == "test-provider"
    assert store.events("job-media")[0]["type"] == "media.asset_ready"


def test_hybrid_ai_images_only_generate_selected_scenes_in_parallel_and_keep_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Available:
        value = "available"

    class FakeImageSelector:
        def __init__(self) -> None:
            self.inputs: list[dict] = []
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def get_status(self) -> Available:
            return Available()

        def execute(self, inputs: dict) -> ToolResult:
            with self.lock:
                self.inputs.append(dict(inputs))
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            # Complete scene 03 first so the renderer must restore scene order.
            time.sleep(0.08 if inputs["scene_id"] == "scene-02" else 0.01)
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(f"generated-{inputs['scene_id']}".encode())
            with self.lock:
                self.active -= 1
            return ToolResult(
                success=True,
                data={
                    "output": str(output),
                    "selected_provider": "test-provider",
                    "selected_tool": "test-image",
                },
                artifacts=[str(output)],
                cost_usd=0.02,
                duration_seconds=0.08,
                model="image-v1",
            )

    from tools.tool_registry import registry

    fake = FakeImageSelector()
    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(registry, "get", lambda name: fake if name == "image_selector" else None)
    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "ai_image",
        "image_provider": "test-provider",
        "voice_provider": "none",
        "music_provider": "none",
    }
    payload["scenes"] = [
        {
            "scene_id": f"scene-{index:02d}",
            "title": title,
            "duration_seconds": 1,
            "narration": title,
            "visual": {
                "type": visual_type,
                "prompt": prompt,
                "selection_reason": reason,
                "ai_image_score": score,
            },
        }
        for index, (title, visual_type, prompt, reason, score) in enumerate(
            [
                ("标题", "motion_graphics", "标题动效", "文字镜头", 10),
                ("隐喻", "image", "透明容器中的发光记忆卡片", "关键概念隐喻", 95),
                ("取舍", "image", "新旧记忆卡片交替通过窗口", "关键过程隐喻", 90),
                ("总结", "motion_graphics", "总结动效", "行动号召", 20),
            ],
            start=1,
        )
    ]
    props = {
        "cuts": [
            {"id": f"scene-{index:02d}", "in_seconds": index - 1, "out_seconds": index}
            for index in range(1, 5)
        ]
    }
    store = EngineStore(tmp_path / "runtime")
    job = {"job_id": "job-hybrid-images", "tenant_id": "tenant-a", "correlation_id": "corr"}

    assets = _materialize_media(payload, props, tmp_path / "assets", store, job)

    assert {item["scene_id"] for item in fake.inputs} == {"scene-02", "scene-03"}
    assert all(item["allowed_providers"] == ["test-provider"] for item in fake.inputs)
    assert fake.max_active == 2
    assert "backgroundImage" not in props["cuts"][0]
    assert props["cuts"][1]["backgroundImage"] == "assets/scene-02.png"
    assert props["cuts"][2]["backgroundImage"] == "assets/scene-03.png"
    assert "backgroundImage" not in props["cuts"][3]
    assert [asset["metadata"]["scene_id"] for asset in assets] == ["scene-02", "scene-03"]
    assert assets[0]["metadata"] == {
        "scene_id": "scene-02",
        "prompt": "透明容器中的发光记忆卡片",
        "selection_reason": "关键概念隐喻",
        "ai_image_score": 95.0,
        "model": "image-v1",
        "generation_duration_seconds": 0.08,
        "provider_call": True,
        "reused": False,
    }
    assert [event["data"]["scene_id"] for event in store.events("job-hybrid-images")] == [
        "scene-02",
        "scene-03",
    ]


def test_image_retry_reuses_successful_calls_without_losing_their_cost_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Available:
        value = "available"

    class FakeImageSelector:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def get_status(self) -> Available:
            return Available()

        def execute(self, inputs: dict) -> ToolResult:
            scene_id = str(inputs["scene_id"])
            self.calls[scene_id] = self.calls.get(scene_id, 0) + 1
            if scene_id == "scene-01" and self.calls[scene_id] == 1:
                return ToolResult(
                    success=False,
                    error="temporary provider failure",
                    error_code="rate_limit",
                    retryable=True,
                )
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(f"generated-{scene_id}".encode())
            return ToolResult(
                success=True,
                data={
                    "output": str(output),
                    "selected_provider": "dashscope",
                    "selected_tool": "dashscope_image",
                },
                artifacts=[str(output)],
                cost_usd=0.02,
                duration_seconds=0.5,
                model="qwen-image-2.0-pro",
            )

    from tools.tool_registry import registry

    fake = FakeImageSelector()
    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(registry, "get", lambda name: fake if name == "image_selector" else None)
    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "ai_image",
        "image_provider": "dashscope",
        "voice_provider": "none",
        "music_provider": "none",
        "fallback": "ask",
    }
    payload["scenes"] = [
        {
            "scene_id": f"scene-{index:02d}",
            "title": f"Scene {index}",
            "duration_seconds": 1,
            "narration": f"Scene {index}",
            "visual": {
                "type": "image",
                "prompt": f"Prompt {index}",
                "selection_reason": f"Reason {index}",
                "ai_image_score": 100 - index,
            },
        }
        for index in range(1, 4)
    ]
    props = {
        "cuts": [
            {"id": f"scene-{index:02d}", "in_seconds": index - 1, "out_seconds": index}
            for index in range(1, 4)
        ]
    }
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-image-retry-receipts",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
        "status": "rendering",
        "stage": "media",
        "progress": {"percent": 52, "message": "media", "updated_at": utc_now()},
    }
    store.save_job(job)

    with pytest.raises(MediaActionRequired):
        _materialize_media(payload, props, tmp_path / "assets", store, job)

    job["actions"] = []
    job["status"] = "rendering"
    assets = _materialize_media(payload, props, tmp_path / "assets", store, job)

    assert fake.calls == {"scene-01": 2, "scene-02": 1, "scene-03": 1}
    assert [asset["metadata"]["scene_id"] for asset in assets] == [
        "scene-01",
        "scene-02",
        "scene-03",
    ]
    assert sum(float(asset["cost_usd"]) for asset in assets) == pytest.approx(0.06)
    recovered = assets[1:]
    assert all(asset["provider"] == "dashscope" for asset in recovered)
    assert all(asset["metadata"]["provider_call"] is True for asset in recovered)
    assert all(asset["metadata"]["reused"] is False for asset in recovered)
    assert all(asset["metadata"]["resumed_from_cached_generation"] is True for asset in recovered)


def test_unavailable_approved_media_pauses_for_fallback_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_failed_image_result_preserves_a_redacted_provider_reason_in_the_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Available:
        value = "available"

    secret = "dashscope-secret-must-not-persist"

    class FakeImageSelector:
        def get_status(self) -> Available:
            return Available()

        def execute(self, _inputs: dict) -> ToolResult:
            return ToolResult(
                success=False,
                error=f"DashScope HTTP 429: api_key={secret}; quota exhausted",
                error_code="rate_limit",
                retryable=True,
            )

    from tools.tool_registry import registry

    monkeypatch.setenv("DASHSCOPE_API_KEY", secret)
    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(
        registry,
        "get",
        lambda name: FakeImageSelector() if name == "image_selector" else None,
    )
    payload = manifest()
    payload["media_policy"] = {
        "visual_source": "ai_image",
        "image_provider": "dashscope",
        "voice_provider": "none",
        "music_provider": "none",
        "fallback": "ask",
    }
    payload["scenes"][0]["visual"] = {
        "type": "image",
        "prompt": "A clear visual metaphor",
    }
    props = {"cuts": [{"id": "scene-01", "in_seconds": 0, "out_seconds": 1}]}
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-provider-reason",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
        "status": "rendering",
        "stage": "media",
        "progress": {"percent": 52, "message": "media", "updated_at": utc_now()},
    }
    store.save_job(job)

    with pytest.raises(MediaActionRequired):
        _materialize_media(payload, props, tmp_path / "assets", store, job)

    paused = store.load_job("job-provider-reason")
    assert paused is not None
    context = paused["actions"][0]["context"]
    assert context == {
        "scene_id": "scene-01",
        "capability": "ai_image",
        "provider": "dashscope",
        "provider_error": "DashScope HTTP 429: api_key=[redacted]; quota exhausted",
        "error_code": "rate_limit",
        "retryable": True,
    }
    assert secret not in json.dumps(paused)


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


def create(
    client: TestClient,
    *,
    title: str = "CouncilForge",
    tenant: str = "tenant-a",
    key: str = "create-1",
) -> dict:
    response = client.post("/v1/jobs", json=request_body(title, tenant), headers=headers(tenant, key))
    assert response.status_code == 202
    return response.json()


def test_create_waits_for_script_and_budget_approval_and_never_persists_credentials(
    client: TestClient,
) -> None:
    job = create(client)
    assert job["status"] == "waiting_approval"
    assert job["approval"]["status"] == "pending"
    runtime_text = "\n".join(path.read_text(errors="ignore") for path in client.app.state.store.root.rglob("*") if path.is_file())
    assert "must-never-persist" not in runtime_text
    assert "credential_grants" not in runtime_text


def test_platform_managed_job_skips_duplicate_engine_approval(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = tmp_path / "platform-approved.mp4"
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x14232D:s=640x360:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(fixture),
        ],
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


def test_platform_managed_job_accepts_verified_deferred_source_inputs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    asset_id = "asset_1234567890abcdef1234567890abcdef"
    payload = b"approved-logo-bytes"
    checksum = hashlib.sha256(payload).hexdigest()
    body = request_body(execution_mode="platform_managed")
    body["defer_start"] = True
    body["input"]["source_materials"] = [
        {
            "platform_asset_id": asset_id,
            "filename": "brand-logo.png",
            "media_type": "image/png",
            "checksum_sha256": checksum,
        }
    ]
    body["input"]["scenes"][0]["visual"]["asset_id"] = asset_id
    created = client.post(
        "/v1/jobs",
        json=body,
        headers=headers(key="deferred-input"),
    )
    assert created.status_code == 202
    job = created.json()
    assert job["status"] == "created"
    assert job["stage"] == "inputs"

    missing = client.post(
        f"/v1/jobs/{job['job_id']}/start",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert missing.status_code == 409
    assert missing.json()["error_code"] == "SOURCE_INPUTS_MISSING"
    assert client.put(
        f"/v1/jobs/{job['job_id']}/inputs/{asset_id}",
        headers={
            "X-Tenant-ID": "tenant-b",
            "X-File-Name": "brand-logo.png",
            "X-Content-SHA256": checksum,
            "Content-Type": "image/png",
        },
        content=payload,
    ).status_code == 404
    mismatch = client.put(
        f"/v1/jobs/{job['job_id']}/inputs/{asset_id}",
        headers={
            "X-Tenant-ID": "tenant-a",
            "X-File-Name": "brand-logo.png",
            "X-Content-SHA256": "0" * 64,
            "Content-Type": "image/png",
        },
        content=payload,
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error_code"] == "SOURCE_INPUT_CHECKSUM_MISMATCH"

    uploaded = client.put(
        f"/v1/jobs/{job['job_id']}/inputs/{asset_id}",
        headers={
            "X-Tenant-ID": "tenant-a",
            "X-File-Name": "brand-logo.png",
            "X-Content-SHA256": checksum,
            "Content-Type": "image/png",
        },
        content=payload,
    )
    assert uploaded.status_code == 201
    assert "storage_name" not in uploaded.json()
    replay = client.put(
        f"/v1/jobs/{job['job_id']}/inputs/{asset_id}",
        headers={
            "X-Tenant-ID": "tenant-a",
            "X-File-Name": "brand-logo.png",
            "X-Content-SHA256": checksum,
            "Content-Type": "image/png",
        },
        content=payload,
    )
    assert replay.status_code == 200
    monkeypatch.setattr(client.app.state.scheduler, "submit", lambda _job_id: None)
    started = client.post(
        f"/v1/jobs/{job['job_id']}/start",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "running"


def test_platform_managed_job_requires_declared_reuse_inputs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    asset_id = "asset_abcdefabcdefabcdefabcdefabcdefab"
    payload = b"RIFF-reused-narration"
    checksum = hashlib.sha256(payload).hexdigest()
    body = request_body(execution_mode="platform_managed")
    body["defer_start"] = True
    body["input"]["reuse_assets"] = [
        {
            "asset_id": asset_id,
            "platform_artifact_id": "artifact-audio-v1",
            "scene_id": "scene-01",
            "kind": "narration",
            "media_type": "audio/wav",
            "checksum_sha256": checksum,
        }
    ]
    created = client.post(
        "/v1/jobs",
        json=body,
        headers=headers(key="deferred-reuse-input"),
    )
    assert created.status_code == 202
    job = created.json()
    missing = client.post(
        f"/v1/jobs/{job['job_id']}/start",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert missing.status_code == 409
    assert asset_id in missing.json()["detail"]
    uploaded = client.put(
        f"/v1/jobs/{job['job_id']}/inputs/{asset_id}",
        headers={
            "X-Tenant-ID": "tenant-a",
            "X-File-Name": "narration.wav",
            "X-Content-SHA256": checksum,
            "Content-Type": "audio/wav",
        },
        content=payload,
    )
    assert uploaded.status_code == 201
    monkeypatch.setattr(client.app.state.scheduler, "submit", lambda _job_id: None)
    started = client.post(
        f"/v1/jobs/{job['job_id']}/start",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert started.status_code == 200


def test_bound_source_image_is_materialized_as_a_remotion_background(tmp_path: Path) -> None:
    job_dir = tmp_path / "job-source"
    source = job_dir / "inputs" / "asset_logo" / "logo.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-source",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
        "inputs": [
            {
                "asset_id": "asset_logo",
                "storage_name": "inputs/asset_logo/logo.png",
                "media_type": "image/png",
            }
        ],
    }
    payload = manifest()
    payload["scenes"][0]["visual"]["asset_id"] = "asset_logo"
    props = {"cuts": [{"id": "scene-01"}]}

    approved = _apply_bound_source_inputs(payload, props, job_dir, store, job)

    assert approved["asset_logo"]["path"] == source
    assert props["cuts"][0]["backgroundImage"] == "inputs/asset_logo/logo.png"
    assert props["cuts"][0]["backgroundOverlay"] == pytest.approx(0.28)


def test_uploaded_music_is_bound_with_narration_ducking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.tool_registry import registry

    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    job_dir = tmp_path / "job-music"
    music = job_dir / "inputs" / "asset_music" / "theme.mp3"
    music.parent.mkdir(parents=True)
    music.write_bytes(b"ID3-approved-music")
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-music",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
        "inputs": [
            {
                "asset_id": "asset_music",
                "storage_name": "inputs/asset_music/theme.mp3",
                "media_type": "audio/mpeg",
            }
        ],
    }
    payload = manifest()
    payload["audio"] = {
        "voice": "none",
        "music": "uploaded",
        "music_asset_id": "asset_music",
    }
    payload["media_policy"] = {
        "visual_source": "motion_graphics",
        "voice_provider": "none",
        "music_provider": "uploaded",
        "fallback": "ask",
    }
    props = {"cuts": [{"id": "scene-01"}]}

    media_assets = _materialize_media(payload, props, job_dir / "assets", store, job)

    assert len(media_assets) == 1
    assert media_assets[0]["role"] == "background_music"
    assert media_assets[0]["metadata"]["license"]["source"] == "user_upload"
    assert props["audio"]["music"]["src"] == "inputs/asset_music/theme.mp3"
    assert props["audio"]["music"]["duckingVolume"] < props["audio"]["music"]["volume"]


def test_failed_job_retry_is_idempotent_and_reuses_the_same_workspace(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = request_body(execution_mode="platform_managed")
    body["defer_start"] = True
    created = client.post(
        "/v1/jobs",
        json=body,
        headers=headers(key="retry-source"),
    ).json()
    stored = client.app.state.store.load_job(created["job_id"])
    assert stored is not None
    stored["status"] = "failed"
    stored["stage"] = "quality"
    stored["error"] = {"code": "QUALITY_GATE_FAILED", "message": "black frames", "retryable": True}
    client.app.state.store.save_job(stored)
    submitted: list[str] = []
    monkeypatch.setattr(client.app.state.scheduler, "submit", submitted.append)

    first = client.post(
        f"/v1/jobs/{created['job_id']}/retry",
        headers={"X-Tenant-ID": "tenant-a", "Idempotency-Key": "retry-1"},
    )
    replay = client.post(
        f"/v1/jobs/{created['job_id']}/retry",
        headers={"X-Tenant-ID": "tenant-a", "Idempotency-Key": "retry-1"},
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["job_id"] == created["job_id"]
    assert first.json()["retry_attempt"] == 1
    assert first.json()["error"] is None
    assert submitted == [created["job_id"]]


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
        repeated = client.put(
            "/v1/runtime/config",
            json={"values": {"PIXABAY_API_KEY": "runtime-only-secret"}},
        )
        assert repeated.status_code == 200
        assert repeated.json() == configured.json()
        runtime_text = "\n".join(path.read_text(errors="ignore") for path in client.app.state.store.root.rglob("*") if path.is_file())
        assert "runtime-only-secret" not in runtime_text
    finally:
        import os

        from tools.tool_registry import registry

        os.environ.pop("PIXABAY_API_KEY", None)
        registry.clear()

    rejected = client.put("/v1/runtime/config", json={"values": {"NOT_A_PROVIDER_FIELD": "secret"}})
    assert rejected.status_code == 422
    assert rejected.json()["error_code"] == "RUNTIME_CONFIG_FIELD_UNSUPPORTED"


def test_runtime_configuration_refresh_does_not_block_health(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_refresh(values: dict[str, str | None]) -> dict:
        started.set()
        assert release.wait(timeout=3)
        return {
            "configured_fields": sorted(key for key, value in values.items() if value),
            "capabilities": {},
            "providers": [],
        }

    monkeypatch.setattr("engine_api.app._allowed_runtime_fields", lambda: {"PIXABAY_API_KEY"})
    monkeypatch.setattr("engine_api.app._apply_runtime_config", slow_refresh)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            client.put,
            "/v1/runtime/config",
            json={"values": {"PIXABAY_API_KEY": "runtime-only-secret"}},
        )
        assert started.wait(timeout=2)
        health = client.get("/v1/health")
        assert health.status_code == 200
        assert health.json()["ready"] is True
        release.set()
        assert pending.result(timeout=3).status_code == 200


def test_pipeline_catalog_exposes_platform_contract_readiness(
    client: TestClient,
) -> None:
    response = client.get("/v1/pipelines")
    assert response.status_code == 200
    pipelines = {item["name"]: item for item in response.json()["pipelines"]}

    explainer = pipelines["animated-explainer"]["platform_contract"]
    assert explainer["version"] == "2.0"
    assert explainer["readiness"] == "ready"
    assert explainer["intake_adapter"] == "councilforge-video-brief-v1"
    assert explainer["intake_schema"] == "councilforge-video-brief-v1"
    assert explainer["acceptance"]["status"] == "validated"
    assert explainer["acceptance"]["evidence_id"] == "job_25c18f1fc69c45c3872545d36ea017bd"
    assert "knowledge_explainer" in explainer["supported_formats"]
    assert explainer["required_source_materials"] == []
    assert {item["capability"] for item in explainer["capability_requirements"]} >= {
        "video_post",
        "tts",
        "image_generation",
        "video_generation",
    }

    animation = pipelines["animation"]["platform_contract"]
    assert animation["readiness"] == "ready"
    assert animation["intake_adapter"] == "councilforge-video-brief-v1"
    assert animation["required_source_materials"] == []
    assert {item["capability"] for item in animation["capability_requirements"]} >= {
        "video_post",
        "graphics",
        "tts",
        "music_library",
    }

    talking_head = pipelines["talking-head"]["platform_contract"]
    assert talking_head["readiness"] == "ready"
    assert talking_head["intake_adapter"] == "councilforge-source-materials-v1"
    assert talking_head["required_source_materials"] == ["raw_talking_head_video"]

    screen_demo = pipelines["screen-demo"]["platform_contract"]
    assert screen_demo["readiness"] == "ready"
    assert screen_demo["intake_adapter"] == "councilforge-source-materials-v1"
    assert screen_demo["required_source_materials"] == ["screen_recording_or_terminal_script"]
    screen_bundle = client.get("/v1/pipelines/screen-demo/bundle")
    assert screen_bundle.status_code == 200
    screen_script = next(stage for stage in screen_bundle.json()["manifest"]["stages"] if stage["name"] == "script")
    assert "transcriber" not in screen_script.get("required_tools", [])
    assert "transcriber" in screen_script.get("optional_tools", [])

    source_driven = {
        name: {
            "readiness": pipelines[name]["platform_contract"]["readiness"],
            "intake_adapter": pipelines[name]["platform_contract"]["intake_adapter"],
            "required_source_materials": pipelines[name]["platform_contract"]["required_source_materials"],
        }
        for name in [
            "clip-factory",
            "podcast-repurpose",
            "localization-dub",
            "documentary-montage",
        ]
    }
    assert source_driven == {
        "clip-factory": {
            "readiness": "ready",
            "intake_adapter": "councilforge-source-materials-v1",
            "required_source_materials": ["long_form_video_or_audio"],
        },
        "podcast-repurpose": {
            "readiness": "ready",
            "intake_adapter": "councilforge-source-materials-v1",
            "required_source_materials": ["podcast_audio_or_video"],
        },
        "localization-dub": {
            "readiness": "ready",
            "intake_adapter": "councilforge-source-materials-v1",
            "required_source_materials": ["source_video", "target_languages"],
        },
        "documentary-montage": {
            "readiness": "requires_input_adapter",
            "intake_adapter": "documentary-source-brief-v1",
            "required_source_materials": ["archive_or_stock_source_collection"],
        },
    }
    for name in [
        "talking-head",
        "clip-factory",
        "podcast-repurpose",
        "localization-dub",
    ]:
        assert client.get(f"/v1/pipelines/{name}/bundle").status_code == 200


def test_pipeline_bundle_exposes_manifest_and_stage_director_instructions(
    client: TestClient,
) -> None:
    response = client.get("/v1/pipelines/animation/bundle")
    assert response.status_code == 200
    bundle = response.json()
    assert bundle["manifest"]["name"] == "animation"
    assert bundle["stages"]
    assert any(stage["instruction"] for stage in bundle["stages"])
    assets_stage = next(stage for stage in bundle["manifest"]["stages"] if stage["name"] == "assets")
    assert "music_library" in assets_stage["tools_available"]
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


def test_approval_is_idempotent_and_render_registers_range_artifact(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.mp4"
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x14232D:s=640x360:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(fixture),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("OPENMONTAGE_ENGINE_RENDER_MODE", "fixture-copy")
    monkeypatch.setenv("OPENMONTAGE_ENGINE_FIXTURE_VIDEO", str(fixture))
    job = create(client)
    body = {
        "approval_id": job["approval"]["approval_id"],
        "decision": "approved",
        "decided_by": "user-1",
    }
    first = client.post(
        f"/v1/jobs/{job['job_id']}/approve",
        headers={"X-Tenant-ID": "tenant-a"},
        json=body,
    )
    assert first.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        current = client.get(f"/v1/jobs/{job['job_id']}", headers={"X-Tenant-ID": "tenant-a"}).json()
        if current["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert current["status"] == "succeeded"
    assert current["artifacts"][0]["checksum"]["algorithm"] == "sha256"
    cover = next(item for item in current["artifacts"] if item["kind"] == "image")
    assert cover["role"] == "final"
    assert cover["media_type"] == "image/jpeg"
    assert cover["metadata"]["image_format"] == "JPEG"
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
    replay = client.post(
        f"/v1/jobs/{job['job_id']}/approve",
        headers={"X-Tenant-ID": "tenant-a"},
        json=body,
    )
    assert replay.status_code == 200


def test_action_resolution_cancel_event_sequence_and_restart_recovery(
    client: TestClient,
) -> None:
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


def test_ai_image_fallback_only_changes_the_failed_scene(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = create(client)
    stored = client.app.state.store.load_job(job["job_id"])
    assert stored is not None
    stored["input"]["media_policy"] = {
        "visual_source": "ai_image",
        "image_provider": "dashscope",
        "fallback": "ask",
    }
    stored["input"]["scenes"] = [
        {
            "scene_id": "scene-01",
            "title": "开场",
            "duration_seconds": 1,
            "narration": "开场",
            "visual": {"type": "motion_graphics", "prompt": "标题动效"},
        },
        {
            "scene_id": "scene-02",
            "title": "失败镜头",
            "duration_seconds": 1,
            "narration": "失败镜头",
            "visual": {"type": "image", "prompt": "失败图片"},
        },
        {
            "scene_id": "scene-03",
            "title": "保留镜头",
            "duration_seconds": 1,
            "narration": "保留镜头",
            "visual": {"type": "image", "prompt": "保留图片"},
        },
    ]
    action_id = new_id("action")
    stored["status"] = "waiting_action"
    stored["actions"] = [
        {
            "action_id": action_id,
            "job_id": job["job_id"],
            "type": "provider_fallback",
            "status": "pending",
            "summary": "Image provider failed",
            "options": [
                {"value": "use_motion_graphics", "label": "Use motion graphics"},
                {"value": "retry", "label": "Retry"},
            ],
            "context": {"scene_id": "scene-02", "capability": "ai_image"},
            "created_at": utc_now(),
        }
    ]
    client.app.state.store.save_job(stored)
    submitted: list[str] = []
    monkeypatch.setattr(client.app.state.scheduler, "submit", submitted.append)

    response = client.post(
        f"/v1/jobs/{job['job_id']}/actions/{action_id}/resolve",
        headers={"X-Tenant-ID": "tenant-a"},
        json={"resolution": "use_motion_graphics", "resolved_by": "user-1"},
    )

    assert response.status_code == 200
    updated = client.app.state.store.load_job(job["job_id"])
    assert updated is not None
    assert updated["input"]["media_policy"]["visual_source"] == "ai_image"
    assert updated["input"]["scenes"][1]["visual"]["type"] == "motion_graphics"
    assert updated["input"]["scenes"][2]["visual"]["type"] == "image"
    assert submitted == [job["job_id"]]


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
            "metadata": {
                "platform_task_id": "video-task-1",
                "source_materials": [
                    {
                        "kind": "source_video",
                        "platform_asset_id": "asset_1",
                        "platform_path": "/tmp/demo.mp4",
                    }
                ],
            },
        },
    )
    assert response.status_code == 201
    return response.json()


def test_capability_workspace_is_idempotent_tenant_isolated_and_exposes_stage_skill(
    client: TestClient,
) -> None:
    workspace = create_workspace(client)
    replay = client.post(
        "/v1/workspaces",
        headers=headers(key="workspace-1"),
        json={
            "request_id": "video-task-1",
            "title": "Agent-hosted explainer",
            "pipeline": "animated-explainer",
            "metadata": {
                "platform_task_id": "video-task-1",
                "source_materials": [
                    {
                        "kind": "source_video",
                        "platform_asset_id": "asset_1",
                        "platform_path": "/tmp/demo.mp4",
                    }
                ],
            },
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
    assert (
        client.get(
            f"/v1/workspaces/{workspace['workspace_id']}",
            headers={"X-Tenant-ID": "tenant-b"},
        ).status_code
        == 404
    )

    context = client.get(
        f"/v1/workspaces/{workspace['workspace_id']}/stages/assets/context",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert context.status_code == 200
    payload = context.json()
    assert payload["stage"]["name"] == "assets"
    assert payload["workspace_metadata"]["source_materials"][0]["platform_asset_id"] == "asset_1"
    assert "asset" in payload["instruction"].lower()
    assert {tool["name"] for tool in payload["tools"]} >= {
        "diagram_gen",
        "image_selector",
        "subtitle_gen",
        "tts_selector",
    }
    assert payload["artifact_schemas"]["asset_manifest"]["title"]


def test_capability_gateway_executes_only_stage_tools_and_serves_artifacts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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
            cost_usd=0.125,
            duration_seconds=1.75,
            model="diagram-test-v1",
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
            "trace_id": "trace-video-001",
            "platform_job_id": "job-video-001",
            "stage_attempt": 2,
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
    assert execution["trace_id"] == "trace-video-001"
    assert execution["platform_job_id"] == "job-video-001"
    assert execution["stage_attempt"] == 2
    assert execution["provider"] == "mermaid"
    assert execution["artifacts"]
    assert execution["artifacts"][0]["checksum"]
    assert "metadata" in execution["artifacts"][0]
    persisted_result = json.dumps(execution["result"], ensure_ascii=False)
    assert "must-not-persist" not in persisted_result
    assert "OSSAccessKeyId" not in persisted_result
    assert "Signature=" not in persisted_result
    assert execution["result"]["data"]["audio_url"] == "[redacted-signed-url]"
    assert execution["result"]["data"]["nested"]["api_token"] == "[redacted]"
    assert execution["result"]["cost_usd"] == 0.125
    assert execution["result"]["duration_seconds"] == 1.75
    events = client.get(
        f"/v1/workspaces/{workspace_id}/events",
        headers={"X-Tenant-ID": "tenant-a"},
    ).json()["events"]
    completed = next(event for event in events if event["type"] == "execution.succeeded")
    assert completed["trace_id"] == "trace-video-001"
    assert completed["platform_job_id"] == "job-video-001"
    assert completed["stage"] == "assets"
    assert completed["stage_attempt"] == 2
    assert completed["execution_id"] == execution_id
    assert completed["tool_name"] == "diagram_gen"
    assert completed["provider"] == "mermaid"
    assert completed["data"]["cost_usd"] == 0.125
    assert completed["data"]["duration_seconds"] == 1.75
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


def test_capability_gateway_redacts_provider_errors_from_execution_events(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(client, key="workspace-redacted-error")
    workspace_id = workspace["workspace_id"]
    from tools.tool_registry import registry

    registry.ensure_discovered()
    diagram = registry.get("diagram_gen")
    assert diagram is not None
    secret = "provider-secret-must-never-persist"
    monkeypatch.setenv("DASHSCOPE_API_KEY", secret)

    def fail_with_secret(_inputs: dict) -> ToolResult:
        raise RuntimeError(f"Bearer {secret}; api_key={secret}; request failed")

    monkeypatch.setattr(diagram, "execute", fail_with_secret)
    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="redacted-failure"),
        json={
            "stage": "assets",
            "tool_name": "diagram_gen",
            "trace_id": "trace-redacted-error",
            "platform_job_id": "job-redacted-error",
            "stage_attempt": 3,
            "inputs": {"output_path": "assets/images/failure.png"},
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
        if execution["status"] == "failed":
            break
        time.sleep(0.02)
    events = client.get(
        f"/v1/workspaces/{workspace_id}/events",
        headers={"X-Tenant-ID": "tenant-a"},
    ).json()["events"]
    persisted = json.dumps({"execution": execution, "events": events})
    assert secret not in persisted
    assert "[redacted]" in persisted
    failed = events[-1]
    assert failed["type"] == "execution.failed"
    assert failed["trace_id"] == "trace-redacted-error"
    assert failed["platform_job_id"] == "job-redacted-error"
    assert failed["stage_attempt"] == 3
    assert failed["data"]["error_code"] == "TOOL_EXCEPTION"


def test_capability_gateway_retries_retryable_tool_results_and_records_attempts(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(client, key="workspace-tool-retry")
    workspace_id = workspace["workspace_id"]
    from tools.tool_registry import registry

    registry.ensure_discovered()
    diagram = registry.get("diagram_gen")
    assert diagram is not None
    monkeypatch.setattr(
        diagram,
        "retry_policy",
        RetryPolicy(max_retries=2, backoff_seconds=0),
    )
    calls: dict[str, int] = {}

    def retry_then_succeed(inputs: dict) -> ToolResult:
        output_name = str(inputs.get("output_path") or "")
        calls[output_name] = calls.get(output_name, 0) + 1
        if output_name.endswith("retry.png") and calls[output_name] < 3:
            return ToolResult(
                success=False,
                error="provider rate limited",
                error_code="PROVIDER_RATE_LIMITED",
                retryable=True,
            )
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"retry-success")
        return ToolResult(success=True, artifacts=[str(output)])

    monkeypatch.setattr(diagram, "execute", retry_then_succeed)
    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="tool-retry"),
        json={
            "stage": "assets",
            "tool_name": "diagram_gen",
            "inputs": {"output_path": "assets/images/retry.png"},
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
    assert execution["attempt_count"] == 3, calls
    assert execution["result"]["attempt_count"] == 3
    assert len(execution["retry_history"]) == 2
    events = client.get(
        f"/v1/workspaces/{workspace_id}/events",
        headers={"X-Tenant-ID": "tenant-a"},
    ).json()["events"]
    assert [event["type"] for event in events].count("execution.retrying") == 2


def test_capability_gateway_injects_workspace_local_remotion_paths(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = create_workspace(client, key="workspace-remotion-defaults")
    workspace_id = workspace["workspace_id"]

    from tools.tool_registry import registry

    registry.ensure_discovered()
    remotion = registry.get("remotion_motion_graphics")
    assert remotion is not None

    seen_inputs: dict[str, object] = {}

    def fake_execute(inputs: dict) -> ToolResult:
        seen_inputs.update(inputs)
        output_dir = Path(str(inputs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "composition-props.json"
        output.write_text("{}", encoding="utf-8")
        return ToolResult(success=True, data={"props_path": str(output)}, artifacts=[str(output)])

    monkeypatch.setattr(remotion, "execute", fake_execute)
    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="remotion-defaults"),
        json={
            "stage": "assets",
            "tool_name": "remotion_motion_graphics",
            "inputs": {
                "operation": "prepare",
                "title": "CouncilForge",
                "objective": "Explain the platform",
                "scenes": [
                    {
                        "scene_id": "scene-1",
                        "title": "One brain",
                        "narration": "CouncilForge plans, OpenMontage executes.",
                        "description": "A clean platform workflow diagram.",
                        "start_seconds": 0,
                        "end_seconds": 1,
                    }
                ],
                "render": {
                    "width": 640,
                    "height": 360,
                    "fps": 24,
                    "duration_seconds": 1,
                },
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
    output_dir = Path(str(seen_inputs["output_dir"]))
    assert workspace_id in output_dir.parts
    assert execution["artifacts"]
    assert execution["artifacts"][0]["path"].startswith("tool-output/assets/remotion-motion/")


def test_capability_gateway_injects_workspace_local_video_compose_output_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = create_workspace(client, key="workspace-video-compose-default")
    workspace_id = workspace["workspace_id"]

    from tools.tool_registry import registry

    registry.ensure_discovered()
    video_compose = registry.get("video_compose")
    assert video_compose is not None

    seen_inputs: dict[str, object] = {}

    def fake_execute(inputs: dict) -> ToolResult:
        seen_inputs.update(inputs)
        output = Path(str(inputs["output_path"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"workspace-local-video")
        return ToolResult(success=True, data={"output_path": str(output)}, artifacts=[str(output)])

    monkeypatch.setattr(video_compose, "execute", fake_execute)
    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="video-compose-default"),
        json={
            "stage": "compose",
            "tool_name": "video_compose",
            "inputs": {
                "edit_decisions": {
                    "cuts": [
                        {
                            "id": "scene-1",
                            "type": "text_card",
                            "text": "CouncilForge",
                            "in_seconds": 0,
                            "out_seconds": 1,
                        }
                    ]
                }
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
    output_path = Path(str(seen_inputs["output_path"]))
    assert output_path.name == "final.mp4"
    assert output_path.parent.name == "renders"
    assert workspace_id in output_path.parts
    assert execution["artifacts"]
    assert execution["artifacts"][0]["path"] == "renders/final.mp4"


def test_capability_gateway_resolves_manifest_paths_inside_workspace(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = create_workspace(client, key="workspace-video-compose-manifest-paths")
    workspace_id = workspace["workspace_id"]

    from tools.tool_registry import registry

    registry.ensure_discovered()
    video_compose = registry.get("video_compose")
    assert video_compose is not None
    seen_inputs: dict[str, object] = {}

    def fake_execute(inputs: dict) -> ToolResult:
        seen_inputs.update(inputs)
        output = Path(str(inputs["output_path"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"workspace-local-video")
        return ToolResult(success=True, data={"output_path": str(output)}, artifacts=[str(output)])

    monkeypatch.setattr(video_compose, "execute", fake_execute)
    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="video-compose-manifest-paths"),
        json={
            "stage": "compose",
            "tool_name": "video_compose",
            "inputs": {
                "operation": "render",
                "edit_decisions": {
                    "render_runtime": "remotion",
                    "renderer_family": "explainer-data",
                    "cuts": [
                        {
                            "id": "cut-1",
                            "source": "image-1",
                            "in_seconds": 0,
                            "out_seconds": 1,
                        }
                    ],
                },
                "asset_manifest": {
                    "assets": [
                        {
                            "id": "image-1",
                            "type": "image",
                            "path": "assets/images/scene.png",
                        },
                        {
                            "id": "subtitle-1",
                            "type": "subtitle",
                            "path": "assets/subtitles/script.srt",
                        },
                    ]
                },
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
    assets = seen_inputs["asset_manifest"]["assets"]  # type: ignore[index]
    for asset in assets:
        path = Path(asset["path"])
        assert path.is_absolute()
        assert workspace_id in path.parts


def test_capability_gateway_materializes_script_section_subtitles_for_video_compose(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = create_workspace(client, key="workspace-video-compose-subtitles")
    workspace_id = workspace["workspace_id"]

    from tools.tool_registry import registry

    registry.ensure_discovered()
    video_compose = registry.get("video_compose")
    assert video_compose is not None

    seen_inputs: dict[str, object] = {}

    def fake_execute(inputs: dict) -> ToolResult:
        seen_inputs.update(inputs)
        output = Path(str(inputs["output_path"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"workspace-local-video")
        return ToolResult(success=True, data={"output_path": str(output)}, artifacts=[str(output)])

    monkeypatch.setattr(video_compose, "execute", fake_execute)
    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="video-compose-subtitles"),
        json={
            "stage": "compose",
            "tool_name": "video_compose",
            "inputs": {
                "edit_decisions": {
                    "cuts": [
                        {
                            "id": "scene-1",
                            "type": "text_card",
                            "text": "CouncilForge",
                            "in_seconds": 0,
                            "out_seconds": 3,
                        }
                    ],
                    "subtitles": {
                        "enabled": True,
                        "source": "script-sections:s1",
                    },
                    "metadata": {
                        "language": "zh-CN",
                        "subtitle_sections": [
                            {
                                "section_id": "s1",
                                "start_seconds": 0,
                                "end_seconds": 3,
                                "text": "这是中文平台字幕。",
                            }
                        ],
                    },
                }
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
    edit_decisions = seen_inputs["edit_decisions"]  # type: ignore[index]
    subtitle_path = Path(str(edit_decisions["subtitles"]["source"]))  # type: ignore[index]
    assert workspace_id in subtitle_path.parts
    assert subtitle_path.name == "script-sections.srt"
    assert "这是中文平台字幕。" in subtitle_path.read_text(encoding="utf-8")
    assert seen_inputs["subtitle_path"] == str(subtitle_path)
    assert edit_decisions["captions"]  # type: ignore[index]
    assert edit_decisions["captionJoiner"] == ""  # type: ignore[index]


def test_capability_gateway_materializes_local_music_library_inputs_for_video_compose(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = create_workspace(client, key="workspace-video-compose-local-music")
    workspace_id = workspace["workspace_id"]

    repo_root = tmp_path / "repo"
    music_dir = repo_root / "music_library"
    music_dir.mkdir(parents=True)
    music_file = music_dir / "ambient.mp3"
    music_file.write_bytes(b"fake-local-music")
    client.app.state.capability_gateway.repo_root = repo_root

    from tools.tool_registry import registry

    registry.ensure_discovered()
    video_compose = registry.get("video_compose")
    assert video_compose is not None

    seen_inputs: dict[str, object] = {}

    def fake_execute(inputs: dict) -> ToolResult:
        seen_inputs.update(inputs)
        output = Path(str(inputs["output_path"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"workspace-local-video")
        return ToolResult(success=True, data={"output_path": str(output)}, artifacts=[str(output)])

    monkeypatch.setattr(video_compose, "execute", fake_execute)
    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/executions",
        headers=headers(key="video-compose-local-music"),
        json={
            "stage": "compose",
            "tool_name": "video_compose",
            "inputs": {
                "operation": "render",
                "edit_decisions": {
                    "cuts": [
                        {
                            "id": "scene-1",
                            "source": "scene-1",
                            "in_seconds": 0,
                            "out_seconds": 1,
                        }
                    ],
                    "audio": {"music": {"asset_id": "music-bg"}},
                },
                "asset_manifest": {
                    "assets": [
                        {
                            "id": "music-bg",
                            "type": "audio",
                            "subtype": "music",
                            "path": str(music_file),
                        }
                    ],
                    "metadata": {"library_dir": str(music_dir)},
                },
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
    asset_path = Path(str(seen_inputs["asset_manifest"]["assets"][0]["path"]))  # type: ignore[index]
    library_dir = Path(str(seen_inputs["asset_manifest"]["metadata"]["library_dir"]))  # type: ignore[index]
    assert workspace_id in asset_path.parts
    assert workspace_id in library_dir.parts
    assert asset_path.read_bytes() == b"fake-local-music"


def test_capability_workspace_cancel_is_tenant_scoped_and_idempotent(
    client: TestClient,
) -> None:
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


def test_capability_gateway_recovery_records_retryable_failure_event(
    client: TestClient,
) -> None:
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


def test_agent_layer_three_skill_checkpoint_and_workspace_event_sequence(
    client: TestClient,
) -> None:
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
