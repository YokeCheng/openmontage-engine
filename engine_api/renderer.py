"""Zero-key Remotion rendering for the CouncilForge platform fixture."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .store import EngineStore, new_id, utc_now
from tools.subtitle.subtitle_gen import SubtitleGen


class MediaActionRequired(RuntimeError):
    """Rendering paused until CouncilForge resolves a provider fallback."""


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
    if source_mode == "motion_graphics" and voice_provider == "none" and music_provider == "none":
        return []

    from tools.tool_registry import registry

    registry.ensure_discovered()
    asset_dir.mkdir(parents=True, exist_ok=True)
    media_assets: list[dict[str, Any]] = []
    cuts = props.get("cuts") if isinstance(props.get("cuts"), list) else []
    scenes = manifest.get("scenes") if isinstance(manifest.get("scenes"), list) else []
    aspect_ratio = str((manifest.get("render") or {}).get("aspect_ratio") or "16:9")

    def fallback(scene_id: str, capability: str) -> None:
        if str(policy.get("fallback") or "ask") == "ask":
            action_id = new_id("action")
            action = {
                "action_id": action_id,
                "job_id": job["job_id"],
                "type": "provider_fallback",
                "status": "pending",
                "title": "Media provider unavailable",
                "summary": f"The approved {capability} path is unavailable for {scene_id}.",
                "recommended_resolution": "use_motion_graphics",
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
                cut[field] = str(output_path.resolve())
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
            cut[field] = str(path)
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
                cut[field] = str(path)
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
        narration = "\n".join(str(scene.get("narration") or "").strip() for scene in scenes).strip()
        narration_path = asset_dir / "narration.mp3"
        if narration_path.is_file() and narration_path.stat().st_size > 0:
            props.setdefault("audio", {})["narration"] = {"src": str(narration_path.resolve()), "volume": 1}
            media_assets.append(
                {
                    "path": narration_path.resolve(),
                    "kind": "audio",
                    "tool": "cached",
                    "provider": voice_provider,
                    "cost_usd": 0.0,
                }
            )
        elif selector is not None and selector.get_status().value == "available" and narration:
            result = selector.execute(
                {
                    "text": narration,
                    "preferred_provider": voice_provider,
                    "speed": float((manifest.get("audio") or {}).get("voice_speed") or 1),
                    "output_path": str(narration_path),
                }
            )
            path = _first_output(result) if result.success else None
            if path:
                props.setdefault("audio", {})["narration"] = {"src": str(path), "volume": 1}
                media_assets.append(_media_asset(path, kind="audio", result=result, tool_name="tts_selector"))
            else:
                fallback("narration", "tts")

    if music_provider != "none":
        music_path = asset_dir / "music.mp3"
        if music_path.is_file() and music_path.stat().st_size > 0:
            props.setdefault("audio", {})["music"] = {
                "src": str(music_path.resolve()),
                "volume": 0.12,
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
                    "src": str(path),
                    "volume": 0.12,
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


def render_job(store: EngineStore, job_id: str, repo_root: Path) -> None:
    job = store.load_job(job_id)
    if not job or job["status"] in {"cancelled", "succeeded"}:
        return
    try:
        job["status"] = "rendering"
        job["stage"] = "rendering"
        job["progress"] = {"percent": 72, "message": "Rendering MP4", "updated_at": utc_now()}
        store.save_job(job)
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
            store.save_job(job)
            media_assets = _materialize_media(manifest, props, job_dir / "assets", store, job)
        props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
        if mode == "fixture-copy":
            fixture = Path(os.environ["OPENMONTAGE_ENGINE_FIXTURE_VIDEO"])
            shutil.copyfile(fixture, output_path)
        else:
            composer = repo_root / "remotion-composer"
            command = [
                "npx", "remotion", "render", "src/index.tsx", composition_id,
                str(output_path), f"--props={props_path}", "--codec=h264",
            ]
            process = subprocess.Popen(command, cwd=composer)
            while process.poll() is None:
                latest = store.load_job(job_id)
                if not latest or latest.get("status") == "cancelled":
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return
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
        artifacts: list[dict[str, Any]] = []
        for media_asset in media_assets:
            media_path = media_asset["path"]
            media_id = new_id("artifact")
            try:
                storage_name = str(media_path.relative_to(job_dir))
            except ValueError:
                continue
            media_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
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
                    "metadata": {
                        "provider": media_asset["provider"],
                        "tool": media_asset["tool"],
                        "cost_usd": media_asset["cost_usd"],
                    },
                }
            )
        artifacts.append(video_artifact)
        if bool(manifest.get("audio", {}).get("subtitles", True)):
            subtitle_path = (artifact_dir / "subtitles.srt").resolve()
            cursor = 0.0
            segments = []
            for scene in manifest["scenes"]:
                end = cursor + float(scene["duration_seconds"])
                segments.append({"text": scene.get("narration", ""), "start": cursor, "end": end})
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
        current["artifacts"] = artifacts
        current["status"] = "succeeded"
        current["stage"] = "delivery"
        current["progress"] = {"percent": 100, "message": "Video ready", "updated_at": utc_now()}
        store.save_job(current)
        for artifact in artifacts:
            store.append_event(
                current,
                "artifact.created",
                {"artifact_id": artifact["artifact_id"], "kind": artifact["kind"]},
            )
        store.append_event(current, "job.succeeded", {"artifact_id": artifact_id})
    except MediaActionRequired:
        return
    except Exception as exc:
        current = store.load_job(job_id)
        if not current or current["status"] == "cancelled":
            return
        current["status"] = "failed"
        current["stage"] = "rendering"
        current["error"] = {
            "code": "RENDER_FAILED",
            "message": "The video renderer could not complete this job.",
            "retryable": True,
        }
        current["progress"] = {"percent": max(72, current["progress"]["percent"]), "message": "Render failed", "updated_at": utc_now()}
        store.save_job(current)
        store.append_event(current, "job.failed", {"code": "RENDER_FAILED", "diagnostic": type(exc).__name__})
