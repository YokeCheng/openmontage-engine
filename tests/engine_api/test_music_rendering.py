"""Background-music contracts for the deterministic engine boundary."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine_api.models import MusicRequest
from engine_api.renderer import (
    _materialize_media,
    _normalize_rendered_audio,
    _quality_report,
)
from engine_api.store import EngineStore
from tools.base_tool import ToolResult


class _Available:
    value = "available"


class _FailingMusicTool:
    name = "music_generator"
    provider = "dashscope"

    def get_status(self) -> _Available:
        return _Available()

    def execute(self, _inputs: dict) -> ToolResult:
        return ToolResult(
            success=False,
            error="provider unavailable",
            retryable=True,
        )


def _manifest() -> dict:
    return {
        "title": "Music contract",
        "objective": "Explain a product",
        "creative": {"direction": "warm and precise"},
        "media_policy": {
            "visual_source": "motion_graphics",
            "voice_provider": "none",
            "music_provider": "uploaded",
            "fallback": "ask",
        },
        "budget": {"maximum_usd": 1.0},
        "audio": {
            "voice": "none",
            "music": {
                "source": "uploaded",
                "asset_id": "asset-music",
                "duration_seconds": 6,
                "target_lufs": -16,
                "ducking_db": -8,
                "fade_in_seconds": 0.4,
                "fade_out_seconds": 1.2,
                "maximum_cost_usd": 0,
            },
        },
        "render": {"aspect_ratio": "16:9", "duration_seconds": 6},
        "scenes": [
            {
                "scene_id": "scene-01",
                "title": "One",
                "duration_seconds": 6,
                "narration": "",
                "visual": {"type": "motion_graphics"},
            }
        ],
    }


def test_music_request_rejects_missing_uploaded_asset() -> None:
    with pytest.raises(ValidationError, match="asset_id"):
        MusicRequest(source="uploaded", duration_seconds=6, maximum_cost_usd=0)


def test_uploaded_music_is_bound_with_license_and_mix_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.tool_registry import registry

    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    job_dir = tmp_path / "job"
    music = job_dir / "inputs" / "asset-music" / "theme.wav"
    music.parent.mkdir(parents=True)
    music.write_bytes(b"approved music")
    manifest = _manifest()
    props = {"cuts": [{"id": "scene-01"}]}
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-music",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
        "inputs": [
            {
                "asset_id": "asset-music",
                "storage_name": "inputs/asset-music/theme.wav",
                "media_type": "audio/wav",
                "metadata": {"license_name": "user supplied"},
            }
        ],
    }

    assets = _materialize_media(
        manifest,
        props,
        job_dir / "assets",
        store,
        job,
    )

    background_music = next(item for item in assets if item["role"] == "background_music")
    assert background_music["metadata"]["license"]["source"] == "user_upload"
    assert props["audio"]["music"] == {
        "src": "inputs/asset-music/theme.wav",
        "volume": pytest.approx(0.12),
        "duckingVolume": pytest.approx(0.047773, abs=1e-6),
        "fadeInSeconds": pytest.approx(0.4),
        "fadeOutSeconds": pytest.approx(1.2),
        "loop": True,
    }
    assert props["audio"]["targetLufs"] == pytest.approx(-16)


def test_generated_music_failure_can_continue_without_music(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.tool_registry import registry

    failing = _FailingMusicTool()
    monkeypatch.setattr(registry, "ensure_discovered", lambda: None)
    monkeypatch.setattr(
        registry,
        "get_by_capability",
        lambda capability: [failing] if capability == "music_generation" else [],
    )
    manifest = _manifest()
    manifest["audio"]["music"] = {
        "source": "generated",
        "style": "ambient",
        "duration_seconds": 6,
        "provider": "dashscope",
        "maximum_cost_usd": 0.5,
        "fallback": "continue_without_music",
    }
    manifest["media_policy"]["music_provider"] = "dashscope"
    props = {"cuts": [{"id": "scene-01"}]}
    store = EngineStore(tmp_path / "runtime")
    job = {
        "job_id": "job-music-fallback",
        "tenant_id": "tenant-a",
        "correlation_id": "corr",
    }

    assets = _materialize_media(
        manifest,
        props,
        tmp_path / "job" / "assets",
        store,
        job,
    )

    assert assets == []
    assert props["degradations"] == ["background_music"]
    assert "music" not in props.get("audio", {})


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_final_mix_is_normalized_and_reports_integrated_lufs(tmp_path: Path) -> None:
    output = tmp_path / "delivery.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ],
        check=True,
    )

    measured = _normalize_rendered_audio(output, target_lufs=-16)
    quality = _quality_report(
        output,
        {
            "audio": {
                "voice": "none",
                "music": {"source": "uploaded", "target_lufs": -16},
            },
            "render": {
                "duration_seconds": 2,
                "width": 320,
                "height": 240,
            },
        },
        {"captionStyle": {}},
    )

    assert -18 <= measured <= -14
    assert -18 <= quality["audio"]["integrated_lufs"] <= -14
    loudness = next(item for item in quality["checks"] if item["name"] == "audio_loudness")
    assert loudness["status"] == "passed"
