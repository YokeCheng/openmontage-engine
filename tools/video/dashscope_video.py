"""DashScope Wan text-to-video and image-to-video provider.

The provider uses DashScope's asynchronous native API.  It deliberately
returns no temporary provider URL or raw response body because both may carry
sensitive query parameters.  A caller can resume an already-created remote
task by passing ``provider_task_id``.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
_DEFAULT_TEXT_MODEL = "wan2.6-t2v"
_DEFAULT_IMAGE_MODEL = "wan2.6-i2v-flash"
_TEXT_SIZE_BY_RATIO = {
    "16:9": "1280*720",
    "9:16": "720*1280",
    "1:1": "960*960",
}
_TERMINAL_FAILURE_STATES = {"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}
_PENDING_STATES = {"PENDING", "RUNNING", "WAITING", "QUEUED"}
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


class DashscopeVideo(BaseTool):
    name = "dashscope_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "dashscope"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:DASHSCOPE_API_KEY"]
    install_instructions = (
        "Set DASHSCOPE_API_KEY to an Alibaba Cloud Model Studio key. "
        "Optionally set DASHSCOPE_BASE_URL for a region/workspace endpoint."
    )
    agent_skills = ["dashscope", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": True,
        "negative_prompt": True,
        "aspect_ratio": True,
        "prompt_extend": True,
        "remote_task_resume": True,
    }
    best_for = [
        "Chinese-language product and knowledge explainer hero shots",
        "Wan text-to-video and first-frame image-to-video generation",
        "resumable asynchronous generation through Alibaba Cloud Model Studio",
    ]
    not_good_for = ["offline generation", "zero-cost production", "sub-two-second clips"]
    fallback_tools = [
        "wan_video",
        "kling_official_video",
        "seedance_video",
        "veo_video",
    ]
    quality_score = 0.86

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "maxLength": 1500},
            "negative_prompt": {"type": "string", "maxLength": 500},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "model": {"type": "string"},
            "model_name": {"type": "string"},
            "duration": {
                "description": "Wan 2.6 duration in seconds; normalized to 2-15.",
                "default": 5,
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1"],
                "default": "16:9",
            },
            "reference_image_path": {"type": "string"},
            "reference_image_url": {"type": "string"},
            "image_url": {"type": "string"},
            "prompt_extend": {"type": "boolean", "default": True},
            "watermark": {"type": "boolean", "default": False},
            "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
            "shot_type": {
                "type": "string",
                "enum": ["single", "multi"],
                "default": "single",
            },
            "provider_task_id": {
                "type": "string",
                "description": "Previously created DashScope task to poll without resubmitting.",
            },
            "poll_interval_seconds": {"type": "number", "minimum": 0, "default": 5},
            "timeout_seconds": {"type": "number", "minimum": 0, "default": 300},
            "output_path": {"type": "string"},
            "workspace_root": {
                "type": "string",
                "description": "Optional path boundary for local reference-image validation.",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=512,
        vram_mb=0,
        disk_mb=500,
        network_required=True,
    )
    retry_policy = RetryPolicy(
        max_retries=0,
        retryable_errors=[
            "PROVIDER_TASK_PENDING",
            "PROVIDER_RATE_LIMITED",
            "PROVIDER_TRANSIENT_ERROR",
        ],
    )
    idempotency_key_fields = [
        "prompt",
        "negative_prompt",
        "operation",
        "model",
        "model_name",
        "duration",
        "aspect_ratio",
        "reference_image_path",
        "reference_image_url",
        "image_url",
        "prompt_extend",
        "watermark",
        "seed",
        "shot_type",
    ]
    side_effects = [
        "calls the paid DashScope Wan video-generation API",
        "writes a generated MP4 to output_path",
    ]
    user_visible_verification = [
        "Watch the clip for prompt adherence, motion coherence, text artifacts, and brand safety"
    ]

    def get_status(self) -> ToolStatus:
        return (
            ToolStatus.AVAILABLE
            if os.environ.get("DASHSCOPE_API_KEY")
            else ToolStatus.UNAVAILABLE
        )

    def is_operation_available(self, operation: str) -> bool:
        return operation in {"text_to_video", "image_to_video"} and (
            self.get_status() == ToolStatus.AVAILABLE
        )

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Conservative planning estimate only. The provider bills successful
        # output seconds and current console pricing is the source of truth.
        duration = self._normalize_duration(inputs.get("duration", 5))
        return round(duration * 0.10, 4)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        duration = self._normalize_duration(inputs.get("duration", 5))
        return float(max(60, min(300, 60 + duration * 12)))

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        result = super().dry_run(inputs)
        result.update(
            {
                "paid_api": True,
                "model": self._model_for(inputs),
                "cost_estimate_confidence": "low",
                "cost_estimate_basis": (
                    "Conservative planning estimate; Alibaba Cloud console "
                    "pricing and successful output duration are authoritative."
                ),
            }
        )
        return result

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            return ToolResult(
                success=False,
                data={"provider": self.provider, "charged": False},
                error="DASHSCOPE_API_KEY not set. " + self.install_instructions,
                error_code="PROVIDER_AUTH_NOT_CONFIGURED",
            )

        import requests

        started = time.monotonic()
        task_id = str(inputs.get("provider_task_id") or "").strip() or None
        model = self._model_for(inputs)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        base_url = os.environ.get("DASHSCOPE_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")

        try:
            if task_id is None:
                create_response = requests.post(
                    f"{base_url}/services/aigc/video-generation/video-synthesis",
                    headers=headers,
                    json=self._build_create_payload(inputs),
                    timeout=30,
                )
                create_response.raise_for_status()
                task_id = self._task_id(create_response.json())
                if not task_id:
                    return ToolResult(
                        success=False,
                        data={"provider": self.provider, "charged": False},
                        error="DashScope did not return a video task ID.",
                        error_code="PROVIDER_RESPONSE_INVALID",
                    )

            poll_result = self._poll_task(
                requests=requests,
                base_url=base_url,
                headers=headers,
                task_id=task_id,
                timeout_seconds=float(inputs.get("timeout_seconds", 300)),
                poll_interval_seconds=float(
                    inputs.get("poll_interval_seconds", 5)
                ),
            )
            status = self._task_status(poll_result)
            if status in _PENDING_STATES:
                return self._incomplete_result(
                    task_id=task_id,
                    model=model,
                    started=started,
                    error="DashScope video task is still running and must be resumed.",
                    error_code="PROVIDER_TASK_PENDING",
                )
            if status in _TERMINAL_FAILURE_STATES:
                return ToolResult(
                    success=False,
                    data={
                        "provider": self.provider,
                        "task_id": task_id,
                        "external_task_id": task_id,
                        "charged": False,
                    },
                    error="DashScope video generation failed.",
                    error_code="PROVIDER_TASK_FAILED",
                    retryable=False,
                    duration_seconds=round(time.monotonic() - started, 2),
                    model=model,
                )
            if status != "SUCCEEDED":
                return self._incomplete_result(
                    task_id=task_id,
                    model=model,
                    started=started,
                    error=f"DashScope returned an unsupported task status: {status}.",
                    error_code="PROVIDER_RESPONSE_INVALID",
                )

            video_url = self._video_url(poll_result)
            if not video_url:
                return self._incomplete_result(
                    task_id=task_id,
                    model=model,
                    started=started,
                    error="DashScope completed without a downloadable video.",
                    error_code="PROVIDER_OUTPUT_MISSING",
                )

            download = requests.get(video_url, timeout=120)
            download.raise_for_status()
            output_path = self._output_path(inputs)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(download.content)
            usage = self._safe_usage(poll_result.get("usage"))
        except Exception as exc:
            error_code, retryable = self._classify_error(exc)
            if task_id:
                return self._incomplete_result(
                    task_id=task_id,
                    model=model,
                    started=started,
                    error=f"DashScope video task was interrupted: {self._safe_error(exc)}",
                    error_code=error_code,
                    retryable=retryable,
                )
            return ToolResult(
                success=False,
                data={"provider": self.provider, "charged": False},
                error=f"DashScope video request failed: {self._safe_error(exc)}",
                error_code=error_code,
                retryable=retryable,
                duration_seconds=round(time.monotonic() - started, 2),
                model=model,
            )

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model": model,
                "task_id": task_id,
                "external_task_id": task_id,
                "operation": str(inputs.get("operation") or "text_to_video"),
                "aspect_ratio": str(inputs.get("aspect_ratio") or "16:9"),
                "output": str(output_path),
                "output_path": str(output_path),
                "usage": usage,
                "charged": True,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.monotonic() - started, 2),
            model=model,
        )

    def _build_create_payload(self, inputs: dict[str, Any]) -> dict[str, Any]:
        operation = str(inputs.get("operation") or "text_to_video")
        if operation not in {"text_to_video", "image_to_video"}:
            raise ValueError(f"Unsupported DashScope video operation: {operation}")

        model = self._model_for(inputs)
        input_values: dict[str, Any] = {"prompt": str(inputs["prompt"])}
        if inputs.get("negative_prompt"):
            input_values["negative_prompt"] = str(inputs["negative_prompt"])

        parameters: dict[str, Any] = {
            "prompt_extend": bool(inputs.get("prompt_extend", True)),
            "watermark": bool(inputs.get("watermark", False)),
            "duration": self._normalize_duration(inputs.get("duration", 5)),
        }
        if inputs.get("seed") is not None:
            parameters["seed"] = int(inputs["seed"])

        if operation == "image_to_video":
            input_values["img_url"] = self._image_value(inputs)
            parameters["resolution"] = "720P"
            # Wan 2.6 flash can generate silent visual material for a platform
            # that performs its own final audio mix.
            parameters["audio"] = False
        else:
            parameters["size"] = _TEXT_SIZE_BY_RATIO.get(
                str(inputs.get("aspect_ratio") or "16:9"),
                _TEXT_SIZE_BY_RATIO["16:9"],
            )
            parameters["shot_type"] = str(inputs.get("shot_type") or "single")

        return {
            "model": model,
            "input": input_values,
            "parameters": parameters,
        }

    @staticmethod
    def _normalize_duration(value: Any) -> int:
        try:
            requested = float(value)
        except (TypeError, ValueError):
            requested = 5.0
        return max(2, min(15, int(round(requested))))

    @staticmethod
    def _model_for(inputs: dict[str, Any]) -> str:
        explicit = str(inputs.get("model_name") or inputs.get("model") or "").strip()
        if explicit:
            return explicit
        if str(inputs.get("operation") or "text_to_video") == "image_to_video":
            return os.environ.get("DASHSCOPE_IMAGE_TO_VIDEO_MODEL", _DEFAULT_IMAGE_MODEL)
        return os.environ.get(
            "DASHSCOPE_VIDEO_MODEL",
            _DEFAULT_TEXT_MODEL,
        )

    @classmethod
    def _image_value(cls, inputs: dict[str, Any]) -> str:
        remote = str(
            inputs.get("reference_image_url") or inputs.get("image_url") or ""
        ).strip()
        if remote:
            if not remote.startswith(("https://", "http://", "oss://", "data:image/")):
                raise ValueError("Unsupported DashScope reference image URL.")
            return remote

        raw_path = str(inputs.get("reference_image_path") or "").strip()
        if not raw_path:
            raise ValueError("image_to_video requires a reference image.")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("DashScope reference image does not exist.")

        workspace_root = str(inputs.get("workspace_root") or "").strip()
        if workspace_root:
            root = Path(workspace_root).expanduser().resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    "DashScope reference image is outside the workspace."
                ) from exc

        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("DashScope reference image exceeds 20 MB.")
        mime_type, _ = mimetypes.guess_type(path.name)
        if not mime_type or not mime_type.startswith("image/"):
            raise ValueError("Unsupported DashScope reference image format.")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _output_path(inputs: dict[str, Any]) -> Path:
        path = Path(str(inputs.get("output_path") or "dashscope_video.mp4"))
        if path.suffix.lower() != ".mp4":
            path = path.with_suffix(".mp4")
        return path

    @staticmethod
    def _task_id(payload: dict[str, Any]) -> str | None:
        output = payload.get("output")
        if not isinstance(output, dict):
            return None
        value = output.get("task_id")
        return str(value) if value else None

    @staticmethod
    def _task_status(payload: dict[str, Any]) -> str:
        output = payload.get("output")
        if not isinstance(output, dict):
            return "UNKNOWN"
        return str(output.get("task_status") or "UNKNOWN").upper()

    @staticmethod
    def _video_url(payload: dict[str, Any]) -> str | None:
        output = payload.get("output")
        if not isinstance(output, dict):
            return None
        value = output.get("video_url")
        if not value and isinstance(output.get("results"), list):
            for item in output["results"]:
                if isinstance(item, dict) and item.get("url"):
                    value = item["url"]
                    break
        return str(value) if value else None

    @classmethod
    def _poll_task(
        cls,
        *,
        requests: Any,
        base_url: str,
        headers: dict[str, str],
        task_id: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            response = requests.get(
                f"{base_url}/tasks/{task_id}",
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            status = cls._task_status(payload)
            if status not in _PENDING_STATES:
                return payload
            if time.monotonic() >= deadline:
                return payload
            if poll_interval_seconds > 0:
                time.sleep(poll_interval_seconds)

    def _incomplete_result(
        self,
        *,
        task_id: str,
        model: str,
        started: float,
        error: str,
        error_code: str,
        retryable: bool = True,
    ) -> ToolResult:
        return ToolResult(
            success=False,
            data={
                "provider": self.provider,
                "task_id": task_id,
                "external_task_id": task_id,
                "charged": None,
            },
            error=self._safe_error(error),
            error_code=error_code,
            retryable=retryable,
            duration_seconds=round(time.monotonic() - started, 2),
            model=model,
        )

    @staticmethod
    def _safe_usage(value: Any) -> dict[str, int | float]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): item
            for key, item in value.items()
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        }

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[str, bool]:
        import requests

        if isinstance(exc, requests.Timeout):
            return "PROVIDER_TIMEOUT", True
        if isinstance(exc, requests.ConnectionError):
            return "PROVIDER_CONNECTION_ERROR", True
        if isinstance(exc, requests.HTTPError):
            status = getattr(exc.response, "status_code", None)
            if status == 429:
                return "PROVIDER_RATE_LIMITED", True
            if status in {408, 425} or (
                isinstance(status, int) and status >= 500
            ):
                return "PROVIDER_TRANSIENT_ERROR", True
            return "PROVIDER_REQUEST_REJECTED", False
        return "PROVIDER_EXECUTION_ERROR", False

    @staticmethod
    def _safe_error(value: Any) -> str:
        text = str(value)
        key = os.environ.get("DASHSCOPE_API_KEY")
        if key:
            text = text.replace(key, "[redacted]")
        return _URL_RE.sub("[redacted-url]", text)[:500]
