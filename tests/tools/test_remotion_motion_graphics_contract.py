from tools.base_tool import ToolStatus
from tools.video.remotion_motion_graphics import RemotionMotionGraphics


def test_remotion_bundle_has_no_runtime_google_font_dependency() -> None:
    composer = RemotionMotionGraphics()._repo_root / "remotion-composer" / "src"
    offenders = [
        path
        for path in composer.rglob("*.tsx")
        if "@remotion/google-fonts" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def _inputs() -> dict:
    return {
        "operation": "prepare",
        "title": "Test",
        "objective": "Verify caption contract",
        "subtitles": True,
        "scenes": [
            {
                "scene_id": "scene-1",
                "title": "Hook",
                "narration": "第一段字幕",
                "description": "开场标题与断裂连线动画",
                "start_seconds": 0,
                "end_seconds": 5,
            },
            {
                "scene_id": "scene-2",
                "title": "Setup",
                "narration": "第二段字幕",
                "description": "CouncilForge 决策中枢图",
                "start_seconds": 5,
                "end_seconds": 11,
            },
        ],
        "render": {"width": 1920, "height": 1080, "fps": 30, "duration_seconds": 11},
    }


def test_scene_duration_is_derived_from_approved_timeline() -> None:
    props = RemotionMotionGraphics()._props(_inputs())
    assert [scene["duration_seconds"] for scene in props["scenes"]] == [5.0, 6.0]


def test_asset_manifest_media_is_added_to_props(tmp_path) -> None:
    image = tmp_path / "scene.png"
    narration = tmp_path / "narration.wav"
    music = tmp_path / "music.wav"
    image.write_bytes(b"image")
    narration.write_bytes(b"narration")
    music.write_bytes(b"music")
    inputs = _inputs()
    inputs["asset_manifest"] = {
        "version": "1.0",
        "assets": [
            {"id": "img-1", "type": "image", "scene_id": "scene-1", "path": str(image)},
            {"id": "voice-1", "type": "audio", "subtype": "narration", "scene_id": "scene-1", "path": str(narration)},
            {"id": "music", "type": "audio", "subtype": "music", "path": str(music)},
        ],
    }
    props = RemotionMotionGraphics()._props(inputs)
    assert props["scenes"][0]["visual"]["image_src"] == str(image)
    assert props["scenes"][0]["audio_src"] == str(narration)
    assert props["audio"]["music"]["src"] == str(music)


def test_manifest_accepts_narration_and_music_as_asset_types(tmp_path) -> None:
    narration = tmp_path / "narration.mp3"
    music = tmp_path / "music.mp3"
    narration.write_bytes(b"narration")
    music.write_bytes(b"music")
    inputs = _inputs()
    inputs["asset_manifest"] = {
        "version": "1.0",
        "assets": [
            {"id": "voice-1", "type": "narration", "scene_id": "scene-1", "path": str(narration)},
            {"id": "music", "type": "music", "path": str(music)},
        ],
    }
    props = RemotionMotionGraphics()._props(inputs)
    assert props["scenes"][0]["audio_src"] == str(narration)
    assert props["audio"]["music"]["src"] == str(music)


def test_local_media_is_staged_under_remotion_public(tmp_path) -> None:
    image = tmp_path / "scene.png"
    narration = tmp_path / "narration.wav"
    music = tmp_path / "music.wav"
    image.write_bytes(b"image")
    narration.write_bytes(b"narration")
    music.write_bytes(b"music")
    inputs = _inputs()
    inputs["asset_manifest"] = {
        "version": "1.0",
        "assets": [
            {"id": "img-1", "type": "image", "scene_id": "scene-1", "path": str(image)},
            {"id": "voice-1", "type": "audio", "subtype": "narration", "scene_id": "scene-1", "path": str(narration)},
            {"id": "music", "type": "audio", "subtype": "music", "path": str(music)},
        ],
    }
    staged = RemotionMotionGraphics()._stage_props_public_assets(
        RemotionMotionGraphics()._props(inputs),
        tmp_path / "renders" / "final.mp4",
    )
    assert staged["scenes"][0]["visual"]["image_src"].startswith("councilforge-runtime/")
    assert staged["scenes"][0]["audio_src"].startswith("councilforge-runtime/")
    assert staged["audio"]["music"]["src"].startswith("councilforge-runtime/")


def test_captioned_render_rejects_blank_narration(monkeypatch) -> None:
    tool = RemotionMotionGraphics()
    monkeypatch.setattr(tool, "get_status", lambda: ToolStatus.AVAILABLE)
    inputs = _inputs()
    inputs["scenes"][1]["narration"] = ""
    result = tool.execute(inputs)
    assert result.success is False
    assert "narration is missing" in str(result.error)


def test_render_rejects_timeline_duration_drift(monkeypatch) -> None:
    tool = RemotionMotionGraphics()
    monkeypatch.setattr(tool, "get_status", lambda: ToolStatus.AVAILABLE)
    inputs = _inputs()
    inputs["render"]["duration_seconds"] = 30
    result = tool.execute(inputs)
    assert result.success is False
    assert "does not match render duration" in str(result.error)


def test_render_rejects_blank_visual_description(monkeypatch) -> None:
    tool = RemotionMotionGraphics()
    monkeypatch.setattr(tool, "get_status", lambda: ToolStatus.AVAILABLE)
    inputs = _inputs()
    inputs["scenes"][0]["description"] = ""
    result = tool.execute(inputs)
    assert result.success is False
    assert "visual description is missing" in str(result.error)
