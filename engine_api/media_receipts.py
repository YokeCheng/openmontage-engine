"""Safe, idempotent execution receipts for billable media providers.

Receipts deliberately persist an allowlisted subset of provider results. They
are production recovery records, not raw provider response logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine_api.contract import canonical_json_bytes, sha256_file
from engine_api.models import VideoShotRequest
from tools.base_tool import ToolResult
from tools.video._shared import probe_output


class MediaReceiptConflict(RuntimeError):
    """The same idempotency key was reused for a different request."""


class MediaChargeReconciliationRequired(RuntimeError):
    """Automatic retry is unsafe because provider charging is uncertain."""


class MediaProviderFailed(RuntimeError):
    """The provider confirmed that a failed call incurred no charge."""


class MediaBudgetExceeded(RuntimeError):
    """The provider estimate exceeds the request's hard cost ceiling."""


class MediaReceipt(BaseModel):
    """Allowlisted durable facts for exactly one provider operation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: str
    request_sha256: str
    status: Literal[
        "submitted",
        "succeeded",
        "failed_no_charge",
        "charge_unknown",
    ]
    provider: str
    model: str | None = None
    external_task_id: str | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    output_path: str | None = None
    sha256: str | None = None
    media: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None


def canonical_request_sha256(request: VideoShotRequest) -> str:
    return hashlib.sha256(
        canonical_json_bytes(request.model_dump(mode="json"))
    ).hexdigest()


def receipt_path(root: Path, idempotency_key: str) -> Path:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return Path(root) / f"{digest}.json"


def save_receipt(root: Path, receipt: MediaReceipt) -> None:
    target = receipt_path(root, receipt.idempotency_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        receipt.model_dump_json(indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def load_receipt(root: Path, idempotency_key: str) -> MediaReceipt | None:
    target = receipt_path(root, idempotency_key)
    if not target.is_file():
        return None
    return MediaReceipt.model_validate_json(target.read_text(encoding="utf-8"))


def _provider_inputs(request: VideoShotRequest) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "scene_id": request.scene_id,
        "operation": request.operation,
        "prompt": request.prompt,
        "negative_prompt": request.negative_prompt,
        "duration": str(request.duration_seconds),
        "aspect_ratio": request.aspect_ratio,
        "preferred_provider": request.provider,
        "external_task_id": request.idempotency_key,
    }
    if request.reference_image_path:
        inputs["reference_image_path"] = request.reference_image_path
    if request.reference_asset_ids:
        inputs["reference_asset_ids"] = list(request.reference_asset_ids)
    if request.model:
        inputs["model_name"] = request.model
    if request.output_path:
        inputs["output_path"] = request.output_path
    if request.provider != "auto":
        inputs["allowed_providers"] = [request.provider]
    return inputs


def _cached_result(receipt: MediaReceipt) -> ToolResult:
    assert receipt.output_path is not None
    return ToolResult(
        success=True,
        data={
            "selected_provider": receipt.provider,
            "provider": receipt.provider,
            "task_id": receipt.external_task_id,
            "external_task_id": receipt.external_task_id,
            "output": receipt.output_path,
            "output_path": receipt.output_path,
            "sha256": receipt.sha256,
            "media": dict(receipt.media),
            "receipt_reused": True,
            "provider_call": False,
            "original_cost_usd": receipt.cost_usd,
        },
        artifacts=[receipt.output_path],
        cost_usd=0.0,
        duration_seconds=(receipt.elapsed_ms or 0) / 1000,
        model=receipt.model,
    )


def _require_safe_existing(
    root: Path,
    request: VideoShotRequest,
    request_sha256: str,
) -> ToolResult | None:
    receipt = load_receipt(root, request.idempotency_key)
    if receipt is None:
        return None
    if receipt.request_sha256 != request_sha256:
        raise MediaReceiptConflict("IDEMPOTENCY_CONFLICT")
    if receipt.status in {"submitted", "charge_unknown"}:
        raise MediaChargeReconciliationRequired(
            receipt.error_code or "PROVIDER_CHARGE_RECONCILIATION_REQUIRED"
        )
    if receipt.status == "failed_no_charge":
        return None
    if not receipt.output_path or not receipt.sha256:
        raise MediaChargeReconciliationRequired("RECEIPT_OUTPUT_INCOMPLETE")
    output = Path(receipt.output_path)
    if not output.is_file() or sha256_file(output) != receipt.sha256:
        raise MediaChargeReconciliationRequired("RECEIPT_OUTPUT_MISSING_OR_CHANGED")
    return _cached_result(receipt)


def execute_video_shot(
    request: VideoShotRequest,
    receipt_root: Path,
    selector: Any,
) -> ToolResult:
    """Execute one AI video shot at most once unless no charge is confirmed."""

    request_sha256 = canonical_request_sha256(request)
    cached = _require_safe_existing(receipt_root, request, request_sha256)
    if cached is not None:
        return cached

    inputs = _provider_inputs(request)
    estimated_cost = float(selector.estimate_cost(inputs) or 0)
    if estimated_cost > request.maximum_cost_usd:
        raise MediaBudgetExceeded("VIDEO_SHOT_BUDGET_EXCEEDED")

    started = time.monotonic()
    submitted = MediaReceipt(
        idempotency_key=request.idempotency_key,
        request_sha256=request_sha256,
        status="submitted",
        provider=request.provider,
        model=request.model,
    )
    save_receipt(receipt_root, submitted)

    try:
        result = selector.execute(inputs)
    except Exception as exc:
        save_receipt(
            receipt_root,
            submitted.model_copy(
                update={
                    "status": "charge_unknown",
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "error_code": "PROVIDER_EXECUTION_INTERRUPTED",
                }
            ),
        )
        raise MediaChargeReconciliationRequired(
            "PROVIDER_CHARGE_RECONCILIATION_REQUIRED"
        ) from exc

    elapsed_ms = round((time.monotonic() - started) * 1000)
    data = result.data if isinstance(result.data, dict) else {}
    selected_provider = str(
        data.get("selected_provider") or data.get("provider") or request.provider
    )
    external_task_id = data.get("external_task_id") or data.get("task_id")
    common = {
        "provider": selected_provider,
        "model": result.model or request.model,
        "external_task_id": str(external_task_id) if external_task_id else None,
        "cost_usd": float(result.cost_usd or 0),
        "elapsed_ms": elapsed_ms,
    }

    if request.provider != "auto" and selected_provider != request.provider:
        save_receipt(
            receipt_root,
            submitted.model_copy(
                update={
                    **common,
                    "status": "charge_unknown",
                    "error_code": "PROVIDER_CONSTRAINT_VIOLATION",
                }
            ),
        )
        raise MediaChargeReconciliationRequired(
            "PROVIDER_CONSTRAINT_VIOLATION"
        )

    if not result.success:
        if data.get("charged") is False:
            save_receipt(
                receipt_root,
                submitted.model_copy(
                    update={
                        **common,
                        "status": "failed_no_charge",
                        "error_code": result.error_code or "PROVIDER_FAILED_NO_CHARGE",
                    }
                ),
            )
            raise MediaProviderFailed(result.error or "MEDIA_PROVIDER_FAILED")
        save_receipt(
            receipt_root,
            submitted.model_copy(
                update={
                    **common,
                    "status": "charge_unknown",
                    "error_code": result.error_code
                    or "PROVIDER_CHARGE_RECONCILIATION_REQUIRED",
                }
            ),
        )
        raise MediaChargeReconciliationRequired(
            "PROVIDER_CHARGE_RECONCILIATION_REQUIRED"
        )

    output_value = data.get("output_path") or data.get("output")
    if not output_value and result.artifacts:
        output_value = result.artifacts[0]
    output = Path(str(output_value)) if output_value else None
    if output is None or not output.is_file():
        save_receipt(
            receipt_root,
            submitted.model_copy(
                update={
                    **common,
                    "status": "charge_unknown",
                    "error_code": "PROVIDER_OUTPUT_MISSING",
                }
            ),
        )
        raise MediaChargeReconciliationRequired("PROVIDER_OUTPUT_MISSING")

    receipt = submitted.model_copy(
        update={
            **common,
            "status": "succeeded",
            "output_path": str(output),
            "sha256": sha256_file(output),
            "media": probe_output(output),
            "error_code": None,
        }
    )
    save_receipt(receipt_root, receipt)
    result.data["sha256"] = receipt.sha256
    result.data["media"] = dict(receipt.media)
    result.data["external_task_id"] = receipt.external_task_id
    result.data["provider_call"] = True
    return result
