"""Safe, idempotent receipts for billable AI video generation."""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from engine_api.models import VideoShotRequest
from tools.base_tool import ToolResult


class FakeVideoSelector:
    def __init__(
        self,
        output_path: Path,
        *,
        provider: str = "kling",
        success: bool = True,
        charged: bool | None = None,
    ) -> None:
        self.output_path = output_path
        self.provider = provider
        self.success = success
        self.charged = charged
        self.calls = 0
        self.inputs: list[dict[str, Any]] = []

    def estimate_cost(self, _inputs: dict[str, Any]) -> float:
        return 0.4

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        self.calls += 1
        self.inputs.append(dict(inputs))
        if not self.success:
            data: dict[str, Any] = {
                "provider": self.provider,
                "task_id": "provider-task-failed",
            }
            if self.charged is not None:
                data["charged"] = self.charged
            return ToolResult(
                success=False,
                data=data,
                error="provider request failed",
                retryable=True,
            )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(b"safe-video-payload")
        return ToolResult(
            success=True,
            data={
                "selected_tool": "kling_official_video",
                "selected_provider": self.provider,
                "provider": self.provider,
                "task_id": "provider-task-123",
                "output": str(self.output_path),
                "output_path": str(self.output_path),
                "remote_url": "https://provider.invalid/video.mp4?token=secret-token",
                "api_key": "must-not-persist",
            },
            artifacts=[str(self.output_path)],
            cost_usd=0.37,
            duration_seconds=12.5,
            model="kling-v2",
        )


def request(**changes: Any) -> VideoShotRequest:
    values: dict[str, Any] = {
        "scene_id": "scene-01",
        "operation": "text_to_video",
        "prompt": "A precise product benefit animates into view",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "provider": "kling",
        "idempotency_key": "job-1:final:scene-01",
        "maximum_cost_usd": 0.8,
    }
    values.update(changes)
    return VideoShotRequest.model_validate(values)


def _receipts():
    assert importlib.util.find_spec("engine_api.media_receipts") is not None
    return importlib.import_module("engine_api.media_receipts")


def test_completed_receipt_prevents_a_second_provider_call(tmp_path: Path) -> None:
    receipts = _receipts()
    selector = FakeVideoSelector(tmp_path / "assets" / "scene-01.mp4")

    first = receipts.execute_video_shot(request(), tmp_path / "receipts", selector)
    second = receipts.execute_video_shot(request(), tmp_path / "receipts", selector)
    saved = receipts.load_receipt(tmp_path / "receipts", request().idempotency_key)

    assert selector.calls == 1
    assert first.artifacts == second.artifacts
    assert saved.status == "succeeded"
    assert saved.external_task_id == "provider-task-123"
    assert saved.cost_usd == 0.37
    assert len(saved.sha256 or "") == 64
    assert saved.media["file_size_bytes"] == len(b"safe-video-payload")
    persisted = next((tmp_path / "receipts").glob("*.json")).read_text(encoding="utf-8")
    assert "secret-token" not in persisted
    assert "must-not-persist" not in persisted


def test_explicit_provider_is_a_hard_constraint(tmp_path: Path) -> None:
    receipts = _receipts()
    selector = FakeVideoSelector(
        tmp_path / "assets" / "scene-01.mp4",
        provider="veo",
    )

    with pytest.raises(
        receipts.MediaChargeReconciliationRequired,
        match="PROVIDER_CONSTRAINT_VIOLATION",
    ):
        receipts.execute_video_shot(request(provider="kling"), tmp_path / "receipts", selector)

    assert selector.inputs[0]["allowed_providers"] == ["kling"]
    saved = receipts.load_receipt(tmp_path / "receipts", request().idempotency_key)
    assert saved.status == "charge_unknown"
    with pytest.raises(receipts.MediaChargeReconciliationRequired):
        receipts.execute_video_shot(request(provider="kling"), tmp_path / "receipts", selector)
    assert selector.calls == 1


def test_idempotency_key_rejects_a_different_request(tmp_path: Path) -> None:
    receipts = _receipts()
    selector = FakeVideoSelector(tmp_path / "assets" / "scene-01.mp4")
    receipts.execute_video_shot(request(), tmp_path / "receipts", selector)

    with pytest.raises(receipts.MediaReceiptConflict, match="IDEMPOTENCY_CONFLICT"):
        receipts.execute_video_shot(
            request(prompt="A materially different prompt"),
            tmp_path / "receipts",
            selector,
        )

    assert selector.calls == 1


def test_known_no_charge_failure_can_retry_same_request(tmp_path: Path) -> None:
    receipts = _receipts()
    selector = FakeVideoSelector(
        tmp_path / "assets" / "scene-01.mp4",
        success=False,
        charged=False,
    )

    with pytest.raises(receipts.MediaProviderFailed, match="provider request failed"):
        receipts.execute_video_shot(request(), tmp_path / "receipts", selector)
    assert receipts.load_receipt(
        tmp_path / "receipts",
        request().idempotency_key,
    ).status == "failed_no_charge"

    selector.success = True
    result = receipts.execute_video_shot(request(), tmp_path / "receipts", selector)
    assert result.success is True
    assert selector.calls == 2


def test_ambiguous_failure_stops_automatic_retry(tmp_path: Path) -> None:
    receipts = _receipts()
    selector = FakeVideoSelector(
        tmp_path / "assets" / "scene-01.mp4",
        success=False,
        charged=None,
    )

    with pytest.raises(receipts.MediaChargeReconciliationRequired):
        receipts.execute_video_shot(request(), tmp_path / "receipts", selector)
    with pytest.raises(receipts.MediaChargeReconciliationRequired):
        receipts.execute_video_shot(request(), tmp_path / "receipts", selector)

    assert selector.calls == 1
    saved_path = next((tmp_path / "receipts").glob("*.json"))
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    assert saved["status"] == "charge_unknown"
