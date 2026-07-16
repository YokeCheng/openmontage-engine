"""Zero-key Remotion rendering for the CouncilForge platform fixture."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .store import EngineStore, new_id, utc_now


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
        props = {
            "title": manifest["title"],
            "objective": manifest["objective"],
            "format": manifest["format"],
            "language": manifest.get("language", "zh-CN"),
            "scenes": manifest["scenes"],
            "render": manifest["render"],
        }
        props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

        mode = os.getenv("OPENMONTAGE_ENGINE_RENDER_MODE", "remotion")
        if mode == "fixture-copy":
            fixture = Path(os.environ["OPENMONTAGE_ENGINE_FIXTURE_VIDEO"])
            shutil.copyfile(fixture, output_path)
        else:
            composer = repo_root / "remotion-composer"
            command = [
                "npx", "remotion", "render", "src/index.tsx", "CouncilForgePlatform",
                str(output_path), f"--props={props_path}", "--codec=h264",
            ]
            subprocess.run(command, cwd=composer, check=True)

        current = store.load_job(job_id)
        if not current or current["status"] == "cancelled":
            return
        digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
        metadata = _ffprobe(output_path)
        artifact_id = new_id("artifact")
        artifact = {
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
        current["artifacts"] = [artifact]
        current["status"] = "succeeded"
        current["stage"] = "delivery"
        current["progress"] = {"percent": 100, "message": "Video ready", "updated_at": utc_now()}
        store.save_job(current)
        store.append_event(current, "artifact.created", {"artifact_id": artifact_id, "kind": "video"})
        store.append_event(current, "job.succeeded", {"artifact_id": artifact_id})
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
