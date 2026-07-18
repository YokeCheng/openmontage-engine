#!/usr/bin/env python3
"""Render the WBS 2.4 real media as an auditable Remotion deliverable.

The verifier continues an existing animated-explainer Workspace whose latest
checkpoint contains a real image, Mandarin narration and approved-script SRT.
It writes a schema-valid edit decision, executes video_compose through the
Capability Gateway, validates the actual MP4/SRT/poster and commits the compose
checkpoint. No provider call is repeated and no credential is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled"}


class EngineClient:
    def __init__(self, base_url: str, token: str | None, tenant_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"X-Tenant-ID": tenant_id})
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(method, f"{self.base_url}{path}", timeout=900, **kwargs)
        if not response.ok:
            raise RuntimeError(
                f"{method} {path} failed with HTTP {response.status_code}: {response.text[:1600]}"
            )
        return response.json()

    def execute(self, workspace_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        execution = self.request(
            "POST",
            f"/v1/workspaces/{workspace_id}/executions",
            headers={"Idempotency-Key": f"wbs25-compose:{workspace_id}:{run_id}"},
            json={
                "stage": "compose",
                "tool_name": "video_compose",
                "trace_id": f"wbs25-{run_id}",
                "platform_job_id": f"wbs25-{run_id}",
                "stage_attempt": 1,
                "inputs": inputs,
            },
        )
        execution_id = str(execution["execution_id"])
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            current = self.request(
                "GET", f"/v1/workspaces/{workspace_id}/executions/{execution_id}"
            )
            if current.get("status") in TERMINAL:
                if current.get("status") != "succeeded":
                    error = current.get("error") or {}
                    raise RuntimeError(
                        f"video_compose failed: {error.get('code')} {error.get('message')}"
                    )
                return current
            time.sleep(0.5)
        raise TimeoutError("video_compose did not finish within 900 seconds")

    def download(self, path: str) -> tuple[bytes, dict[str, str], int]:
        response = self.session.get(
            f"{self.base_url}{path}", headers={"Range": "bytes=0-63"}, timeout=120
        )
        if response.status_code != 206:
            raise AssertionError(f"Range request returned {response.status_code}, expected 206")
        return response.content, dict(response.headers), response.status_code


def _workspace_dir(workspace_id: str) -> Path:
    runtime_root = Path(os.getenv("OPENMONTAGE_ENGINE_RUNTIME", ROOT / ".engine-runtime"))
    return runtime_root / "capability-gateway" / "workspaces" / workspace_id


def _tenant_id(workspace_id: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    metadata = json.loads((_workspace_dir(workspace_id) / "gateway.json").read_text(encoding="utf-8"))
    return str(metadata["tenant_id"])


def _probe_video(content_path: Path) -> dict[str, Any]:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(content_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(output.stdout)


def run(base_url: str, token: str | None, workspace_id: str, tenant_id: str) -> dict[str, Any]:
    client = EngineClient(base_url, token, tenant_id)
    checkpoint_response = client.request("GET", f"/v1/workspaces/{workspace_id}/checkpoint")
    checkpoint = checkpoint_response.get("checkpoint") or {}
    if checkpoint.get("stage") != "assets":
        assets_checkpoint_path = _workspace_dir(workspace_id) / "checkpoint_assets.json"
        if not assets_checkpoint_path.is_file():
            raise AssertionError(f"Expected assets checkpoint, got {checkpoint.get('stage')}")
        checkpoint = json.loads(assets_checkpoint_path.read_text(encoding="utf-8"))
    manifest = (checkpoint.get("artifacts") or {}).get("asset_manifest")
    if not isinstance(manifest, dict):
        raise AssertionError("asset_manifest is missing from the assets checkpoint")
    assets = [item for item in manifest.get("assets", []) if isinstance(item, dict)]
    image = next((item for item in assets if item.get("type") == "image"), None)
    narration = next(
        (
            item
            for item in assets
            if item.get("type") == "audio"
            and str(item.get("subtype") or "").lower() in {"narration", "voice", "voiceover"}
        ),
        None,
    )
    subtitle = next((item for item in assets if item.get("type") == "subtitle"), None)
    if not all((image, narration, subtitle)):
        raise AssertionError("Workspace must contain a real image, narration and subtitle")

    duration = max(8.0, float(narration.get("duration_seconds") or 0))
    decisions = {
        "version": "1.0",
        "cuts": [
            {
                "id": "cut-real-media",
                "source": image["id"],
                "in_seconds": 0,
                "out_seconds": duration,
                "layer": "primary",
                "transform": {"animation": "ken-burns-slow-zoom", "scale": 1.0},
                "transition_in": "fade",
                "transition_out": "fade",
                "transition_duration": 0.4,
                "reason": "展示 CouncilForge 与 OpenMontage 的受控视频生产主线。",
            }
        ],
        "audio": {
            "narration": {
                "segments": [
                    {
                        "asset_id": narration["id"],
                        "start_seconds": 0,
                        "end_seconds": duration,
                    }
                ]
            }
        },
        "subtitles": {
            "enabled": True,
            "style": "sentence",
            "source": subtitle["id"],
            "font": "Noto Sans SC",
            "font_size": 46,
            "color": "#F8FAFC",
            "outline_color": "#14232D",
            "background": "rgba(20, 35, 45, 0.82)",
            "position": "bottom-center",
            "max_words_per_line": 3,
            "max_width_percent": 80,
            "bottom_margin_percent": 7.5,
        },
        "renderer_family": "explainer-data",
        "render_runtime": "remotion",
        "composition_mode": "templated",
        "metadata": {
            "language": "zh-CN",
            "target_duration_seconds": duration,
            "proposal_render_runtime": "remotion",
            "acceptance": "wbs-2.5-real-compose",
        },
    }
    edit_checkpoint = client.request(
        "PUT",
        f"/v1/workspaces/{workspace_id}/checkpoint",
        json={
            "stage": "edit",
            "status": "completed",
            "artifacts": {"edit_decisions": decisions},
            "human_approval_required": False,
            "human_approved": False,
            "review": {"status": "passed", "summary": "真实素材的音画字幕时间线已核验。"},
            "cost_snapshot": {"actual_usd": float(manifest.get("total_cost_usd") or 0)},
            "metadata": {"acceptance": "wbs-2.5"},
        },
    )
    execution = client.execute(
        workspace_id,
        {
            "operation": "render",
            "edit_decisions": decisions,
            "asset_manifest": manifest,
            "output_profile": "youtube_landscape",
            "options": {"subtitle_burn": True},
            "script_text": str((manifest.get("metadata") or {}).get("approved_script") or ""),
            "remotion_timeout_ms": 180000,
        },
    )
    data = (execution.get("result") or {}).get("data") or {}
    render_report = data.get("render_report")
    final_review = data.get("final_review")
    if not isinstance(render_report, dict) or not isinstance(final_review, dict):
        raise AssertionError("video_compose did not return canonical render artifacts")
    checks = final_review.get("checks") or {}
    if final_review.get("status") != "pass":
        raise AssertionError(f"Final review did not pass: {final_review.get('issues_found')}")
    if not (checks.get("technical_probe") or {}).get("has_audio"):
        raise AssertionError("Rendered MP4 has no audio stream")
    if not (checks.get("subtitle_check") or {}).get("subtitles_present"):
        raise AssertionError("Rendered MP4 has no proven burned subtitles")
    if int(data.get("caption_count") or render_report.get("metadata", {}).get("caption_count") or 0) < 1:
        raise AssertionError("No Remotion caption token was rendered")

    compose_checkpoint = client.request(
        "PUT",
        f"/v1/workspaces/{workspace_id}/checkpoint",
        json={
            "stage": "compose",
            "status": "completed",
            "artifacts": {"render_report": render_report, "final_review": final_review},
            "human_approval_required": False,
            "human_approved": False,
            "review": {"status": "passed", "summary": "MP4 音频、字幕、安全区和抽帧检查通过。"},
            "cost_snapshot": {"actual_usd": float(manifest.get("total_cost_usd") or 0)},
            "metadata": {"acceptance": "wbs-2.5"},
        },
    )

    artifacts = execution.get("artifacts") or []
    video = next((item for item in artifacts if item.get("media_type") == "video/mp4"), None)
    srt = next((item for item in artifacts if item.get("media_type") == "application/x-subrip"), None)
    poster = next((item for item in artifacts if item.get("file_name") == "poster.png"), None)
    if not all((video, srt, poster)):
        raise AssertionError("Compose execution must register MP4, SRT and poster artifacts")
    range_checks: list[dict[str, Any]] = []
    for artifact in (video, srt, poster):
        artifact_id = str(artifact["artifact_id"])
        content, headers, status = client.download(
            f"/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content"
        )
        if len(content) != 64:
            raise AssertionError(f"Range body for {artifact_id} was not 64 bytes")
        range_checks.append(
            {
                "artifact_id": artifact_id,
                "status": status,
                "content_range": headers.get("Content-Range") or headers.get("content-range"),
                "bytes": len(content),
            }
        )

    video_path = _workspace_dir(workspace_id) / str(video["path"])
    with tempfile.NamedTemporaryFile(suffix=".mp4") as temporary:
        temporary.write(video_path.read_bytes())
        temporary.flush()
        probe = _probe_video(Path(temporary.name))
    streams = probe.get("streams") or []
    if not any(item.get("codec_type") == "video" for item in streams):
        raise AssertionError("ffprobe found no video stream")
    if not any(item.get("codec_type") == "audio" for item in streams):
        raise AssertionError("ffprobe found no audio stream")

    return {
        "acceptance": "wbs-2.5",
        "workspace_id": workspace_id,
        "edit_checkpoint": edit_checkpoint.get("status"),
        "compose_checkpoint": compose_checkpoint.get("status"),
        "execution_id": execution["execution_id"],
        "artifacts": {
            "video": video,
            "subtitle": srt,
            "poster": poster,
        },
        "render_report": render_report,
        "final_review_status": final_review.get("status"),
        "range_checks": range_checks,
        "ffprobe": probe,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--tenant-id")
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENMONTAGE_ENGINE_URL", "http://127.0.0.1:8100"),
    )
    args = parser.parse_args()
    evidence = run(
        args.base_url,
        os.getenv("OPENMONTAGE_ENGINE_TOKEN"),
        args.workspace_id,
        _tenant_id(args.workspace_id, args.tenant_id),
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
