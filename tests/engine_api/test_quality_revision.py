"""Scene- and variant-level deterministic quality reporting."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from engine_api.quality import evaluate_media
from engine_api.renderer import _should_raise_quality_gate


def _video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )


def _manifest(*, overflow: bool) -> dict:
    subtitle = (
        "这一段字幕故意非常非常长，需要在竖屏安全区域内展示但它已经超过两行允许的最大字符数量"
        * 3
        if overflow
        else "短字幕"
    )
    return {
        "render": {
            "duration_seconds": 1,
            "width": 320,
            "height": 240,
        },
        "audio": {"voice": "none"},
        "scenes": [
            {
                "scene_id": "scene-1",
                "duration_seconds": 0.4,
                "subtitle": "开场",
            },
            {
                "scene_id": "scene-2",
                "duration_seconds": 0.6,
                "subtitle": subtitle,
            },
        ],
    }


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_quality_report_identifies_scene_and_variant(tmp_path: Path) -> None:
    video = tmp_path / "delivery.mp4"
    _video(video)

    report = evaluate_media(video, _manifest(overflow=True), "9:16")

    assert report["status"] == "failed"
    subtitle_failure = next(
        item
        for item in report["failed_checks"]
        if item["code"] == "subtitle_overflow"
    )
    assert subtitle_failure["scene_id"] == "scene-2"
    assert subtitle_failure["variant"] == "9:16"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_quality_report_passes_safe_subtitles(tmp_path: Path) -> None:
    video = tmp_path / "delivery.mp4"
    _video(video)

    report = evaluate_media(video, _manifest(overflow=False), "16:9")

    assert not any(
        item["code"] == "subtitle_overflow"
        for item in report["failed_checks"]
    )


def test_platform_managed_quality_failure_is_returned_to_councilforge() -> None:
    report = {"status": "failed"}

    assert not _should_raise_quality_gate(
        {"execution_mode": "platform_managed"},
        report,
    )
    assert _should_raise_quality_gate(
        {"execution_mode": "engine_managed"},
        report,
    )
