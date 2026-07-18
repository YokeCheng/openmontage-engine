from __future__ import annotations

from typing import Any

from tools.audio.tts_selector import TTSSelector
from tools.base_tool import ToolResult, ToolStatus
from tools.graphics.image_selector import ImageSelector


class _Provider:
    def __init__(self, *, provider: str, properties: dict[str, Any]) -> None:
        self.name = f"{provider}_tool"
        self.provider = provider
        self.input_schema = {"properties": properties}
        self.supports: dict[str, Any] = {}
        self.seen: dict[str, Any] | None = None

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def get_info(self) -> dict[str, Any]:
        return {
            "agent_skills": [],
            "usage_location": "test",
            "best_for": [],
        }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        self.seen = inputs
        return ToolResult(success=True)


def test_image_selector_maps_dashscope_widescreen_size(monkeypatch) -> None:
    provider = _Provider(
        provider="dashscope",
        properties={
            "prompt": {},
            "model": {},
            "size": {},
            "output_path": {},
        },
    )
    selector = ImageSelector()
    monkeypatch.setattr(selector, "_providers", lambda: [provider])
    monkeypatch.setattr(
        selector,
        "_select_best_tool",
        lambda _inputs, _candidates, _context: (provider, None),
    )

    result = selector.execute(
        {
            "prompt": "中文知识解说主视觉",
            "preferred_provider": "dashscope",
            "aspect_ratio": "16:9",
            "model": "qwen-image-2.0-pro",
            "output_path": "scene.png",
        }
    )

    assert result.success is True
    assert provider.seen is not None
    assert provider.seen["size"] == "2688*1536"
    assert "preferred_provider" not in provider.seen
    assert "aspect_ratio" not in provider.seen


def test_tts_selector_maps_chinese_voice_and_strips_selector_fields(monkeypatch) -> None:
    provider = _Provider(
        provider="dashscope",
        properties={
            "text": {},
            "voice": {},
            "language_type": {},
            "model": {},
            "instructions": {},
            "output_path": {},
        },
    )
    selector = TTSSelector()
    monkeypatch.setattr(selector, "_providers", lambda: [provider])
    monkeypatch.setattr(
        selector,
        "_select_best_tool",
        lambda _inputs, _candidates, _context: (provider, None),
    )

    result = selector.execute(
        {
            "text": "这是中文配音。",
            "voice_id": "Cherry",
            "voice_language": "zh",
            "preferred_provider": "dashscope",
            "instructions": "自然、清晰地讲解",
            "output_path": "narration.wav",
        }
    )

    assert result.success is True
    assert provider.seen is not None
    assert provider.seen["voice"] == "Cherry"
    assert provider.seen["language_type"] == "Chinese"
    assert "voice_id" not in provider.seen
    assert "preferred_provider" not in provider.seen
