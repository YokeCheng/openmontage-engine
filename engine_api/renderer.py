"""Zero-key Remotion rendering for the CouncilForge platform fixture."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .store import EngineStore, new_id, utc_now
from tools.subtitle.subtitle_gen import SubtitleGen


logger = logging.getLogger(__name__)


class MediaActionRequired(RuntimeError):
    """Rendering paused until CouncilForge resolves a provider fallback."""


class QualityGateError(RuntimeError):
    """The deterministic render completed but did not satisfy delivery gates."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        failed = [item["name"] for item in report.get("checks", []) if item.get("status") == "failed"]
        super().__init__("QUALITY_GATE_FAILED:" + ",".join(failed))


def _remotion_command(
    repo_root: Path,
    *,
    composition_id: str,
    output_path: Path,
    props_path: Path,
    public_dir: Path,
) -> list[str]:
    """Build a non-interactive command from the reviewed local installation.

    ``npx`` is intentionally not used here. A long-running API process may
    inherit a terminal as stdin, in which case npm can wait forever for an
    install/confirmation prompt even though the repository already contains a
    reviewed Remotion installation. The engine must execute that pinned local
    CLI directly so rendering is deterministic and cannot become interactive.
    """

    cli = (repo_root / "remotion-composer" / "node_modules" / ".bin" / "remotion").resolve()
    if not cli.is_file():
        raise FileNotFoundError("REMOTION_CLI_MISSING")
    concurrency = max(
        1,
        int(os.getenv("OPENMONTAGE_REMOTION_CONCURRENCY", str(min(8, os.cpu_count() or 1)))),
    )
    return [
        str(cli),
        "render",
        "src/index.tsx",
        composition_id,
        str(output_path),
        f"--props={props_path}",
        f"--public-dir={public_dir}",
        "--codec=h264",
        f"--concurrency={concurrency}",
        f"--gl={os.getenv('OPENMONTAGE_REMOTION_GL', 'angle')}",
        f"--x264-preset={os.getenv('OPENMONTAGE_REMOTION_X264_PRESET', 'veryfast')}",
    ]


def _terminate_process_group(process: subprocess.Popen[Any], *, grace_seconds: float = 5) -> None:
    """Stop Remotion and every browser/FFmpeg child started for the render."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    process.wait(timeout=grace_seconds)


def _first_output(result: Any) -> Path | None:
    for value in [*(result.artifacts or []), result.data.get("output"), result.data.get("output_path")]:
        if isinstance(value, str) and Path(value).is_file():
            return Path(value).resolve()
    return None


def _media_asset(path: Path, *, kind: str, result: Any, tool_name: str) -> dict[str, Any]:
    return {
        "path": path,
        "kind": kind,
        "tool": str(result.data.get("selected_tool") or tool_name),
        "provider": str(result.data.get("selected_provider") or result.data.get("provider") or "local"),
        "cost_usd": float(result.cost_usd or 0),
    }


def _public_asset_src(path: Path, public_dir: Path) -> str:
    """Return a Remotion ``staticFile`` source within the job public directory.

    Remotion deliberately refuses ``file://`` media during rendering. Every
    provider artifact used by a job must therefore live below that job's
    isolated runtime directory and be addressed through ``--public-dir``.
    """

    resolved = path.resolve()
    try:
        return resolved.relative_to(public_dir.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("MEDIA_OUTSIDE_JOB_WORKSPACE") from exc


def _approved_job_inputs(job: dict[str, Any], public_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in job.get("inputs", []):
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "")
        storage_name = str(item.get("storage_name") or "")
        if not asset_id or not storage_name:
            continue
        path = (public_dir / storage_name).resolve()
        try:
            path.relative_to(public_dir.resolve())
        except ValueError as exc:
            raise ValueError("SOURCE_INPUT_OUTSIDE_JOB_WORKSPACE") from exc
        if not path.is_file():
            raise FileNotFoundError(f"SOURCE_INPUT_MISSING:{asset_id}")
        result[asset_id] = {**item, "path": path}
    return result


def _apply_bound_source_inputs(
    manifest: dict[str, Any],
    props: dict[str, Any],
    public_dir: Path,
    store: EngineStore,
    job: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    approved_inputs = _approved_job_inputs(job, public_dir)
    cuts = props.get("cuts") if isinstance(props.get("cuts"), list) else []
    scenes = manifest.get("scenes") if isinstance(manifest.get("scenes"), list) else []
    for index, (scene, cut) in enumerate(zip(scenes, cuts)):
        visual = scene.get("visual") if isinstance(scene.get("visual"), dict) else {}
        asset_id = str(visual.get("asset_id") or visual.get("source_asset_id") or "")
        if not asset_id:
            continue
        source_input = approved_inputs.get(asset_id)
        if source_input is None:
            raise ValueError(f"SOURCE_INPUT_NOT_UPLOADED:{asset_id}")
        media_type = str(source_input.get("media_type") or "application/octet-stream")
        if media_type.startswith("image/"):
            cut["backgroundImage"] = _public_asset_src(source_input["path"], public_dir)
        elif media_type.startswith("video/"):
            cut["backgroundVideo"] = _public_asset_src(source_input["path"], public_dir)
            cut["backgroundVideoStart"] = float(visual.get("start_seconds") or 0)
        else:
            raise ValueError(f"SOURCE_INPUT_VISUAL_TYPE_UNSUPPORTED:{asset_id}")
        cut["backgroundOverlay"] = float(visual.get("overlay") or 0.28)
        store.append_event(
            job,
            "media.source_input_bound",
            {
                "scene_id": str(scene.get("scene_id") or f"scene-{index + 1:02d}"),
                "asset_id": asset_id,
                "media_type": media_type,
            },
        )
    return approved_inputs


def _materialize_media(
    manifest: dict[str, Any],
    props: dict[str, Any],
    asset_dir: Path,
    store: EngineStore,
    job: dict[str, Any],
) -> list[dict[str, Any]]:
    """Execute approved media choices through the existing tool registry."""

    policy = manifest.get("media_policy") if isinstance(manifest.get("media_policy"), dict) else {}
    source_mode = str(policy.get("visual_source") or "motion_graphics")
    voice_provider = str(policy.get("voice_provider") or "none")
    music_provider = str(policy.get("music_provider") or "none")
    asset_dir.mkdir(parents=True, exist_ok=True)
    media_assets: list[dict[str, Any]] = []
    public_dir = asset_dir.parent
    cuts = props.get("cuts") if isinstance(props.get("cuts"), list) else []
    scenes = manifest.get("scenes") if isinstance(manifest.get("scenes"), list) else []
    aspect_ratio = str((manifest.get("render") or {}).get("aspect_ratio") or "16:9")
    approved_inputs = _apply_bound_source_inputs(manifest, props, public_dir, store, job)
    reuse_by_scene: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for declaration in manifest.get("reuse_assets") or []:
        if not isinstance(declaration, dict) or declaration.get("kind") != "narration":
            continue
        scene_id = str(declaration.get("scene_id") or "")
        source_scene_id = str(declaration.get("source_scene_id") or scene_id)
        if scene_id != source_scene_id:
            raise ValueError("REUSE_ASSET_SCENE_MISMATCH")
        if scene_id in reuse_by_scene:
            raise ValueError("REUSE_ASSET_SCENE_DUPLICATE")
        source_input = approved_inputs.get(str(declaration.get("asset_id") or ""))
        if scene_id and source_input is not None:
            reuse_by_scene[scene_id] = (declaration, source_input)
    if source_mode == "motion_graphics" and voice_provider == "none" and music_provider == "none":
        return []

    from tools.tool_registry import registry

    registry.ensure_discovered()

    def fallback(scene_id: str, capability: str) -> None:
        if str(policy.get("fallback") or "ask") == "ask":
            action_id = new_id("action")
            recommended_resolution = "retry" if capability == "tts" else "use_motion_graphics"
            action = {
                "action_id": action_id,
                "job_id": job["job_id"],
                "type": "provider_fallback",
                "status": "pending",
                "title": "Media provider unavailable",
                "summary": f"The approved {capability} path is unavailable for {scene_id}.",
                "recommended_resolution": recommended_resolution,
                "options": [
                    {
                        "value": "use_motion_graphics",
                        "label": "Use Remotion motion graphics",
                        "description": "Continue without an external media generation charge.",
                    },
                    {
                        "value": "retry",
                        "label": "Retry provider",
                        "description": "Retry after checking provider availability or quota.",
                    },
                    {"value": "cancel", "label": "Cancel job"},
                ],
                "context": {"scene_id": scene_id, "capability": capability},
                "created_at": utc_now(),
            }
            job.setdefault("actions", []).append(action)
            job["status"] = "waiting_action"
            job["stage"] = "media"
            job["progress"] = {
                "percent": 54,
                "message": "Waiting for media fallback decision",
                "updated_at": utc_now(),
            }
            store.save_job(job)
            store.append_event(job, "action.required", {"action_id": action_id, "type": "provider_fallback"})
            raise MediaActionRequired(capability)
        store.append_event(
            job,
            "media.fallback_applied",
            {"scene_id": scene_id, "capability": capability, "fallback": "motion_graphics"},
        )

    if source_mode in {"ai_image", "ai_video"}:
        selector_name = "image_selector" if source_mode == "ai_image" else "video_selector"
        selector = registry.get(selector_name)
        preferred = str(
            policy.get("image_provider" if source_mode == "ai_image" else "video_provider") or "auto"
        )
        for index, (scene, cut) in enumerate(zip(scenes, cuts)):
            scene_id = str(scene.get("scene_id") or f"scene-{index + 1:02d}")
            if selector is None or selector.get_status().value != "available":
                fallback(scene_id, source_mode)
                continue
            suffix = ".png" if source_mode == "ai_image" else ".mp4"
            output_path = asset_dir / f"{scene_id}{suffix}"
            if output_path.is_file() and output_path.stat().st_size > 0:
                field = "backgroundImage" if source_mode == "ai_image" else "backgroundVideo"
                cut[field] = _public_asset_src(output_path, public_dir)
                cut["backgroundOverlay"] = 0.45
                media_assets.append(
                    {
                        "path": output_path.resolve(),
                        "kind": "image" if source_mode == "ai_image" else "video",
                        "tool": "cached",
                        "provider": preferred,
                        "cost_usd": 0.0,
                    }
                )
                continue
            inputs: dict[str, Any] = {
                "prompt": str((scene.get("visual") or {}).get("prompt") or scene.get("title") or ""),
                "preferred_provider": preferred,
                "aspect_ratio": aspect_ratio,
                "output_path": str(output_path),
                "scene_id": scene_id,
            }
            if source_mode == "ai_video":
                inputs.update(
                    {
                        "operation": "text_to_video",
                        "duration": str(max(1, min(10, round(float(scene.get("duration_seconds") or 5))))),
                    }
                )
            result = selector.execute(inputs)
            path = _first_output(result) if result.success else None
            if path is None:
                fallback(scene_id, source_mode)
                continue
            field = "backgroundImage" if source_mode == "ai_image" else "backgroundVideo"
            cut[field] = _public_asset_src(path, public_dir)
            cut["backgroundOverlay"] = 0.45
            asset = _media_asset(
                path,
                kind="image" if source_mode == "ai_image" else "video",
                result=result,
                tool_name=selector_name,
            )
            media_assets.append(asset)
            store.append_event(
                job,
                "media.asset_ready",
                {"scene_id": scene_id, "kind": asset["kind"], "provider": asset["provider"], "tool": asset["tool"]},
            )
    elif source_mode == "stock" and scenes and cuts:
        tool = registry.get("direct_clip_search")
        if tool is not None and tool.get_status().value == "available":
            result = tool.execute(
                {
                    "output_dir": str(asset_dir / "stock"),
                    "queries": [
                        {
                            "query": str((scene.get("visual") or {}).get("prompt") or scene.get("title") or ""),
                            "slot_id": str(scene.get("scene_id") or f"scene-{index + 1:02d}"),
                            "kind": "video",
                        }
                        for index, scene in enumerate(scenes)
                    ],
                    "clips_per_query": 1,
                    "filters": {"orientation": "portrait" if aspect_ratio == "9:16" else "landscape"},
                    "extract_thumbnails": False,
                }
            )
            by_scene = {
                str(item.get("slot_id")): item
                for item in result.data.get("clips", [])
                if isinstance(item, dict) and item.get("path")
            } if result.success else {}
            for index, (scene, cut) in enumerate(zip(scenes, cuts)):
                scene_id = str(scene.get("scene_id") or f"scene-{index + 1:02d}")
                item = by_scene.get(scene_id)
                path = Path(str(item["path"])).resolve() if item else None
                if path is None or not path.is_file():
                    fallback(scene_id, "stock")
                    continue
                field = "backgroundImage" if item.get("kind") == "image" else "backgroundVideo"
                cut[field] = _public_asset_src(path, public_dir)
                cut["backgroundOverlay"] = 0.45
                media_assets.append(
                    {
                        "path": path,
                        "kind": str(item.get("kind") or "video"),
                        "tool": "direct_clip_search",
                        "provider": str(item.get("source") or "stock"),
                        "cost_usd": 0.0,
                    }
                )
        else:
            for scene in scenes:
                fallback(str(scene.get("scene_id") or "scene"), "stock")

    if voice_provider != "none":
        selector = registry.get("tts_selector")
        if selector is None or selector.get_status().value != "available":
            fallback("narration", "tts")
        else:
            # Each scene has an independent approved narration request and a
            # distinct output path. Execute the network-bound provider calls in
            # a small bounded pool, then preserve timeline order while fitting
            # and registering the returned audio. This reduces production time
            # without adding Agent/model calls or changing scene decisions.
            tts_results: dict[int, Any | None] = {}
            pending_tts: dict[int, Any] = {}
            tts_inputs: list[tuple[int, str, Path, str]] = []
            for index, scene in enumerate(scenes):
                narration = str(scene.get("narration") or "").strip()
                scene_id = str(scene.get("scene_id") or f"scene-{index + 1:02d}")
                narration_path = asset_dir / f"narration-{index + 1:02d}.wav"
                if (
                    narration
                    and scene_id not in reuse_by_scene
                    and not (narration_path.is_file() and narration_path.stat().st_size > 0)
                ):
                    tts_inputs.append(
                        (
                            index,
                            narration,
                            narration_path,
                            str(scene.get("voice") or voice_provider),
                        )
                    )
            def execute_tts(inputs: dict[str, Any]) -> Any:
                result: Any = None
                for attempt in range(1, 4):
                    result = selector.execute(inputs)
                    if result.success or not bool(getattr(result, "retryable", False)):
                        return result
                    time.sleep(0.25 * (2 ** (attempt - 1)))
                return result

            if tts_inputs:
                # Three concurrent calls remain below the common provider burst
                # limit while still collapsing six serial network round trips
                # into two waves. Retry only the failed request in-place.
                with ThreadPoolExecutor(max_workers=min(3, len(tts_inputs)), thread_name_prefix="tts") as pool:
                    for index, narration, narration_path, scene_voice_provider in tts_inputs:
                        pending_tts[index] = pool.submit(
                            execute_tts,
                            {
                                "text": narration,
                                "preferred_provider": scene_voice_provider,
                                "speed": float((manifest.get("audio") or {}).get("voice_speed") or 1),
                                "output_path": str(narration_path),
                            },
                        )
                    for index, future in pending_tts.items():
                        try:
                            tts_results[index] = future.result()
                        except Exception:
                            logger.exception("Parallel TTS request failed for scene %s", index + 1)
                            tts_results[index] = None

            narration_segments: list[dict[str, Any]] = []
            cursor_seconds = 0.0
            for index, scene in enumerate(scenes):
                scene_id = str(scene.get("scene_id") or f"scene-{index + 1:02d}")
                narration = str(scene.get("narration") or "").strip()
                scene_duration = max(0.25, float(scene.get("duration_seconds") or 0.25))
                if not narration:
                    cursor_seconds += scene_duration
                    continue

                # Synthesise one approved sentence per scene. A single
                # concatenated TTS file starts every later sentence too early
                # and leaves the second half of sparse 30-second videos silent.
                # Scene segments preserve the approved storyboard timing and
                # make burned-in captions line up with the spoken content.
                narration_path = asset_dir / f"narration-{index + 1:02d}.wav"
                narration_receipt = asset_dir / f"narration-{index + 1:02d}.receipt.json"
                reused = reuse_by_scene.get(scene_id)
                if reused is not None:
                    declaration, source_input = reused
                    narration_asset = {
                        "path": source_input["path"],
                        "kind": "audio",
                        "tool": "artifact_reuse",
                        "provider": "councilforge-minio",
                        "cost_usd": 0.0,
                        "metadata": {
                            "reused": True,
                            "reused_from_artifact_id": declaration.get("platform_artifact_id"),
                            "provider_call": False,
                        },
                    }
                    store.append_event(
                        job,
                        "media.asset_reused",
                        {
                            "scene_id": scene_id,
                            "kind": "narration",
                            "source_artifact_id": declaration.get("platform_artifact_id"),
                        },
                    )
                elif index not in tts_results and narration_path.is_file() and narration_path.stat().st_size > 0:
                    receipt: dict[str, Any] = {}
                    if narration_receipt.is_file():
                        try:
                            loaded_receipt = json.loads(narration_receipt.read_text(encoding="utf-8"))
                            receipt = loaded_receipt if isinstance(loaded_receipt, dict) else {}
                        except (OSError, ValueError):
                            receipt = {}
                    narration_asset: dict[str, Any] | None = {
                        "path": narration_path.resolve(),
                        "kind": "audio",
                        "tool": str(receipt.get("tool") or "cached"),
                        "provider": str(receipt.get("provider") or voice_provider),
                        # The receipt records the original provider charge. It
                        # is registered once when the final artifact set is
                        # committed, even if a retry reused this file.
                        "cost_usd": float(receipt.get("cost_usd") or 0),
                    }
                else:
                    result = tts_results.get(index)
                    path = _first_output(result) if result is not None and result.success else None
                    narration_asset = (
                        _media_asset(path, kind="audio", result=result, tool_name="tts_selector")
                        if path
                        else None
                    )
                    if narration_asset is not None:
                        narration_receipt.write_text(
                            json.dumps(
                                {
                                    "schema_version": "1.0",
                                    "tool": narration_asset["tool"],
                                    "provider": narration_asset["provider"],
                                    "cost_usd": narration_asset["cost_usd"],
                                },
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                if narration_asset is None:
                    fallback(scene_id, "tts")
                    cursor_seconds += scene_duration
                    continue

                target_duration = max(0.25, scene_duration - 0.35)
                if reused is not None:
                    # A reusable narration artifact is the already-approved,
                    # timeline-fitted output of an immutable earlier version.
                    # Re-encoding it against the same scene boundary can
                    # introduce rounding drift and destroys byte-level reuse.
                    fitted_path = Path(narration_asset["path"]).resolve()
                    fit_metadata = {
                        "timeline_repaired": False,
                        "timeline_fit_reused": True,
                        "timeline_speed_factor": 1.0,
                    }
                else:
                    fitted_path, fit_metadata = _fit_audio_to_timeline(
                        narration_asset["path"],
                        target_duration_seconds=target_duration,
                        output_path=asset_dir / f"narration-{index + 1:02d}-timeline.wav",
                    )
                scene_end = cursor_seconds + scene_duration
                narration_asset["path"] = fitted_path
                narration_asset["metadata"] = {
                    **(narration_asset.get("metadata") or {}),
                    **fit_metadata,
                    "scene_id": scene_id,
                    "timeline_start_seconds": round(cursor_seconds, 3),
                    "timeline_end_seconds": round(scene_end, 3),
                }
                narration_segments.append(
                    {
                        "src": _public_asset_src(fitted_path, public_dir),
                        "start_seconds": round(cursor_seconds, 3),
                        "end_seconds": round(scene_end, 3),
                        "volume": 1,
                    }
                )
                media_assets.append(narration_asset)
                if fit_metadata["timeline_repaired"]:
                    store.append_event(
                        job,
                        "media.audio_timeline_repaired",
                        {
                            "scene_id": scene_id,
                            "original_duration_seconds": fit_metadata["original_duration_seconds"],
                            "fitted_duration_seconds": fit_metadata["fitted_duration_seconds"],
                            "speed_factor": fit_metadata["timeline_speed_factor"],
                        },
                    )
                cursor_seconds = scene_end
            if narration_segments:
                props.setdefault("audio", {})["narration"] = {
                    "volume": 1,
                    "segments": narration_segments,
                }

    if music_provider != "none":
        music_asset_id = str((manifest.get("audio") or {}).get("music_asset_id") or "")
        approved_music = approved_inputs.get(music_asset_id) if music_asset_id else None
        if approved_music is not None:
            media_type = str(approved_music.get("media_type") or "")
            if not media_type.startswith("audio/"):
                raise ValueError(f"SOURCE_INPUT_MUSIC_TYPE_UNSUPPORTED:{music_asset_id}")
            props.setdefault("audio", {})["music"] = {
                "src": _public_asset_src(approved_music["path"], public_dir),
                "volume": 0.12,
                "duckingVolume": 0.045,
                "fadeInSeconds": 1.5,
                "fadeOutSeconds": 2.5,
                "loop": True,
            }
            store.append_event(job, "media.music_input_bound", {"asset_id": music_asset_id})
            return media_assets
        music_path = asset_dir / "music.mp3"
        if music_path.is_file() and music_path.stat().st_size > 0:
            props.setdefault("audio", {})["music"] = {
                "src": _public_asset_src(music_path, public_dir),
                "volume": 0.12,
                "duckingVolume": 0.045,
                "fadeInSeconds": 1.5,
                "fadeOutSeconds": 2.5,
                "loop": True,
            }
            media_assets.append(
                {
                    "path": music_path.resolve(),
                    "kind": "audio",
                    "tool": "cached",
                    "provider": music_provider,
                    "cost_usd": 0.0,
                }
            )
            return media_assets
        candidates = [
            tool
            for tool in registry.get_by_capability("music_generation")
            if tool.get_status().value == "available"
            and (music_provider == "auto" or tool.provider == music_provider)
        ]
        tool = sorted(candidates, key=lambda item: (item.provider, item.name))[0] if candidates else None
        if tool is not None:
            result = tool.execute(
                {
                    "prompt": str((manifest.get("creative") or {}).get("direction") or manifest.get("objective") or ""),
                    "duration_seconds": float((manifest.get("render") or {}).get("duration_seconds") or 30),
                    "force_instrumental": True,
                    "output_path": str(music_path),
                }
            )
            path = _first_output(result) if result.success else None
            if path:
                props.setdefault("audio", {})["music"] = {
                    "src": _public_asset_src(path, public_dir),
                    "volume": 0.12,
                    "duckingVolume": 0.045,
                    "fadeInSeconds": 1.5,
                    "fadeOutSeconds": 2.5,
                    "loop": True,
                }
                media_assets.append(_media_asset(path, kind="audio", result=result, tool_name=tool.name))
            else:
                fallback("soundtrack", "music_generation")
        else:
            fallback("soundtrack", "music_generation")

    return media_assets


def _render_contract(manifest: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Choose the existing OpenMontage composition for an approved manifest.

    CouncilForge owns the creative decisions. The engine only verifies that a
    usable canonical edit artifact exists and routes it to the corresponding
    deterministic Remotion composition.
    """

    artifacts = manifest.get("pipeline_artifacts")
    edit_decisions = artifacts.get("edit_decisions") if isinstance(artifacts, dict) else None
    if isinstance(edit_decisions, dict) and isinstance(edit_decisions.get("cuts"), list) and edit_decisions["cuts"]:
        props = deepcopy(edit_decisions)
        props["render"] = deepcopy(manifest["render"])
        return "Explainer", props
    return (
        "CouncilForgePlatform",
        {
            "title": manifest["title"],
            "objective": manifest["objective"],
            "format": manifest["format"],
            "language": manifest.get("language", "zh-CN"),
            "scenes": manifest["scenes"],
            "render": manifest["render"],
        },
    )


def _ffprobe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json", str(path),
    ]
    data = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    rate = video.get("r_frame_rate", "0/1").split("/")
    fps = round(float(rate[0]) / float(rate[1]), 3) if len(rate) == 2 and float(rate[1]) else 0
    return {
        "duration_seconds": round(float(data.get("format", {}).get("duration", 0)), 3),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": fps,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
    }


def _create_cover_artifact(
    output_path: Path,
    *,
    artifact_dir: Path,
    job_id: str,
    video_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Extract a deterministic platform cover from the approved final video."""

    duration = max(0.1, float(video_metadata.get("duration_seconds") or 0.1))
    seek_seconds = min(max(0.1, duration * 0.12), max(0.1, duration - 0.1))
    cover_path = (artifact_dir / "cover.jpg").resolve()
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{seek_seconds:.3f}",
            "-i",
            str(output_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(cover_path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    artifact_id = new_id("artifact")
    return {
        "artifact_id": artifact_id,
        "job_id": job_id,
        "kind": "image",
        "role": "final",
        "media_type": "image/jpeg",
        "uri": f"engine://jobs/{job_id}/artifacts/{artifact_id}",
        "storage_name": "artifacts/cover.jpg",
        "version": 1,
        "size_bytes": cover_path.stat().st_size,
        "checksum": {
            "algorithm": "sha256",
            "value": hashlib.sha256(cover_path.read_bytes()).hexdigest(),
        },
        "created_at": utc_now(),
        "metadata": {
            "width": video_metadata.get("width"),
            "height": video_metadata.get("height"),
            "image_format": "JPEG",
            "source": "final_video",
            "time_seconds": round(seek_seconds, 3),
        },
    }


def _fit_audio_to_timeline(
    source_path: Path,
    *,
    target_duration_seconds: float,
    output_path: Path,
) -> tuple[Path, dict[str, Any]]:
    """Keep all approved narration audible within the final video timeline."""

    original_duration = float(_ffprobe(source_path)["duration_seconds"])
    if original_duration <= target_duration_seconds:
        return source_path.resolve(), {
            "timeline_repaired": False,
            "original_duration_seconds": original_duration,
            "fitted_duration_seconds": original_duration,
            "timeline_speed_factor": 1.0,
        }

    speed_factor = original_duration / target_duration_seconds
    if speed_factor > 1.5:
        raise ValueError("NARRATION_EXCEEDS_SAFE_TIMELINE_REPAIR")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(source_path),
            "-filter:a",
            f"atempo={speed_factor:.8f}",
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(temporary),
        ],
        check=True,
        capture_output=True,
    )
    os.replace(temporary, output_path)
    fitted_duration = float(_ffprobe(output_path)["duration_seconds"])
    if fitted_duration > target_duration_seconds + 0.15:
        raise ValueError("NARRATION_TIMELINE_REPAIR_FAILED")
    return output_path.resolve(), {
        "timeline_repaired": True,
        "timeline_fit_tool": "ffmpeg_atempo",
        "original_duration_seconds": original_duration,
        "fitted_duration_seconds": fitted_duration,
        "timeline_speed_factor": round(speed_factor, 4),
    }


def _media_artifact_metadata(media_path: Path, media_asset: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "provider": media_asset["provider"],
        "tool": media_asset["tool"],
        "cost_usd": media_asset["cost_usd"],
    }
    if media_asset["kind"] in {"audio", "video"}:
        try:
            metadata.update(_ffprobe(media_path))
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            # The final video remains the hard verification gate. Keep an
            # intermediate provider artifact available on an unusual codec.
            pass
    if isinstance(media_asset.get("metadata"), dict):
        metadata.update(media_asset["metadata"])
    return metadata


def _quality_report(
    output_path: Path,
    manifest: dict[str, Any],
    props: dict[str, Any],
) -> dict[str, Any]:
    """Run deterministic, vendor-independent checks on a rendered delivery."""

    metadata = _ffprobe(output_path)
    render = manifest.get("render") if isinstance(manifest.get("render"), dict) else {}
    audio = manifest.get("audio") if isinstance(manifest.get("audio"), dict) else {}
    expected_duration = float(render.get("duration_seconds") or 0)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, *, actual: Any, expected: Any, detail: str) -> None:
        checks.append(
            {
                "name": name,
                "status": "passed" if passed else "failed",
                "actual": actual,
                "expected": expected,
                "detail": detail,
            }
        )

    duration = float(metadata.get("duration_seconds") or 0)
    duration_tolerance = max(0.35, expected_duration * 0.02)
    check(
        "duration",
        expected_duration > 0 and abs(duration - expected_duration) <= duration_tolerance,
        actual=duration,
        expected={"seconds": expected_duration, "tolerance": duration_tolerance},
        detail="Final duration must match the approved timeline.",
    )
    expected_size = [int(render.get("width") or 0), int(render.get("height") or 0)]
    actual_size = [int(metadata.get("width") or 0), int(metadata.get("height") or 0)]
    check(
        "resolution",
        expected_size == actual_size and all(actual_size),
        actual=actual_size,
        expected=expected_size,
        detail="Final dimensions must match the approved render contract.",
    )
    check(
        "encoding",
        metadata.get("video_codec") == "h264",
        actual={"video": metadata.get("video_codec"), "audio": metadata.get("audio_codec")},
        expected={"video": "h264"},
        detail="The delivery video must use H.264 for broad playback support.",
    )
    narration_required = str(audio.get("voice") or "none") != "none"
    has_audio = bool(metadata.get("audio_codec"))
    check(
        "audio_stream",
        has_audio or not narration_required,
        actual=metadata.get("audio_codec"),
        expected="audio stream" if narration_required else "optional",
        detail="Approved narration requires a playable audio stream.",
    )

    black_seconds = 0.0
    black_process = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(output_path),
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
    for value in re.findall(r"black_duration:([0-9.]+)", black_process.stderr):
        black_seconds += float(value)
    black_ratio = round(black_seconds / duration, 4) if duration else 1.0
    check(
        "black_frames",
        black_ratio < 0.9,
        actual={"seconds": round(black_seconds, 3), "ratio": black_ratio},
        expected={"maximum_ratio": 0.9},
        detail="A delivery cannot be predominantly black frames.",
    )

    silence_seconds = 0.0
    if has_audio:
        silence_process = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(output_path),
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
        silence_durations = re.findall(r"silence_duration: ([0-9.]+)", silence_process.stderr)
        for value in silence_durations:
            silence_seconds += float(value)
        if not silence_durations:
            starts = re.findall(r"silence_start: ([0-9.]+)", silence_process.stderr)
            if starts:
                silence_seconds = max(0.0, duration - float(starts[0]))
    silence_ratio = round(silence_seconds / duration, 4) if duration and has_audio else 0.0
    check(
        "silence",
        not narration_required or (has_audio and silence_ratio < 0.9),
        actual={"seconds": round(silence_seconds, 3), "ratio": silence_ratio},
        expected={"maximum_ratio": 0.9 if narration_required else 1.0},
        detail="A narrated delivery cannot be predominantly silent.",
    )

    caption_style = props.get("captionStyle") if isinstance(props.get("captionStyle"), dict) else {}
    caption_safe = (
        1 <= int(caption_style.get("wordsPerPage") or 1) <= 2
        and 24 <= int(caption_style.get("fontSize") or 42) <= 72
        and 40 <= int(caption_style.get("maxWidthPercent") or 72) <= 85
        and 4 <= int(caption_style.get("bottomMarginPercent") or 6) <= 20
    )
    check(
        "caption_safe_area",
        caption_safe,
        actual=caption_style or "default-safe-style",
        expected={"maxWidthPercent": "40-85", "bottomMarginPercent": "4-20"},
        detail="Caption layout must remain inside the approved safe area.",
    )
    check(
        "artifact_integrity",
        output_path.is_file() and output_path.stat().st_size > 0,
        actual=output_path.stat().st_size if output_path.exists() else 0,
        expected="> 0 bytes",
        detail="The final MP4 must exist and contain bytes.",
    )
    failed = [item["name"] for item in checks if item["status"] == "failed"]
    return {
        "schema_version": "1.0",
        "status": "failed" if failed else "passed",
        "checks": checks,
        "failed_checks": failed,
        "media": metadata,
        "created_at": utc_now(),
    }


def _render_failure_detail(exc: Exception) -> tuple[str, str]:
    raw = str(exc)
    known = {
        "NARRATION_EXCEEDS_SAFE_TIMELINE_REPAIR": "A narration segment is too long for its approved scene. Shorten the narration or increase the scene duration.",
        "NARRATION_TIMELINE_REPAIR_FAILED": "Narration could not be fitted safely to the approved timeline.",
        "MEDIA_OUTSIDE_JOB_WORKSPACE": "A media file was outside the isolated job workspace.",
        "REMOTION_CLI_MISSING": "The reviewed local Remotion installation is missing. Install the repository dependencies before retrying.",
        "REMOTION_RENDER_TIMEOUT": "Remotion exceeded the configured render deadline and was stopped safely.",
    }
    for code, detail in known.items():
        if code in raw:
            return code, detail
    if raw.startswith("SOURCE_INPUT_"):
        code = raw.split(":", 1)[0]
        return code, "An approved source asset is missing or has an unsupported media type. Re-upload or rebind the material."
    if isinstance(exc, subprocess.CalledProcessError):
        return "COMPOSITOR_PROCESS_FAILED", "Remotion or FFmpeg could not complete the approved composition."
    if isinstance(exc, FileNotFoundError):
        return "RENDER_DEPENDENCY_MISSING", "A required approved media file or render dependency is missing."
    return "RENDER_FAILED", "The video renderer failed while composing the approved production order."


def render_job(store: EngineStore, job_id: str, repo_root: Path) -> None:
    job = store.load_job(job_id)
    if not job or job["status"] in {"cancelled", "succeeded"}:
        return
    try:
        job["status"] = "rendering"
        job["stage"] = "rendering"
        job["progress"] = {"percent": 72, "message": "Rendering MP4", "updated_at": utc_now()}
        saved, active = store.save_job_if_active(job)
        if not active or saved is None:
            return
        job = saved
        store.append_event(job, "job.status_changed", {"status": "rendering"})

        job_dir = store.jobs_dir / job_id
        artifact_dir = job_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        props_path = (job_dir / "render-props.json").resolve()
        output_path = (artifact_dir / "final.mp4").resolve()
        manifest = job["input"]
        composition_id, props = _render_contract(manifest)

        mode = os.getenv("OPENMONTAGE_ENGINE_RENDER_MODE", "remotion")
        media_assets: list[dict[str, Any]] = []
        if mode != "fixture-copy" and composition_id == "Explainer":
            job["progress"] = {"percent": 52, "message": "Preparing approved media", "updated_at": utc_now()}
            saved, active = store.save_job_if_active(job)
            if not active or saved is None:
                return
            job = saved
            media_assets = _materialize_media(manifest, props, job_dir / "assets", store, job)
        props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
        if mode == "fixture-copy":
            fixture = Path(os.environ["OPENMONTAGE_ENGINE_FIXTURE_VIDEO"])
            shutil.copyfile(fixture, output_path)
        else:
            command = _remotion_command(
                repo_root,
                composition_id=composition_id,
                output_path=output_path,
                props_path=props_path,
                public_dir=job_dir,
            )
            job["progress"] = {"percent": 72, "message": "Rendering MP4", "updated_at": utc_now()}
            saved, active = store.save_job_if_active(job)
            if not active or saved is None:
                return
            job = saved
            store.append_event(job, "render.started", {"runtime": "remotion"})
            render_timeout_seconds = max(
                30,
                int(os.getenv("OPENMONTAGE_ENGINE_RENDER_TIMEOUT_SECONDS", "300")),
            )
            deadline = time.monotonic() + render_timeout_seconds
            logger.info("Starting local Remotion render for job %s", job_id)
            process = subprocess.Popen(
                command,
                cwd=repo_root / "remotion-composer",
                stdin=subprocess.DEVNULL,
                env={**os.environ, "CI": "1", "NO_COLOR": "1", "npm_config_yes": "true"},
                start_new_session=True,
            )
            while process.poll() is None:
                latest = store.load_job(job_id)
                if not latest or latest.get("status") == "cancelled":
                    _terminate_process_group(process)
                    return
                if time.monotonic() >= deadline:
                    _terminate_process_group(process)
                    raise TimeoutError("REMOTION_RENDER_TIMEOUT")
                time.sleep(0.25)
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command)

        current = store.load_job(job_id)
        if not current or current["status"] == "cancelled":
            return
        digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
        metadata = _ffprobe(output_path)
        artifact_id = new_id("artifact")
        video_artifact = {
            "artifact_id": artifact_id,
            "job_id": job_id,
            "kind": "video",
            "role": "final",
            "media_type": "video/mp4",
            "uri": f"engine://jobs/{job_id}/artifacts/{artifact_id}",
            "storage_name": "artifacts/final.mp4",
            "version": 1,
            "size_bytes": output_path.stat().st_size,
            "checksum": {"algorithm": "sha256", "value": digest},
            "created_at": utc_now(),
            "metadata": metadata,
        }
        cover_artifact = _create_cover_artifact(
            output_path,
            artifact_dir=artifact_dir,
            job_id=job_id,
            video_metadata=metadata,
        )
        artifacts: list[dict[str, Any]] = []
        for media_asset in media_assets:
            media_path = media_asset["path"]
            media_id = new_id("artifact")
            try:
                storage_name = str(media_path.relative_to(job_dir))
            except ValueError:
                continue
            media_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
            media_metadata = _media_artifact_metadata(media_path, media_asset)
            artifacts.append(
                {
                    "artifact_id": media_id,
                    "job_id": job_id,
                    "kind": media_asset["kind"],
                    "role": "intermediate",
                    "media_type": media_type,
                    "uri": f"engine://jobs/{job_id}/artifacts/{media_id}",
                    "storage_name": storage_name,
                    "version": 1,
                    "size_bytes": media_path.stat().st_size,
                    "checksum": {
                        "algorithm": "sha256",
                        "value": hashlib.sha256(media_path.read_bytes()).hexdigest(),
                    },
                    "created_at": utc_now(),
                    "metadata": media_metadata,
                }
            )
        artifacts.extend([video_artifact, cover_artifact])
        if bool(manifest.get("audio", {}).get("subtitles", True)):
            subtitle_path = (artifact_dir / "subtitles.srt").resolve()
            cursor = 0.0
            segments = []
            for scene in manifest["scenes"]:
                end = cursor + float(scene["duration_seconds"])
                segments.append(
                    {
                        "text": scene.get("subtitle") or scene.get("narration", ""),
                        "start": cursor,
                        "end": end,
                    }
                )
                cursor = end
            subtitle_result = SubtitleGen().execute(
                {
                    "segments": segments,
                    "format": "srt",
                    "output_path": str(subtitle_path),
                    # Each scene is already an editorially approved caption unit.
                    # Keeping one scene per cue preserves its exact timeline,
                    # especially for Chinese narration without word timestamps.
                    "max_words_per_cue": 1,
                }
            )
            if subtitle_result.success:
                subtitle_digest = hashlib.sha256(subtitle_path.read_bytes()).hexdigest()
                subtitle_id = new_id("artifact")
                artifacts.append(
                    {
                        "artifact_id": subtitle_id,
                        "job_id": job_id,
                        "kind": "subtitle",
                        "role": "final",
                        "media_type": "application/x-subrip; charset=utf-8",
                        "uri": f"engine://jobs/{job_id}/artifacts/{subtitle_id}",
                        "storage_name": "artifacts/subtitles.srt",
                        "version": 1,
                        "size_bytes": subtitle_path.stat().st_size,
                        "checksum": {"algorithm": "sha256", "value": subtitle_digest},
                        "created_at": utc_now(),
                        "metadata": {
                            "language": manifest.get("language", "zh-CN"),
                            "cue_count": subtitle_result.data.get("cue_count", len(segments)),
                        },
                    }
                )
        current["stage"] = "quality"
        current["progress"] = {"percent": 94, "message": "Running automatic quality checks", "updated_at": utc_now()}
        saved, active = store.save_job_if_active(current)
        if not active or saved is None:
            return
        current = saved
        quality = _quality_report(output_path, manifest, props)
        report_path = (artifact_dir / "quality-report.json").resolve()
        report_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        report_id = new_id("artifact")
        artifacts.append(
            {
                "artifact_id": report_id,
                "job_id": job_id,
                "kind": "report",
                "role": "final",
                "media_type": "application/json; charset=utf-8",
                "uri": f"engine://jobs/{job_id}/artifacts/{report_id}",
                "storage_name": "artifacts/quality-report.json",
                "version": 1,
                "size_bytes": report_path.stat().st_size,
                "checksum": {
                    "algorithm": "sha256",
                    "value": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                },
                "created_at": utc_now(),
                "metadata": {
                    "quality_status": quality["status"],
                    "failed_checks": quality["failed_checks"],
                },
            }
        )
        current["artifacts"] = artifacts
        current["quality_report"] = quality
        saved, active = store.save_job_if_active(current)
        if not active or saved is None:
            return
        current = saved
        if quality["status"] != "passed":
            raise QualityGateError(quality)
        current["status"] = "succeeded"
        current["stage"] = "delivery"
        current["progress"] = {"percent": 100, "message": "Video ready", "updated_at": utc_now()}
        saved, active = store.save_job_if_active(current)
        if not active or saved is None:
            return
        current = saved
        for artifact in artifacts:
            store.append_event(
                current,
                "artifact.created",
                {"artifact_id": artifact["artifact_id"], "kind": artifact["kind"]},
            )
        store.append_event(current, "job.succeeded", {"artifact_id": artifact_id})
    except MediaActionRequired:
        return
    except QualityGateError as exc:
        current = store.load_job(job_id)
        if not current or current["status"] == "cancelled":
            return
        failed = ", ".join(exc.report.get("failed_checks") or [])
        current["status"] = "failed"
        current["stage"] = "quality"
        current["error"] = {
            "code": "QUALITY_GATE_FAILED",
            "message": f"Automatic quality checks failed: {failed}.",
            "retryable": True,
            "failed_checks": exc.report.get("failed_checks") or [],
        }
        current["progress"] = {"percent": 94, "message": "Quality checks failed", "updated_at": utc_now()}
        saved, active = store.save_job_if_active(current)
        if not active or saved is None:
            return
        current = saved
        store.append_event(current, "job.failed", {"code": "QUALITY_GATE_FAILED", "failed_checks": exc.report.get("failed_checks") or []})
    except Exception as exc:
        logger.exception("Video render failed for job %s", job_id)
        current = store.load_job(job_id)
        if not current or current["status"] == "cancelled":
            return
        code, detail = _render_failure_detail(exc)
        failed_stage = str(current.get("stage") or "rendering")
        current["status"] = "failed"
        current["stage"] = failed_stage
        current["error"] = {
            "code": code,
            "stage": failed_stage,
            "message": detail,
            "retryable": True,
        }
        current["progress"] = {"percent": max(72, current["progress"]["percent"]), "message": "Render failed", "updated_at": utc_now()}
        saved, active = store.save_job_if_active(current)
        if not active or saved is None:
            return
        current = saved
        store.append_event(current, "job.failed", {"code": code, "stage": failed_stage, "diagnostic": type(exc).__name__})
