"""Deterministic scene- and variant-level delivery quality checks."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Literal, TypedDict


class QualityCheck(TypedDict):
    name: str
    code: str
    status: Literal["passed", "failed"]
    severity: Literal["warning", "error"]
    scene_id: str | None
    variant: str | None
    actual: Any
    measured: Any
    expected: Any
    detail: str


def _probe(path: Path) -> dict[str, Any]:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(process.stdout)
    video = next(
        (item for item in data.get("streams", []) if item.get("codec_type") == "video"),
        {},
    )
    audio = next(
        (item for item in data.get("streams", []) if item.get("codec_type") == "audio"),
        {},
    )
    rate = str(video.get("r_frame_rate") or "0/1").split("/")
    fps = (
        round(float(rate[0]) / float(rate[1]), 3)
        if len(rate) == 2 and float(rate[1])
        else 0
    )
    return {
        "duration_seconds": round(
            float(data.get("format", {}).get("duration") or 0),
            3,
        ),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": fps,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
    }


def _integrated_lufs(path: Path) -> float | None:
    process = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-vn",
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    for candidate in reversed(re.findall(r"\{[\s\S]*?\}", process.stderr)):
        try:
            return round(float(json.loads(candidate)["input_i"]), 2)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def evaluate_media(
    video_path: Path,
    manifest: dict[str, Any],
    variant: str,
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable report suitable for bounded platform revision decisions."""

    props = props or {}
    metadata = _probe(video_path)
    render = manifest.get("render") if isinstance(manifest.get("render"), dict) else {}
    audio = manifest.get("audio") if isinstance(manifest.get("audio"), dict) else {}
    checks: list[QualityCheck] = []

    def check(
        code: str,
        passed: bool,
        *,
        measured: Any,
        expected: Any,
        detail: str,
        scene_id: str | None = None,
        check_variant: str | None = None,
        severity: Literal["warning", "error"] = "error",
    ) -> None:
        checks.append(
            {
                "name": code,
                "code": code,
                "status": "passed" if passed else "failed",
                "severity": severity,
                "scene_id": scene_id,
                "variant": check_variant,
                "actual": measured,
                "measured": measured,
                "expected": expected,
                "detail": detail,
            }
        )

    characters_per_line = {"16:9": 24, "9:16": 14, "1:1": 18}.get(variant, 18)
    for scene in manifest.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        subtitle = str(scene.get("subtitle") or scene.get("narration") or "").strip()
        maximum = characters_per_line * 2
        check(
            "subtitle_overflow",
            len(subtitle) <= maximum,
            measured=len(subtitle),
            expected={"maximum_characters": maximum, "maximum_lines": 2},
            detail="Subtitle must fit within two lines in the selected delivery variant.",
            scene_id=str(scene.get("scene_id") or "") or None,
            check_variant=variant,
        )

    expected_duration = float(render.get("duration_seconds") or 0)
    actual_duration = float(metadata.get("duration_seconds") or 0)
    duration_tolerance = max(0.35, expected_duration * 0.02)
    check(
        "duration",
        expected_duration > 0
        and abs(actual_duration - expected_duration) <= duration_tolerance,
        measured=actual_duration,
        expected={"seconds": expected_duration, "tolerance": duration_tolerance},
        detail="Final duration must match the approved timeline.",
        check_variant=variant,
    )
    expected_size = [int(render.get("width") or 0), int(render.get("height") or 0)]
    actual_size = [int(metadata.get("width") or 0), int(metadata.get("height") or 0)]
    check(
        "resolution",
        expected_size == actual_size and all(actual_size),
        measured=actual_size,
        expected=expected_size,
        detail="Final dimensions must match the delivery variant.",
        check_variant=variant,
    )
    check(
        "encoding",
        metadata.get("video_codec") == "h264",
        measured={
            "video": metadata.get("video_codec"),
            "audio": metadata.get("audio_codec"),
        },
        expected={"video": "h264"},
        detail="Delivery video must use H.264.",
        check_variant=variant,
    )

    narration_required = str(audio.get("voice") or "none") != "none"
    has_audio = bool(metadata.get("audio_codec"))
    check(
        "audio_stream",
        has_audio or not narration_required,
        measured=metadata.get("audio_codec"),
        expected="audio stream" if narration_required else "optional",
        detail="Approved narration requires a playable audio stream.",
        check_variant=variant,
    )
    props_audio = props.get("audio") if isinstance(props.get("audio"), dict) else {}
    music = audio.get("music") if isinstance(audio.get("music"), dict) else {}
    target_lufs = props_audio.get("targetLufs", music.get("target_lufs"))
    integrated_lufs = _integrated_lufs(video_path) if has_audio else None
    if target_lufs is not None:
        target_lufs = float(target_lufs)
        check(
            "audio_loudness",
            integrated_lufs is not None
            and abs(integrated_lufs - target_lufs) <= 2.0,
            measured=integrated_lufs,
            expected={"target_lufs": target_lufs, "tolerance": 2.0},
            detail="Final mix must match the approved loudness target.",
            check_variant=variant,
        )

    black = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video_path),
            "-vf",
            "blackdetect=d=0.4:pix_th=0.04",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    black_seconds = sum(
        float(value)
        for value in re.findall(r"black_duration:([0-9.]+)", black.stderr)
    )
    black_ratio = (
        round(black_seconds / actual_duration, 4) if actual_duration else 1.0
    )
    check(
        "black_frames",
        black_ratio < 0.9,
        measured={"seconds": round(black_seconds, 3), "ratio": black_ratio},
        expected={"maximum_ratio": 0.9},
        detail="A delivery cannot be predominantly black frames.",
        check_variant=variant,
    )

    silence_seconds = 0.0
    if has_audio:
        silence = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(video_path),
                "-af",
                "silencedetect=noise=-48dB:d=0.5",
                "-vn",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        silence_seconds = sum(
            float(value)
            for value in re.findall(r"silence_duration: ([0-9.]+)", silence.stderr)
        )
    silence_ratio = (
        round(silence_seconds / actual_duration, 4)
        if actual_duration and has_audio
        else 0.0
    )
    check(
        "silence",
        not narration_required or (has_audio and silence_ratio < 0.9),
        measured={"seconds": round(silence_seconds, 3), "ratio": silence_ratio},
        expected={"maximum_ratio": 0.9 if narration_required else 1.0},
        detail="A narrated delivery cannot be predominantly silent.",
        check_variant=variant,
    )

    caption_style = (
        props.get("captionStyle")
        if isinstance(props.get("captionStyle"), dict)
        else {}
    )
    caption_safe = (
        1 <= int(caption_style.get("wordsPerPage") or 1) <= 2
        and 24 <= int(caption_style.get("fontSize") or 42) <= 72
        and 40 <= int(caption_style.get("maxWidthPercent") or 72) <= 85
        and 4 <= int(caption_style.get("bottomMarginPercent") or 6) <= 20
    )
    check(
        "caption_safe_area",
        caption_safe,
        measured=caption_style or "default-safe-style",
        expected={"maxWidthPercent": "40-85", "bottomMarginPercent": "4-20"},
        detail="Caption layout must remain inside the approved safe area.",
        check_variant=variant,
    )
    check(
        "artifact_integrity",
        video_path.is_file() and video_path.stat().st_size > 0,
        measured=video_path.stat().st_size if video_path.exists() else 0,
        expected="> 0 bytes",
        detail="Final MP4 must exist and contain bytes.",
        check_variant=variant,
    )

    failed = [
        item
        for item in checks
        if item["status"] == "failed" and item["severity"] == "error"
    ]
    return {
        "schema_version": "2.0",
        "status": "failed" if failed else "passed",
        "checks": checks,
        "failed_checks": failed,
        "media": metadata,
        "audio": {
            "integrated_lufs": integrated_lufs,
            "target_lufs": target_lufs,
        },
        "variant": variant,
    }
