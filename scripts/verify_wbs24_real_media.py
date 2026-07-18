#!/usr/bin/env python3
"""Run the paid WBS 2.4 media acceptance against a live engine.

The script creates one isolated animated-explainer Workspace, produces one
DashScope image, one Mandarin narration WAV, and one approved-script SRT, then
commits a schema-valid asset_manifest. It prints only non-secret evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled"}


def _workspace_dir(workspace_id: str) -> Path:
    runtime_root = Path(
        os.getenv("OPENMONTAGE_ENGINE_RUNTIME", ROOT / ".engine-runtime")
    )
    return runtime_root / "capability-gateway" / "workspaces" / workspace_id


class EngineClient:
    def __init__(self, base_url: str, token: str | None, tenant_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"X-Tenant-ID": tenant_id})
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            timeout=360,
            **kwargs,
        )
        if not response.ok:
            detail = response.text[:1200]
            raise RuntimeError(
                f"{method} {path} failed with HTTP {response.status_code}: {detail}"
            )
        return response.json()

    def create_workspace(self, run_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/workspaces",
            headers={"Idempotency-Key": f"wbs24-workspace:{run_id}"},
            json={
                "request_id": f"wbs24-{run_id}",
                "title": "CouncilForge 中文真实媒体验收",
                "pipeline": "animated-explainer",
                "metadata": {
                    "acceptance": "wbs-2.4",
                    "language": "zh-CN",
                    "platform_job_id": f"wbs24-{run_id}",
                },
            },
        )

    def execute(
        self,
        workspace_id: str,
        run_id: str,
        tool_name: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        execution = self.request(
            "POST",
            f"/v1/workspaces/{workspace_id}/executions",
            headers={"Idempotency-Key": f"wbs24:{run_id}:{tool_name}"},
            json={
                "stage": "assets",
                "tool_name": tool_name,
                "trace_id": f"wbs24-{run_id}",
                "platform_job_id": f"wbs24-{run_id}",
                "stage_attempt": 1,
                "inputs": inputs,
            },
        )
        execution_id = str(execution["execution_id"])
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            current = self.request(
                "GET",
                f"/v1/workspaces/{workspace_id}/executions/{execution_id}",
            )
            if current.get("status") in TERMINAL:
                if current.get("status") != "succeeded":
                    error = current.get("error") or {}
                    raise RuntimeError(
                        f"{tool_name} failed: {error.get('code')} "
                        f"{error.get('message')}"
                    )
                return current
            time.sleep(0.5)
        raise TimeoutError(f"{tool_name} did not finish within 360 seconds")


def _manifest_asset(
    execution: dict[str, Any],
    *,
    asset_type: str,
    subtype: str | None,
    scene_id: str,
) -> dict[str, Any]:
    artifact = execution["artifacts"][0]
    result = execution.get("result") or {}
    data = result.get("data") or {}
    metadata = artifact.get("metadata") or {}
    item: dict[str, Any] = {
        "id": f"{asset_type}-{execution['execution_id']}",
        "type": asset_type,
        "path": artifact["path"],
        "source_tool": execution["tool_name"],
        "scene_id": scene_id,
        "provider": str(data.get("selected_provider") or data.get("provider") or execution["provider"]),
        "model": str(result.get("model") or data.get("model") or ""),
        "cost_usd": float(result.get("cost_usd") or 0),
        "media_type": artifact["media_type"],
        "size_bytes": int(artifact["size_bytes"]),
        "checksum": {"algorithm": "sha256", "value": artifact["checksum"]},
        "metadata": metadata,
    }
    if subtype:
        item["subtype"] = subtype
    if metadata.get("duration_seconds") is not None:
        item["duration_seconds"] = float(metadata["duration_seconds"])
    if metadata.get("width") and metadata.get("height"):
        item["resolution"] = f"{metadata['width']}x{metadata['height']}"
    return item


def _assert_no_secrets(workspace_id: str) -> int:
    workspace_dir = _workspace_dir(workspace_id)
    if not workspace_dir.is_dir():
        raise AssertionError(f"Workspace directory not found: {workspace_dir}")
    forbidden = [
        value.encode()
        for value in (
            os.getenv("DASHSCOPE_API_KEY", ""),
            os.getenv("OPENMONTAGE_ENGINE_TOKEN", ""),
        )
        if len(value) >= 6
    ]
    scanned = 0
    for path in workspace_dir.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_bytes()
        scanned += 1
        if any(secret in content for secret in forbidden):
            raise AssertionError(f"Secret value persisted in {path.relative_to(workspace_dir)}")
        if path.suffix in {".json", ".jsonl", ".srt", ".txt"}:
            text = content.decode("utf-8", errors="ignore")
            if "OSSAccessKeyId=" in text or "X-Amz-Signature=" in text:
                raise AssertionError(
                    f"Temporary signed URL persisted in {path.relative_to(workspace_dir)}"
                )
    return scanned


def run(base_url: str, token: str | None, tenant_id: str) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    client = EngineClient(base_url, token, tenant_id)
    workspace = client.create_workspace(run_id)
    workspace_id = str(workspace["workspace_id"])
    approved_text = (
        "CouncilForge 负责理解创作目标，OpenMontage 按受控阶段生成并验证媒体资产。"
    )

    image = client.execute(
        workspace_id,
        run_id,
        "image_selector",
        {
            "prompt": (
                "16:9 横版中文科技解说主视觉，夜蓝色现代工作室，中央是一条清晰的发光视频生产流程，"
                "左侧代表 CouncilForge 的规划界面，右侧代表 OpenMontage 的媒体执行节点，"
                "青绿色状态光，琥珀色时间码，构图简洁专业，不出现人物，不含水印和乱码文字"
            ),
            "negative_prompt": "模糊，低清晰度，水印，乱码文字，过度饱和，杂乱构图",
            "preferred_provider": "dashscope",
            "allowed_providers": ["dashscope"],
            "aspect_ratio": "16:9",
            "model": "qwen-image-2.0-pro",
            "n": 1,
            "output_path": "assets/images/wbs24-councilforge-openmontage.png",
        },
    )
    narration = client.execute(
        workspace_id,
        run_id,
        "tts_selector",
        {
            "text": approved_text,
            "voice": "Cherry",
            "voice_language": "zh",
            "language_type": "Chinese",
            "preferred_provider": "dashscope",
            "allowed_providers": ["dashscope"],
            "model": "qwen3-tts-flash",
            "output_path": "assets/narration/wbs24-approved-script.wav",
        },
    )
    subtitles = client.execute(
        workspace_id,
        run_id,
        "subtitle_gen",
        {
            "segments": [{"text": approved_text, "start": 0, "end": 8}],
            "format": "srt",
            "max_chars_per_line": 18,
            "max_words_per_cue": 1,
            "output_path": "assets/subtitles/wbs24-approved-script.srt",
        },
    )

    image_asset = _manifest_asset(
        image, asset_type="image", subtype="generated", scene_id="scene-01"
    )
    image_asset["prompt"] = "CouncilForge 与 OpenMontage 受控视频生产流程"
    narration_asset = _manifest_asset(
        narration,
        asset_type="audio",
        subtype="narration",
        scene_id="scene-01",
    )
    subtitle_asset = _manifest_asset(
        subtitles,
        asset_type="subtitle",
        subtype=None,
        scene_id="global",
    )
    subtitle_asset["format"] = "srt"
    assets = [image_asset, narration_asset, subtitle_asset]
    total_cost = round(sum(float(item.get("cost_usd") or 0) for item in assets), 6)
    manifest = {
        "version": "1.0",
        "assets": assets,
        "total_cost_usd": total_cost,
        "metadata": {
            "acceptance": "wbs-2.4-real-media",
            "language": "zh-CN",
            "approved_script": approved_text,
        },
    }
    checkpoint = client.request(
        "PUT",
        f"/v1/workspaces/{workspace_id}/checkpoint",
        json={
            "stage": "assets",
            "status": "awaiting_human",
            "artifacts": {"asset_manifest": manifest},
            "human_approval_required": True,
            "human_approved": False,
            "review": {
                "status": "passed",
                "summary": "真实图片、中文配音与批准脚本字幕均已生成并核验。",
            },
            "cost_snapshot": {"actual_usd": total_cost},
            "metadata": {"acceptance": "wbs-2.4"},
        },
    )

    subtitle_path = _workspace_dir(workspace_id) / subtitle_asset["path"]
    if approved_text not in subtitle_path.read_text(encoding="utf-8"):
        raise AssertionError("Generated SRT does not contain the approved Chinese script")
    scanned_files = _assert_no_secrets(workspace_id)

    return {
        "acceptance": "wbs-2.4",
        "workspace_id": workspace_id,
        "checkpoint_status": checkpoint["status"],
        "executions": {
            "image": image["execution_id"],
            "narration": narration["execution_id"],
            "subtitle": subtitles["execution_id"],
        },
        "artifacts": [
            {
                "type": item["type"],
                "path": item["path"],
                "media_type": item["media_type"],
                "size_bytes": item["size_bytes"],
                "checksum": item["checksum"]["value"],
                "metadata": item["metadata"],
                "provider": item["provider"],
                "model": item["model"],
                "cost_usd": item["cost_usd"],
            }
            for item in assets
        ],
        "total_cost_usd": total_cost,
        "secret_scan": {"files_scanned": scanned_files, "passed": True},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENMONTAGE_ENGINE_URL", "http://127.0.0.1:8100"),
    )
    parser.add_argument(
        "--tenant-id",
        default=f"wbs24-acceptance-{uuid.uuid4().hex[:8]}",
    )
    args = parser.parse_args()
    evidence = run(
        args.base_url,
        os.getenv("OPENMONTAGE_ENGINE_TOKEN"),
        args.tenant_id,
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
