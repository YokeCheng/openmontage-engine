from pathlib import Path

from tools.base_tool import ToolResult
from tools.video.video_compose import VideoCompose


def test_animation_descriptor_asset_is_not_forwarded_as_image_source(tmp_path, monkeypatch):
    descriptor = tmp_path / "scene-1.json"
    descriptor.write_text('{"kind":"motion-graphics-descriptor"}', encoding="utf-8")
    captured = {}

    def fake_render(self, inputs):
        captured["inputs"] = inputs
        return ToolResult(success=True, data={"path": inputs["output_path"]})

    monkeypatch.setattr(VideoCompose, "_remotion_available", lambda self: True)
    monkeypatch.setattr(VideoCompose, "_remotion_render", fake_render)
    monkeypatch.setattr(VideoCompose, "_run_final_review", lambda *args, **kwargs: {"status": "pass", "issues_found": []})

    result = VideoCompose().execute(
        {
            "operation": "render",
            "output_path": str(tmp_path / "out.mp4"),
            "asset_manifest": {
                "assets": [
                    {
                        "id": "motion-scene-1",
                        "type": "animation",
                        "path": str(descriptor),
                    }
                ]
            },
            "edit_decisions": {
                "render_runtime": "remotion",
                "renderer_family": "explainer-data",
                "cuts": [
                    {
                        "id": "cut-1",
                        "source": "motion-scene-1",
                        "in_seconds": 0,
                        "out_seconds": 3,
                        "reason": "Render as a generated motion text card.",
                        "transform": {"animation": "subtle-pop-in"},
                    }
                ],
            },
        }
    )

    assert result.success
    cut = captured["inputs"]["edit_decisions"]["cuts"][0]
    assert cut["source"] == ""
    assert cut["type"] == "text_card"
    assert cut["text"] == "Render as a generated motion text card."
    assert cut["metadata"]["non_renderable_source_asset"] == "motion-scene-1"


def test_remotion_audio_assets_are_staged_from_repo_music_library(tmp_path, monkeypatch):
    tool = VideoCompose()
    repo_root = Path(__file__).resolve().parents[2]
    music_dir = repo_root / "music_library"
    music_dir.mkdir(exist_ok=True)
    music_path = music_dir / "unit-test-bed.mp3"
    music_path.write_bytes(b"fake mp3 bytes")
    captured = {}

    def fake_run_command(self, cmd, timeout=None, cwd=None):
        props_arg = next(item for item in cmd if item.startswith("--props="))
        captured["props_path"] = props_arg.split("=", 1)[1]
        output_path = cmd[5]
        open(output_path, "wb").write(b"fake mp4")

    monkeypatch.setattr(VideoCompose, "run_command", fake_run_command)
    monkeypatch.setattr(VideoCompose, "_run_final_review", lambda *args, **kwargs: {"status": "pass", "issues_found": []})

    result = tool._remotion_render(
        {
            "output_path": str(tmp_path / "renders" / "out.mp4"),
            "edit_decisions": {
                "renderer_family": "explainer-data",
                "cuts": [
                    {
                        "id": "cut-1",
                        "type": "text_card",
                        "text": "hello",
                        "in_seconds": 0,
                        "out_seconds": 1,
                    }
                ],
                "audio": {"music": {"src": "music_library/unit-test-bed.mp3"}},
            },
        }
    )

    assert result.success
    # _remotion_render removes the temporary props file after command
    # execution; assert via the staged public file instead.
    public_files = list((repo_root / "remotion-composer" / "public" / "councilforge-runtime").rglob("*unit-test-bed.mp3"))
    assert public_files


def test_asset_manifest_music_is_injected_into_remotion_edit_decisions():
    merged = VideoCompose._with_manifest_audio(
        {"cuts": []},
        {
            "assets": [
                {
                    "id": "music-bg",
                    "type": "audio",
                    "subtype": "music",
                    "path": "music_library/councilforge-local-ambient-bed.mp3",
                }
            ]
        },
    )

    assert merged["audio"]["music"]["src"] == "music_library/councilforge-local-ambient-bed.mp3"
    assert merged["audio"]["music"]["loop"] is True
    assert merged["audio"]["music"]["volume"] == 0.85


def test_asset_manifest_music_completes_existing_asset_id_audio_layer():
    merged = VideoCompose._with_manifest_audio(
        {
            "cuts": [],
            "audio": {
                "music": {
                    "asset_id": "music-bg",
                    "volume": 0.1,
                    "fade_in_seconds": 1.2,
                }
            },
        },
        {
            "assets": [
                {
                    "id": "music-bg",
                    "type": "audio",
                    "subtype": "music",
                    "path": "music_library/councilforge-local-ambient-bed.mp3",
                }
            ]
        },
    )

    assert merged["audio"]["music"]["asset_id"] == "music-bg"
    assert merged["audio"]["music"]["src"] == "music_library/councilforge-local-ambient-bed.mp3"
    assert merged["audio"]["music"]["loop"] is True
    assert merged["audio"]["music"]["volume"] == 0.85
