"""Generic, model-free OpenMontage capability execution gateway.

CouncilForge owns the Agent loop.  This module exposes the upstream pipeline,
skill, checkpoint and tool contracts without embedding a second orchestrator.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from fastapi.encoders import jsonable_encoder
from jsonschema import Draft202012Validator

from lib.checkpoint import get_latest_checkpoint, init_project, write_checkpoint
from lib.pipeline_loader import load_pipeline_readonly
from tools.tool_registry import registry

from .store import canonical_digest, utc_now

logger = logging.getLogger(__name__)


ACTIVE_EXECUTION_STATES = {"queued", "running", "cancel_requested"}
TERMINAL_EXECUTION_STATES = {"succeeded", "failed", "cancelled"}

_SENSITIVE_RESULT_KEYS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
_SIGNED_URL_QUERY_KEYS = {
    "accesskeyid",
    "expires",
    "ossaccesskeyid",
    "signature",
    "x-amz-credential",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
}


def _is_sensitive_result_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_RESULT_KEYS)


def _is_signed_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.query:
        return False
    query_keys = {
        key.lower()
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    return bool(query_keys & _SIGNED_URL_QUERY_KEYS)


def _sanitize_persisted_result(value: Any) -> Any:
    """Remove credentials and temporary signed URLs before writing execution state."""

    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _is_sensitive_result_key(key):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_persisted_result(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_persisted_result(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_persisted_result(item) for item in value]
    if isinstance(value, str):
        if _is_signed_url(value):
            return "[redacted-signed-url]"
        return _redact_sensitive_text(value)
    return value


def _redact_sensitive_text(value: str) -> str:
    """Redact credentials from provider errors before persistence or logging."""

    redacted = re.sub(
        r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 [redacted]",
        value,
    )
    redacted = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[redacted]",
        redacted,
    )
    for key, secret in os.environ.items():
        if _is_sensitive_result_key(key) and secret and len(secret) >= 6:
            redacted = redacted.replace(secret, "[redacted]")
    for match in re.findall(r"https?://[^\s'\"<>]+", redacted):
        if _is_signed_url(match.rstrip(".,);]")):
            redacted = redacted.replace(match, "[redacted-signed-url]")
    return redacted[:2000]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(jsonable_encoder(value), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _safe_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _stage(manifest: dict[str, Any], stage_name: str) -> dict[str, Any] | None:
    for item in manifest.get("stages", []):
        if isinstance(item, dict) and item.get("name") == stage_name:
            return item
    return None


def _allowed_tools(stage: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("tools_available", "required_tools", "optional_tools"):
        items = stage.get(key) or []
        if isinstance(items, list):
            values.update(str(item) for item in items if isinstance(item, str))
    return values


def _looks_like_path(key: str) -> bool:
    return key in {"project_dir", "workspace_dir", "output_dir", "input_dir"} or key.endswith(
        ("_path", "_dir", "_paths", "_dirs")
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _subtitle_text(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def _caption_chunks(text: str, *, language: str) -> list[str]:
    """Split approved subtitle text into readable Remotion caption tokens."""

    cleaned = _subtitle_text(text)
    if not cleaned:
        return []
    has_cjk = language.lower().startswith("zh") or any("\u4e00" <= ch <= "\u9fff" for ch in cleaned)
    if not has_cjk:
        return [part for part in cleaned.split() if part]

    chunks: list[str] = []
    current = ""
    hard_breaks = set("，。！？；：、,.!?;:")
    for char in cleaned:
        if char.isspace():
            continue
        current += char
        if char in hard_breaks or len(current) >= 6:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _build_subtitle_payload(
    sections: list[dict[str, Any]], *, language: str
) -> tuple[str, list[dict[str, Any]], str]:
    """Return SRT text, Remotion word captions and preferred token joiner."""

    srt_blocks: list[str] = []
    captions: list[dict[str, Any]] = []
    has_cjk = language.lower().startswith("zh")
    for index, section in enumerate(sections, start=1):
        text = _subtitle_text(section.get("text"))
        if not text:
            continue
        start = float(section.get("start_seconds") or 0)
        end = max(start + 0.25, float(section.get("end_seconds") or start + 1))
        srt_blocks.append(f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}")

        chunks = _caption_chunks(text, language=language)
        if not chunks:
            continue
        has_cjk = has_cjk or any("\u4e00" <= ch <= "\u9fff" for ch in text)
        total_chars = sum(max(1, len(chunk)) for chunk in chunks)
        cursor = start
        duration = end - start
        for chunk_index, chunk in enumerate(chunks):
            if chunk_index == len(chunks) - 1:
                chunk_end = end
            else:
                chunk_end = cursor + duration * (max(1, len(chunk)) / total_chars)
            captions.append(
                {
                    "word": chunk,
                    "startMs": int(round(cursor * 1000)),
                    "endMs": int(round(chunk_end * 1000)),
                }
            )
            cursor = chunk_end
    return (
        "\n\n".join(srt_blocks) + ("\n" if srt_blocks else ""),
        captions,
        "" if has_cjk else " ",
    )


def _media_metadata(path: Path, media_type: str) -> dict[str, Any]:
    """Return best-effort technical metadata without making artifact delivery fail."""

    if media_type.startswith(("video/", "audio/")) and shutil.which("ffprobe"):
        try:
            command = [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
                "-of",
                "json",
                str(path),
            ]
            data = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
            streams = data.get("streams", [])
            video = next((item for item in streams if item.get("codec_type") == "video"), {})
            audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
            rate = str(video.get("r_frame_rate") or "0/1").split("/", maxsplit=1)
            fps = round(float(rate[0]) / float(rate[1]), 3) if len(rate) == 2 and float(rate[1]) else 0
            return {
                "duration_seconds": round(float(data.get("format", {}).get("duration") or 0), 3),
                "width": video.get("width"),
                "height": video.get("height"),
                "fps": fps,
                "video_codec": video.get("codec_name"),
                "audio_codec": audio.get("codec_name"),
                "audio_sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
                "audio_channels": audio.get("channels"),
            }
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            return {}
    if media_type.startswith("image/"):
        try:
            from PIL import Image

            with Image.open(path) as image:
                return {"width": image.width, "height": image.height, "image_format": image.format}
        except (ImportError, OSError):
            return {}
    return {}


class CapabilityGateway:
    """Executes registry tools inside tenant-owned workspaces."""

    def __init__(self, root: Path, repo_root: Path, *, max_workers: int = 2) -> None:
        self.root = root
        self.repo_root = repo_root
        self.workspaces_dir = root / "workspaces"
        self.index_path = root / "workspace-idempotency.json"
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="openmontage-tool")
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            _atomic_json(self.index_path, {})

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def recover(self) -> None:
        """Fail interrupted calls explicitly; idempotent retry remains possible."""

        for path in self.workspaces_dir.glob("*/executions/*.json"):
            try:
                execution = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if execution.get("status") in ACTIVE_EXECUTION_STATES:
                execution["status"] = "failed"
                execution["finished_at"] = utc_now()
                execution["error"] = {
                    "code": "ENGINE_RESTARTED",
                    "message": "The tool worker restarted before this execution completed.",
                    "retryable": True,
                }
                _atomic_json(path, execution)
                workspace_id = str(execution.get("workspace_id") or "")
                tenant_id = str(execution.get("tenant_id") or "")
                if workspace_id and tenant_id:
                    self._append_event(
                        workspace_id,
                        tenant_id,
                        "execution.failed",
                        {
                            "execution_id": execution.get("execution_id"),
                            "tool_name": execution.get("tool_name"),
                            "error_code": "ENGINE_RESTARTED",
                        },
                    )

    def _workspace_dir(self, workspace_id: str) -> Path:
        candidate = (self.workspaces_dir / workspace_id).resolve()
        if candidate.parent != self.workspaces_dir.resolve():
            raise ValueError("INVALID_WORKSPACE_ID")
        return candidate

    def _metadata_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "gateway.json"

    def load_workspace(self, workspace_id: str, tenant_id: str) -> dict[str, Any] | None:
        path = self._metadata_path(workspace_id)
        if not path.is_file():
            return None
        workspace = json.loads(path.read_text(encoding="utf-8"))
        return workspace if workspace.get("tenant_id") == tenant_id else None

    def create_workspace(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_id: str,
        title: str,
        pipeline: str,
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        manifest = load_pipeline_readonly(pipeline)
        body = {
            "tenant_id": tenant_id,
            "request_id": request_id,
            "title": title,
            "pipeline": pipeline,
            "metadata": metadata,
        }
        digest = canonical_digest(body)
        with self._lock:
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
            index_key = f"{tenant_id}:{idempotency_key}"
            existing = index.get(index_key)
            if existing:
                if existing.get("digest") != digest:
                    raise ValueError("IDEMPOTENCY_KEY_REUSED")
                workspace = self.load_workspace(str(existing["workspace_id"]), tenant_id)
                if workspace is None:
                    raise RuntimeError("workspace idempotency index is corrupt")
                return workspace, False

            workspace_id = _safe_id("workspace")
            project_dir = init_project(
                workspace_id,
                title=title,
                pipeline_type=pipeline,
                pipeline_dir=self.workspaces_dir,
            )
            now = utc_now()
            workspace = {
                "schema_version": "1.0",
                "workspace_id": workspace_id,
                "request_id": request_id,
                "tenant_id": tenant_id,
                "title": title,
                "pipeline": {"name": pipeline, "version": str(manifest.get("version") or "1.0")},
                "status": "active",
                "metadata": metadata,
                "created_at": now,
                "updated_at": now,
            }
            _atomic_json(project_dir / "gateway.json", workspace)
            index[index_key] = {"workspace_id": workspace_id, "digest": digest}
            _atomic_json(self.index_path, index)
            self._append_event(workspace_id, tenant_id, "workspace.created", {"pipeline": pipeline})
            return deepcopy(workspace), True

    def stage_context(self, workspace_id: str, tenant_id: str, stage_name: str) -> dict[str, Any]:
        workspace = self.load_workspace(workspace_id, tenant_id)
        if workspace is None:
            raise KeyError("WORKSPACE_NOT_FOUND")
        if workspace.get("status") != "active":
            raise RuntimeError("WORKSPACE_NOT_ACTIVE")
        manifest = load_pipeline_readonly(str(workspace["pipeline"]["name"]))
        stage = _stage(manifest, stage_name)
        if stage is None:
            raise KeyError("STAGE_NOT_FOUND")
        instruction = ""
        skill_ref = stage.get("skill")
        if isinstance(skill_ref, str) and skill_ref:
            skill_path = (self.repo_root / "skills" / f"{skill_ref}.md").resolve()
            skills_root = (self.repo_root / "skills").resolve()
            if skills_root not in skill_path.parents or not skill_path.is_file():
                raise FileNotFoundError(skill_ref)
            instruction = skill_path.read_text(encoding="utf-8")
        tools = []
        registry.ensure_discovered()
        for tool_name in sorted(_allowed_tools(stage)):
            tool = registry.get(tool_name)
            if tool is not None:
                tools.append(self._tool_info(tool))
        artifact_schemas: dict[str, Any] = {}
        schemas_root = (self.repo_root / "schemas" / "artifacts").resolve()
        for artifact_name in stage.get("produces") or []:
            if not isinstance(artifact_name, str):
                continue
            schema_path = (schemas_root / f"{artifact_name}.schema.json").resolve()
            if schemas_root in schema_path.parents and schema_path.is_file():
                artifact_schemas[artifact_name] = json.loads(schema_path.read_text(encoding="utf-8"))
        return {
            "workspace_id": workspace_id,
            "pipeline": manifest,
            "stage": stage,
            "workspace_metadata": workspace.get("metadata") or {},
            "instruction": instruction,
            "tools": tools,
            "artifact_schemas": artifact_schemas,
            "latest_checkpoint": get_latest_checkpoint(self.workspaces_dir, workspace_id),
        }

    @staticmethod
    def _tool_info(tool: Any) -> dict[str, Any]:
        info = tool.get_info()
        retry_policy = getattr(tool, "retry_policy", None)
        return {
            "name": tool.name,
            "version": tool.version,
            "provider": tool.provider,
            "capability": tool.capability,
            "status": tool.get_status().value,
            "runtime": tool.runtime.value,
            "stability": tool.stability.value,
            "input_schema": deepcopy(getattr(tool, "input_schema", {})),
            "output_schema": deepcopy(getattr(tool, "output_schema", {})),
            "best_for": info.get("best_for", []),
            "supports": info.get("supports", {}),
            "agent_skills": info.get("agent_skills", []),
            "install_instructions": info.get("install_instructions", ""),
            "dependencies": info.get("dependencies", []),
            "estimated_runtime_seconds": None,
            "retry_policy": {
                "max_retries": int(getattr(retry_policy, "max_retries", 0) or 0),
                "backoff_seconds": float(getattr(retry_policy, "backoff_seconds", 0) or 0),
                "retryable_errors": list(getattr(retry_policy, "retryable_errors", []) or []),
            },
        }

    def tool_catalog(self) -> list[dict[str, Any]]:
        registry.ensure_discovered()
        tools = [registry.get(name) for name in registry.list_all()]
        return [self._tool_info(tool) for tool in sorted((item for item in tools if item is not None), key=lambda item: item.name)]

    def tool_info(self, tool_name: str) -> dict[str, Any] | None:
        registry.ensure_discovered()
        tool = registry.get(tool_name)
        return self._tool_info(tool) if tool is not None else None

    def _resolve_path(self, workspace_dir: Path, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https", "data"}:
            return value
        candidate = Path(value)
        resolved = (candidate if candidate.is_absolute() else workspace_dir / candidate).resolve()
        if resolved != workspace_dir and workspace_dir not in resolved.parents:
            raise ValueError("TOOL_PATH_OUTSIDE_WORKSPACE")
        return str(resolved)

    def _normalize_inputs(self, workspace_dir: Path, inputs: dict[str, Any]) -> dict[str, Any]:
        def normalize(key: str, value: Any) -> Any:
            if isinstance(value, dict):
                return {nested_key: normalize(nested_key, nested_value) for nested_key, nested_value in value.items()}
            if isinstance(value, list):
                if _looks_like_path(key):
                    return [self._resolve_path(workspace_dir, item) if isinstance(item, str) else normalize(key, item) for item in value]
                return [normalize(key, item) for item in value]
            if (key == "path" or _looks_like_path(key)) and isinstance(value, str):
                return self._resolve_path(workspace_dir, value)
            return value

        result = {key: normalize(key, value) for key, value in inputs.items()}
        return result

    def _workspace_default_inputs(
        self,
        workspace_dir: Path,
        stage_name: str,
        tool_name: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Provide safe workspace-local defaults for tools used through the API.

        Several OpenMontage CLI tools historically defaulted to repo-level
        folders such as ``assets/`` or ``renders/``.  That is fine for a local
        Cursor/Claude workspace, but the service API must keep every produced
        artifact inside the isolated workspace so tenant/path checks and Range
        downloads continue to work.
        """

        result = dict(inputs)
        if tool_name == "video_compose":
            result = self._materialize_allowed_external_inputs(workspace_dir, stage_name, result)
            result = self._materialize_virtual_subtitles(workspace_dir, stage_name, result)
            if not result.get("output_path"):
                result["output_path"] = str(workspace_dir / "renders" / "final.mp4")
            return result
        if tool_name != "remotion_motion_graphics":
            return result
        operation = str(result.get("operation") or "")
        if operation == "prepare" and not result.get("output_dir"):
            result["output_dir"] = str(workspace_dir / "tool-output" / stage_name / "remotion-motion")
        if operation == "render":
            if not result.get("output_path"):
                result["output_path"] = str(workspace_dir / "renders" / "final.mp4")
            if not result.get("output_dir"):
                result["output_dir"] = str(workspace_dir / "tool-output" / stage_name / "remotion-motion")
        return result

    def _materialize_allowed_external_inputs(
        self,
        workspace_dir: Path,
        stage_name: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Copy trusted engine-owned external files into the tenant workspace.

        The service boundary intentionally rejects arbitrary paths outside the
        workspace. A small set of engine-owned resources, such as the local
        music library, are still legitimate inputs selected by previous
        OpenMontage stages. Before path normalization runs, copy those files
        into the current workspace and rewrite the payload to use the
        workspace-local copy. Everything else remains subject to the normal
        path-escape rejection.
        """

        allowed_roots = [self.repo_root / "music_library"]
        import_root = workspace_dir / "tool-inputs" / stage_name / "external"

        def inside(candidate: Path, root: Path) -> bool:
            try:
                candidate.resolve().relative_to(root.resolve())
                return True
            except ValueError:
                return False

        def materialize_file(path: Path) -> str | None:
            resolved = path.resolve()
            if not resolved.is_file() or not any(inside(resolved, root) for root in allowed_roots):
                return None
            digest = _file_sha256(resolved)[:16]
            target = import_root / f"{resolved.stem}-{digest}{resolved.suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or _file_sha256(target) != _file_sha256(resolved):
                shutil.copy2(resolved, target)
            return str(target)

        def materialize_dir(path: Path) -> str | None:
            resolved = path.resolve()
            if not resolved.is_dir():
                return None
            if not any(resolved == root.resolve() or inside(resolved, root) for root in allowed_roots):
                return None
            import_root.mkdir(parents=True, exist_ok=True)
            return str(import_root)

        def walk(key: str, value: Any) -> Any:
            if isinstance(value, dict):
                return {str(nested_key): walk(str(nested_key), nested_value) for nested_key, nested_value in value.items()}
            if isinstance(value, list):
                return [walk(key, item) for item in value]
            if isinstance(value, str) and (key == "path" or _looks_like_path(key)):
                candidate = Path(value)
                if candidate.is_absolute():
                    if replacement := materialize_file(candidate):
                        return replacement
                    if replacement := materialize_dir(candidate):
                        return replacement
            return value

        materialized = walk("", inputs)
        return materialized if isinstance(materialized, dict) else dict(inputs)

    def _materialize_virtual_subtitles(
        self,
        workspace_dir: Path,
        stage_name: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Turn CouncilForge structured subtitle references into real files.

        The platform Agent may produce ``edit_decisions.subtitles.source`` as a
        logical value such as ``script-sections:s1,s2`` because the approved
        script is already structured.  The upstream ``video_compose`` tool,
        however, intentionally deals in concrete media paths.  This adapter
        bridges the two without teaching the tool about CouncilForge-specific
        workflow state.
        """

        edit_decisions = inputs.get("edit_decisions")
        if not isinstance(edit_decisions, dict):
            return inputs
        subtitles = edit_decisions.get("subtitles")
        if not isinstance(subtitles, dict) or not subtitles.get("enabled"):
            return inputs
        source = subtitles.get("source")
        if not (
            isinstance(source, str)
            and source.startswith("script-sections:")
        ):
            return inputs
        metadata = edit_decisions.get("metadata")
        sections = metadata.get("subtitle_sections") if isinstance(metadata, dict) else None
        if not isinstance(sections, list) or not sections:
            return inputs

        language = (
            str(metadata.get("language") or "zh-CN")
            if isinstance(metadata, dict)
            else "zh-CN"
        )
        srt_text, captions, joiner = _build_subtitle_payload(
            [section for section in sections if isinstance(section, dict)],
            language=language,
        )
        if not srt_text:
            return inputs

        subtitle_dir = workspace_dir / "tool-inputs" / stage_name / "subtitles"
        subtitle_path = subtitle_dir / "script-sections.srt"
        subtitle_path.parent.mkdir(parents=True, exist_ok=True)
        subtitle_path.write_text(srt_text, encoding="utf-8")

        result = deepcopy(inputs)
        result_edit = deepcopy(edit_decisions)
        result_subtitles = deepcopy(subtitles)
        result_subtitles["source"] = str(subtitle_path)
        result_subtitles["source_type"] = "generated_srt"
        result_edit["subtitles"] = result_subtitles
        if captions:
            result_edit["captions"] = captions
            result_edit["captionJoiner"] = joiner
        result["edit_decisions"] = result_edit
        result.setdefault("subtitle_path", str(subtitle_path))
        return result

    def create_execution(
        self,
        *,
        workspace_id: str,
        tenant_id: str,
        idempotency_key: str,
        stage_name: str,
        tool_name: str,
        inputs: dict[str, Any],
        trace_id: str | None = None,
        platform_job_id: str | None = None,
        stage_attempt: int = 1,
    ) -> tuple[dict[str, Any], bool]:
        workspace = self.load_workspace(workspace_id, tenant_id)
        if workspace is None:
            raise KeyError("WORKSPACE_NOT_FOUND")
        if workspace.get("status") != "active":
            raise RuntimeError("WORKSPACE_NOT_ACTIVE")
        manifest = load_pipeline_readonly(str(workspace["pipeline"]["name"]))
        stage = _stage(manifest, stage_name)
        if stage is None:
            raise KeyError("STAGE_NOT_FOUND")
        if tool_name not in _allowed_tools(stage):
            raise PermissionError("TOOL_NOT_ALLOWED_FOR_STAGE")
        registry.ensure_discovered()
        tool = registry.get(tool_name)
        if tool is None:
            raise KeyError("TOOL_NOT_FOUND")
        if tool.get_status().value != "available":
            raise RuntimeError("TOOL_UNAVAILABLE")

        workspace_dir = self._workspace_dir(workspace_id)
        inputs_with_defaults = self._workspace_default_inputs(workspace_dir, stage_name, tool_name, inputs)
        normalized_inputs = self._normalize_inputs(workspace_dir, inputs_with_defaults)
        effective_trace_id = str(trace_id or workspace.get("request_id") or workspace_id)
        effective_job_id = str(
            platform_job_id
            or (workspace.get("metadata") or {}).get("platform_job_id")
            or ""
        ) or None
        execution_dir = workspace_dir / "executions"
        index_path = execution_dir / "idempotency.json"
        digest = canonical_digest({"stage": stage_name, "tool_name": tool_name, "inputs": normalized_inputs})
        with self._lock:
            index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
            existing = index.get(idempotency_key)
            if existing:
                if existing.get("digest") != digest:
                    raise ValueError("IDEMPOTENCY_KEY_REUSED")
                return self.get_execution(workspace_id, tenant_id, str(existing["execution_id"])), False

            execution_id = _safe_id("execution")
            now = utc_now()
            execution = {
                "schema_version": "1.0",
                "execution_id": execution_id,
                "workspace_id": workspace_id,
                "tenant_id": tenant_id,
                "pipeline": workspace["pipeline"],
                "stage": stage_name,
                "tool_name": tool_name,
                "provider": str(tool.provider or "unknown"),
                "trace_id": effective_trace_id,
                "platform_job_id": effective_job_id,
                "stage_attempt": stage_attempt,
                "status": "queued",
                "inputs_digest": digest,
                "result": None,
                "error": None,
                "artifacts": [],
                "attempt_count": 0,
                "retry_history": [],
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
            }
            _atomic_json(execution_dir / f"{execution_id}.json", execution)
            index[idempotency_key] = {"execution_id": execution_id, "digest": digest}
            _atomic_json(index_path, index)
            self._append_event(
                workspace_id,
                tenant_id,
                "execution.queued",
                {
                    "execution_id": execution_id,
                    "stage": stage_name,
                    "stage_attempt": stage_attempt,
                    "tool_name": tool_name,
                    "provider": str(tool.provider or "unknown"),
                    "trace_id": effective_trace_id,
                    "platform_job_id": effective_job_id,
                },
            )
            future = self._executor.submit(self._run_execution, execution_id, workspace_id, tenant_id, tool_name, normalized_inputs)
            self._futures[execution_id] = future
            future.add_done_callback(lambda _: self._futures.pop(execution_id, None))
            return deepcopy(execution), True

    def _execution_path(self, workspace_id: str, execution_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "executions" / f"{execution_id}.json"

    def get_execution(self, workspace_id: str, tenant_id: str, execution_id: str) -> dict[str, Any]:
        if self.load_workspace(workspace_id, tenant_id) is None:
            raise KeyError("WORKSPACE_NOT_FOUND")
        path = self._execution_path(workspace_id, execution_id)
        if not path.is_file():
            raise KeyError("EXECUTION_NOT_FOUND")
        execution = json.loads(path.read_text(encoding="utf-8"))
        if execution.get("tenant_id") != tenant_id:
            raise KeyError("EXECUTION_NOT_FOUND")
        return execution

    def _save_execution(self, execution: dict[str, Any]) -> None:
        execution["updated_at"] = utc_now()
        _atomic_json(self._execution_path(str(execution["workspace_id"]), str(execution["execution_id"])), execution)

    def _artifact_payload(self, workspace_id: str, path: Path, *, role: str = "intermediate") -> dict[str, Any]:
        workspace_dir = self._workspace_dir(workspace_id)
        resolved = path.resolve()
        if workspace_dir not in resolved.parents:
            raise ValueError("TOOL_ARTIFACT_OUTSIDE_WORKSPACE")
        relative = str(resolved.relative_to(workspace_dir))
        media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return {
            "artifact_id": _safe_id("artifact"),
            "path": relative,
            "file_name": resolved.name,
            "media_type": media_type,
            "size_bytes": resolved.stat().st_size,
            "checksum": _file_sha256(resolved),
            "metadata": _media_metadata(resolved, media_type),
            "role": role,
            "created_at": utc_now(),
        }

    def _result_artifact_paths(self, workspace_id: str, result: Any) -> list[Path]:
        workspace_dir = self._workspace_dir(workspace_id)
        candidates: list[Any] = [*(result.artifacts or [])]
        for key in ("output", "output_path", "path", "file_path"):
            candidates.append(result.data.get(key))
        paths: list[Path] = []
        for value in candidates:
            if not isinstance(value, str):
                continue
            path = Path(value)
            if path.is_file() and workspace_dir in path.resolve().parents and path.resolve() not in paths:
                paths.append(path.resolve())
        return paths

    def _run_execution(self, execution_id: str, workspace_id: str, tenant_id: str, tool_name: str, inputs: dict[str, Any]) -> None:
        execution = self.get_execution(workspace_id, tenant_id, execution_id)
        started_monotonic = time.monotonic()
        execution["status"] = "running"
        execution["started_at"] = utc_now()
        self._save_execution(execution)
        context = {
            "execution_id": execution_id,
            "stage": execution.get("stage"),
            "stage_attempt": execution.get("stage_attempt"),
            "tool_name": tool_name,
            "provider": execution.get("provider"),
            "trace_id": execution.get("trace_id"),
            "platform_job_id": execution.get("platform_job_id"),
        }
        self._append_event(workspace_id, tenant_id, "execution.started", context)
        logger.info("openmontage_tool_execution %s", json.dumps({**context, "status": "running"}, sort_keys=True))
        try:
            tool = registry.get(tool_name)
            if tool is None:
                raise RuntimeError("Tool disappeared from the registry")
            retry_policy = getattr(tool, "retry_policy", None)
            max_retries = max(0, int(getattr(retry_policy, "max_retries", 0) or 0))
            backoff_seconds = max(0.0, float(getattr(retry_policy, "backoff_seconds", 0) or 0))
            result = None
            attempt_count = 0
            while True:
                current = self.get_execution(workspace_id, tenant_id, execution_id)
                if current.get("status") == "cancel_requested":
                    current["status"] = "cancelled"
                    current["finished_at"] = utc_now()
                    self._save_execution(current)
                    self._append_event(
                        workspace_id,
                        tenant_id,
                        "execution.cancelled",
                        {
                            **context,
                            "attempt_count": attempt_count,
                            "duration_seconds": max(0.0, time.monotonic() - started_monotonic),
                            "cost_usd": 0.0,
                        },
                    )
                    return
                attempt_count += 1
                result = tool.execute(inputs)
                current = self.get_execution(workspace_id, tenant_id, execution_id)
                current["attempt_count"] = attempt_count
                if current.get("status") == "cancel_requested":
                    current["status"] = "cancelled"
                    current["finished_at"] = utc_now()
                    self._save_execution(current)
                    cancelled = {
                        **context,
                        "attempt_count": attempt_count,
                        "duration_seconds": max(0.0, time.monotonic() - started_monotonic),
                        "cost_usd": 0.0,
                    }
                    self._append_event(workspace_id, tenant_id, "execution.cancelled", cancelled)
                    logger.info(
                        "openmontage_tool_execution %s",
                        json.dumps({**cancelled, "status": "cancelled"}, sort_keys=True),
                    )
                    return
                if result.success or not bool(getattr(result, "retryable", False)) or attempt_count > max_retries:
                    break

                delay = min(8.0, backoff_seconds * (2 ** (attempt_count - 1)))
                retry_entry = {
                    "attempt": attempt_count,
                    "next_attempt": attempt_count + 1,
                    "error_code": str(getattr(result, "error_code", None) or "TOOL_FAILED"),
                    "message": _redact_sensitive_text(result.error or "Tool execution failed"),
                    "delay_seconds": delay,
                    "occurred_at": utc_now(),
                }
                current.setdefault("retry_history", []).append(retry_entry)
                self._save_execution(current)
                self._append_event(
                    workspace_id,
                    tenant_id,
                    "execution.retrying",
                    {**context, **retry_entry},
                )
                if delay:
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        latest = self.get_execution(workspace_id, tenant_id, execution_id)
                        if latest.get("status") == "cancel_requested":
                            break
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

            assert result is not None
            current = self.get_execution(workspace_id, tenant_id, execution_id)
            if current.get("status") == "cancel_requested":
                current["status"] = "cancelled"
                current["finished_at"] = utc_now()
                self._save_execution(current)
                cancelled = {
                    **context,
                    "duration_seconds": max(0.0, time.monotonic() - started_monotonic),
                    "cost_usd": 0.0,
                }
                self._append_event(
                    workspace_id,
                    tenant_id,
                    "execution.cancelled",
                    cancelled,
                )
                logger.info(
                    "openmontage_tool_execution %s",
                    json.dumps({**cancelled, "status": "cancelled"}, sort_keys=True),
                )
                return
            artifact_role = "final" if current.get("stage") in {"compose", "publish"} else "intermediate"
            artifacts = [
                self._artifact_payload(workspace_id, path, role=artifact_role)
                for path in self._result_artifact_paths(workspace_id, result)
            ]
            current["status"] = "succeeded" if result.success else "failed"
            current["attempt_count"] = attempt_count
            current["result"] = {
                "success": result.success,
                "data": _sanitize_persisted_result(jsonable_encoder(result.data)),
                "cost_usd": float(result.cost_usd or 0),
                "duration_seconds": float(result.duration_seconds or 0),
                "seed": result.seed,
                "model": result.model,
                "attempt_count": attempt_count,
            }
            current["artifacts"] = artifacts
            elapsed = max(0.0, time.monotonic() - started_monotonic)
            current["error"] = None if result.success else {
                "code": str(result.error_code or "TOOL_FAILED"),
                "message": _redact_sensitive_text(result.error or "Tool execution failed"),
                "retryable": bool(result.retryable),
            }
            current["finished_at"] = utc_now()
            self._save_execution(current)
            completion = {
                **context,
                "artifact_count": len(artifacts),
                "cost_usd": float(result.cost_usd or 0),
                "duration_seconds": float(result.duration_seconds or elapsed),
                "model": result.model,
                "error_code": current["error"]["code"] if current["error"] else None,
                "error_message": current["error"]["message"] if current["error"] else None,
                "retryable": current["error"]["retryable"] if current["error"] else False,
                "attempt_count": attempt_count,
            }
            self._append_event(
                workspace_id,
                tenant_id,
                f"execution.{current['status']}",
                completion,
            )
            logger.info(
                "openmontage_tool_execution %s",
                json.dumps({**completion, "status": current["status"]}, sort_keys=True),
            )
        except Exception as exc:
            message = _redact_sensitive_text(str(exc) or type(exc).__name__)
            elapsed = max(0.0, time.monotonic() - started_monotonic)
            current = self.get_execution(workspace_id, tenant_id, execution_id)
            current["attempt_count"] = max(1, int(current.get("attempt_count") or 0))
            current["status"] = "failed"
            current["error"] = {"code": "TOOL_EXCEPTION", "message": message, "retryable": False}
            current["finished_at"] = utc_now()
            self._save_execution(current)
            failure = {
                **context,
                "error_code": "TOOL_EXCEPTION",
                "error_message": message,
                "duration_seconds": elapsed,
                "cost_usd": 0.0,
            }
            self._append_event(workspace_id, tenant_id, "execution.failed", failure)
            logger.warning(
                "openmontage_tool_execution %s",
                json.dumps({**failure, "status": "failed"}, sort_keys=True),
            )
        finally:
            # The done callback owns cleanup. Keeping it there also covers a
            # worker that completes before create_execution stores the future.
            pass

    def cancel_execution(self, workspace_id: str, tenant_id: str, execution_id: str) -> dict[str, Any]:
        execution = self.get_execution(workspace_id, tenant_id, execution_id)
        if execution["status"] in TERMINAL_EXECUTION_STATES:
            return execution
        future = self._futures.get(execution_id)
        if future is not None and future.cancel():
            execution["status"] = "cancelled"
            execution["finished_at"] = utc_now()
        else:
            execution["status"] = "cancel_requested"
        self._save_execution(execution)
        self._append_event(
            workspace_id,
            tenant_id,
            "execution.cancelled" if execution["status"] == "cancelled" else "execution.cancel_requested",
            {
                "execution_id": execution_id,
                "stage": execution.get("stage"),
                "stage_attempt": execution.get("stage_attempt"),
                "tool_name": execution.get("tool_name"),
                "provider": execution.get("provider"),
                "trace_id": execution.get("trace_id"),
                "platform_job_id": execution.get("platform_job_id"),
                "cost_usd": 0.0,
            },
        )
        return execution

    def cancel_workspace(self, workspace_id: str, tenant_id: str) -> dict[str, Any]:
        workspace = self.load_workspace(workspace_id, tenant_id)
        if workspace is None:
            raise KeyError("WORKSPACE_NOT_FOUND")
        execution_dir = self._workspace_dir(workspace_id) / "executions"
        cancelled: list[str] = []
        for path in execution_dir.glob("execution_*.json"):
            try:
                execution = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if execution.get("status") in ACTIVE_EXECUTION_STATES:
                self.cancel_execution(workspace_id, tenant_id, str(execution["execution_id"]))
                cancelled.append(str(execution["execution_id"]))
        workspace["status"] = "cancelled"
        workspace["updated_at"] = utc_now()
        _atomic_json(self._metadata_path(workspace_id), workspace)
        self._append_event(workspace_id, tenant_id, "workspace.cancelled", {"execution_ids": cancelled})
        return {"workspace_id": workspace_id, "status": "cancelled", "cancelled_execution_ids": cancelled}

    def write_checkpoint(self, workspace_id: str, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self.load_workspace(workspace_id, tenant_id)
        if workspace is None:
            raise KeyError("WORKSPACE_NOT_FOUND")
        if payload.get("status") in {"awaiting_human", "completed"}:
            context = self.stage_context(workspace_id, tenant_id, str(payload["stage"]))
            expected = {str(name) for name in context["stage"].get("produces") or []}
            artifacts = payload.get("artifacts") or {}
            missing = sorted(expected - set(artifacts))
            if missing:
                raise ValueError(f"CANONICAL_ARTIFACTS_MISSING:{','.join(missing)}")
            for artifact_name in sorted(expected):
                schema = context["artifact_schemas"].get(artifact_name)
                if not isinstance(schema, dict):
                    raise ValueError(f"CANONICAL_ARTIFACT_SCHEMA_MISSING:{artifact_name}")
                errors = sorted(
                    Draft202012Validator(schema).iter_errors(artifacts[artifact_name]),
                    key=lambda item: list(item.path),
                )
                if errors:
                    location = "/".join(str(part) for part in errors[0].path) or "$"
                    raise ValueError(f"CANONICAL_ARTIFACT_INVALID:{artifact_name}:{location}:{errors[0].message}")
        path = write_checkpoint(
            self.workspaces_dir,
            workspace_id,
            payload["stage"],
            payload["status"],
            payload.get("artifacts", {}),
            pipeline_type=str(workspace["pipeline"]["name"]),
            human_approval_required=bool(payload.get("human_approval_required")),
            human_approved=bool(payload.get("human_approved")),
            review=payload.get("review"),
            cost_snapshot=payload.get("cost_snapshot"),
            error=payload.get("error"),
            metadata=payload.get("metadata"),
        )
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        self._append_event(workspace_id, tenant_id, "checkpoint.written", {"stage": payload["stage"], "status": payload["status"]})
        return checkpoint

    def latest_checkpoint(self, workspace_id: str, tenant_id: str) -> dict[str, Any] | None:
        if self.load_workspace(workspace_id, tenant_id) is None:
            raise KeyError("WORKSPACE_NOT_FOUND")
        return get_latest_checkpoint(self.workspaces_dir, workspace_id)

    def artifacts(self, workspace_id: str, tenant_id: str) -> list[dict[str, Any]]:
        if self.load_workspace(workspace_id, tenant_id) is None:
            raise KeyError("WORKSPACE_NOT_FOUND")
        result: list[dict[str, Any]] = []
        for path in (self._workspace_dir(workspace_id) / "executions").glob("execution_*.json"):
            try:
                execution = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            result.extend(execution.get("artifacts", []))
        return result

    def artifact_path(self, workspace_id: str, tenant_id: str, artifact_id: str) -> Path | None:
        artifact = next((item for item in self.artifacts(workspace_id, tenant_id) if item.get("artifact_id") == artifact_id), None)
        if artifact is None:
            return None
        candidate = (self._workspace_dir(workspace_id) / str(artifact["path"])).resolve()
        workspace_dir = self._workspace_dir(workspace_id)
        return candidate if candidate.is_file() and workspace_dir in candidate.parents else None

    def _events_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "gateway-events.jsonl"

    def _append_event(self, workspace_id: str, tenant_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            events = self.events(workspace_id, tenant_id)
            workspace = self.load_workspace(workspace_id, tenant_id) or {}
            metadata = workspace.get("metadata") if isinstance(workspace.get("metadata"), dict) else {}
            safe_data = _sanitize_persisted_result(data)
            event = {
                "schema_version": "1.0",
                "event_id": _safe_id("event"),
                "workspace_id": workspace_id,
                "tenant_id": tenant_id,
                "trace_id": str(safe_data.get("trace_id") or workspace.get("request_id") or workspace_id),
                "platform_job_id": safe_data.get("platform_job_id") or metadata.get("platform_job_id"),
                "execution_id": safe_data.get("execution_id"),
                "stage": safe_data.get("stage"),
                "stage_attempt": safe_data.get("stage_attempt"),
                "tool_name": safe_data.get("tool_name"),
                "provider": safe_data.get("provider"),
                "sequence": len(events) + 1,
                "type": event_type,
                "occurred_at": utc_now(),
                "data": safe_data,
            }
            path = self._events_path(workspace_id)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def events(self, workspace_id: str, tenant_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        if self.load_workspace(workspace_id, tenant_id) is None:
            return []
        path = self._events_path(workspace_id)
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(event.get("sequence", 0)) > after_sequence:
                events.append(event)
        return events

    def agent_skill(self, skill_name: str) -> dict[str, Any] | None:
        """Return one Layer 3 skill document referenced by a tool contract."""

        if not skill_name or "/" in skill_name or "\\" in skill_name or skill_name in {".", ".."}:
            return None
        root = (self.repo_root / ".agents" / "skills").resolve()
        path = (root / skill_name / "SKILL.md").resolve()
        if root not in path.parents or not path.is_file():
            return None
        return {"name": skill_name, "content": path.read_text(encoding="utf-8")}
