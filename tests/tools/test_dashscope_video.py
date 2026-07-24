"""DashScope Wan cloud-video provider behavior without paid API calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from tools.base_tool import ToolStatus
from tools.video.dashscope_video import DashscopeVideo


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self.payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.text = json.dumps(self.payload)

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} provider request rejected",
                response=self,
            )


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> DashscopeVideo:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-secret-for-test")
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    return DashscopeVideo()


def test_contract_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    tool = DashscopeVideo()

    assert tool.provider == "dashscope"
    assert tool.capability == "video_generation"
    assert tool.get_status() == ToolStatus.UNAVAILABLE
    assert tool.supports["text_to_video"] is True
    assert tool.supports["image_to_video"] is True

    monkeypatch.setenv("DASHSCOPE_API_KEY", "configured")
    assert tool.get_status() == ToolStatus.AVAILABLE


@pytest.mark.parametrize(
    ("aspect_ratio", "expected"),
    [
        ("16:9", "1280*720"),
        ("9:16", "720*1280"),
        ("1:1", "960*960"),
    ],
)
def test_text_to_video_payload_uses_official_720p_sizes(
    configured: DashscopeVideo,
    aspect_ratio: str,
    expected: str,
) -> None:
    payload = configured._build_create_payload(
        {
            "operation": "text_to_video",
            "prompt": "产品从数据流中出现",
            "aspect_ratio": aspect_ratio,
            "duration": "5",
        }
    )

    assert payload["model"] == "wan2.6-t2v"
    assert payload["input"]["prompt"] == "产品从数据流中出现"
    assert payload["parameters"]["size"] == expected
    assert payload["parameters"]["duration"] == 5
    assert payload["parameters"]["shot_type"] == "single"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, 2), (2, 2), (5.2, 5), (14.8, 15), (40, 15)],
)
def test_duration_is_normalized_to_wan_range(
    configured: DashscopeVideo,
    requested: float,
    expected: int,
) -> None:
    payload = configured._build_create_payload(
        {"prompt": "test", "duration": requested}
    )
    assert payload["parameters"]["duration"] == expected


def test_image_to_video_encodes_local_image_as_data_url(
    configured: DashscopeVideo,
    tmp_path: Path,
) -> None:
    image = tmp_path / "frame.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nprivate-image")

    payload = configured._build_create_payload(
        {
            "operation": "image_to_video",
            "prompt": "界面元素平滑展开",
            "reference_image_path": str(image),
            "duration": 4,
        }
    )

    assert payload["model"] == "wan2.6-i2v-flash"
    assert payload["input"]["img_url"].startswith("data:image/png;base64,")
    assert payload["parameters"]["resolution"] == "720P"
    assert "size" not in payload["parameters"]


def test_create_poll_download_returns_only_safe_fields(
    configured: DashscopeVideo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed_url = "https://provider.invalid/result.mp4?token=signed-secret"
    calls: dict[str, list[Any]] = {"post": [], "get": []}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls["post"].append((url, kwargs))
        return FakeResponse(
            {"output": {"task_id": "wan-task-123", "task_status": "PENDING"}}
        )

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls["get"].append((url, kwargs))
        if "/tasks/" in url:
            return FakeResponse(
                {
                    "output": {
                        "task_id": "wan-task-123",
                        "task_status": "SUCCEEDED",
                        "video_url": signed_url,
                    },
                    "usage": {"video_duration": 5},
                }
            )
        assert url == signed_url
        return FakeResponse(content=b"real-mp4-payload")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    output = tmp_path / "scene.mp4"

    result = configured.execute(
        {
            "prompt": "镜头穿过产品数据流",
            "duration": 5,
            "output_path": str(output),
            "poll_interval_seconds": 0,
        }
    )

    assert result.success is True
    assert output.read_bytes() == b"real-mp4-payload"
    assert result.data["provider"] == "dashscope"
    assert result.data["task_id"] == "wan-task-123"
    assert result.data["external_task_id"] == "wan-task-123"
    assert result.data["output_path"] == str(output)
    serialized = json.dumps(result.data)
    assert "signed-secret" not in serialized
    assert "dashscope-secret-for-test" not in serialized
    assert len(calls["post"]) == 1


def test_pending_then_success_polls_same_task(
    configured: DashscopeVideo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poll_statuses = iter(
        [
            FakeResponse(
                {"output": {"task_id": "wan-task-2", "task_status": "RUNNING"}}
            ),
            FakeResponse(
                {
                    "output": {
                        "task_id": "wan-task-2",
                        "task_status": "SUCCEEDED",
                        "video_url": "https://provider.invalid/final.mp4",
                    }
                }
            ),
        ]
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            {"output": {"task_id": "wan-task-2", "task_status": "PENDING"}}
        ),
    )

    def fake_get(url: str, **_kwargs: Any) -> FakeResponse:
        if "/tasks/" in url:
            return next(poll_statuses)
        return FakeResponse(content=b"mp4")

    monkeypatch.setattr(requests, "get", fake_get)
    result = configured.execute(
        {
            "prompt": "test",
            "output_path": str(tmp_path / "result.mp4"),
            "poll_interval_seconds": 0,
        }
    )
    assert result.success is True
    assert result.data["task_id"] == "wan-task-2"


def test_resume_from_provider_task_id_never_creates_again(
    configured: DashscopeVideo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_create(*_args: Any, **_kwargs: Any) -> FakeResponse:
        raise AssertionError("resume must not create a second paid task")

    monkeypatch.setattr(requests, "post", reject_create)

    def fake_get(url: str, **_kwargs: Any) -> FakeResponse:
        if "/tasks/" in url:
            assert url.endswith("/tasks/already-created")
            return FakeResponse(
                {
                    "output": {
                        "task_id": "already-created",
                        "task_status": "SUCCEEDED",
                        "video_url": "https://provider.invalid/resumed.mp4",
                    }
                }
            )
        return FakeResponse(content=b"resumed-mp4")

    monkeypatch.setattr(requests, "get", fake_get)
    result = configured.execute(
        {
            "prompt": "test",
            "provider_task_id": "already-created",
            "output_path": str(tmp_path / "resumed.mp4"),
            "poll_interval_seconds": 0,
        }
    )

    assert result.success is True
    assert result.data["task_id"] == "already-created"


def test_timeout_after_task_creation_preserves_safe_task_id(
    configured: DashscopeVideo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            {"output": {"task_id": "task-needs-resume", "task_status": "PENDING"}}
        ),
    )
    monkeypatch.setattr(
        requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(
            {"output": {"task_id": "task-needs-resume", "task_status": "RUNNING"}}
        ),
    )

    result = configured.execute(
        {
            "prompt": "test",
            "output_path": str(tmp_path / "pending.mp4"),
            "poll_interval_seconds": 0,
            "timeout_seconds": 0,
        }
    )

    assert result.success is False
    assert result.retryable is True
    assert result.error_code == "PROVIDER_TASK_PENDING"
    assert result.data["task_id"] == "task-needs-resume"
    assert result.data["charged"] is None


def test_provider_rejection_before_task_id_is_known_no_charge(
    configured: DashscopeVideo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            {"code": "InvalidParameter", "message": "bad request"},
            status_code=400,
        ),
    )

    result = configured.execute(
        {
            "prompt": "test",
            "output_path": str(tmp_path / "rejected.mp4"),
        }
    )

    assert result.success is False
    assert result.data["charged"] is False
    assert result.data.get("task_id") is None
    assert result.retryable is False


def test_api_key_and_signed_url_are_redacted_from_errors(
    configured: DashscopeVideo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "dashscope-secret-for-test "
                "https://provider.invalid/video.mp4?token=private"
            )
        ),
    )

    result = configured.execute(
        {
            "prompt": "test",
            "output_path": str(tmp_path / "failed.mp4"),
        }
    )
    serialized = json.dumps(result.data) + str(result.error)
    assert "dashscope-secret-for-test" not in serialized
    assert "token=private" not in serialized


def test_cost_scales_with_duration(configured: DashscopeVideo) -> None:
    short = configured.estimate_cost({"duration": 2, "aspect_ratio": "16:9"})
    long = configured.estimate_cost({"duration": 10, "aspect_ratio": "16:9"})
    assert isinstance(short, float)
    assert short > 0
    assert long > short
