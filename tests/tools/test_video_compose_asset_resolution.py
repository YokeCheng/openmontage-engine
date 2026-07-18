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
    monkeypatch.setattr(
        VideoCompose,
        "_run_final_review",
        lambda *args, **kwargs: {"status": "pass", "issues_found": []},
    )

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
        props_index = cmd.index(props_arg)
        output_path = cmd[props_index - 1]
        open(output_path, "wb").write(b"fake mp4")

    monkeypatch.setattr(VideoCompose, "run_command", fake_run_command)
    monkeypatch.setattr(
        VideoCompose,
        "_run_final_review",
        lambda *args, **kwargs: {"status": "pass", "issues_found": []},
    )

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


def test_manifest_ids_resolve_to_segmented_narration_and_chinese_captions(tmp_path):
    narration = tmp_path / "narration.wav"
    narration.write_bytes(b"RIFF-test")
    subtitles = tmp_path / "approved.srt"
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\n平台负责决策，引擎负责执行。\n",
        encoding="utf-8",
    )
    manifest = {
        "assets": [
            {
                "id": "narration-approved",
                "type": "audio",
                "subtype": "narration",
                "path": str(narration),
                "scene_id": "scene-01",
            },
            {
                "id": "subtitle-approved",
                "type": "subtitle",
                "path": str(subtitles),
            },
        ]
    }
    decisions = {
        "audio": {
            "narration": {
                "segments": [
                    {
                        "asset_id": "narration-approved",
                        "start_seconds": 0,
                        "end_seconds": 3,
                    }
                ]
            }
        },
        "subtitles": {
            "enabled": True,
            "source": "subtitle-approved",
            "max_words_per_line": 3,
            "max_width_percent": 76,
            "bottom_margin_percent": 8,
        },
    }

    merged = VideoCompose._with_manifest_audio(decisions, manifest)
    merged = VideoCompose._with_manifest_subtitles(merged, manifest)

    assert merged["audio"]["narration"]["segments"][0]["src"] == str(narration)
    assert merged["audio"]["narration"].get("src") is None
    assert merged["subtitles"]["source"] == str(subtitles)
    assert merged["captionJoiner"] == ""
    assert "".join(item["word"] for item in merged["captions"]) == "平台负责决策，引擎负责执行。"
    assert merged["captionStyle"] == {
        "wordsPerPage": 3,
        "fontSize": 42,
        "maxWidthPercent": 76.0,
        "bottomMarginPercent": 8.0,
        "color": "#F8FAFC",
        "backgroundColor": "rgba(15, 23, 42, 0.75)",
    }


def test_render_returns_probe_backed_report_srt_and_poster(tmp_path, monkeypatch):
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    narration = tmp_path / "narration.wav"
    narration.write_bytes(b"audio")
    subtitles = tmp_path / "approved.srt"
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\n真实字幕。\n",
        encoding="utf-8",
    )
    frame = tmp_path / "review.png"
    frame.write_bytes(b"frame")

    def fake_render(self, inputs):
        output = Path(inputs["output_path"])
        output.write_bytes(b"real-enough-mp4")
        return ToolResult(success=True, data={"output": str(output)}, artifacts=[str(output)])

    monkeypatch.setattr(VideoCompose, "_remotion_render", fake_render)
    monkeypatch.setattr(VideoCompose, "_pre_compose_validation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        VideoCompose,
        "_run_final_review",
        lambda *args, **kwargs: {
            "version": "1.0",
            "output_path": str(tmp_path / "final.mp4"),
            "status": "pass",
            "checks": {
                "technical_probe": {
                    "valid_container": True,
                    "duration_seconds": 3.0,
                    "resolution": "1920x1080",
                    "fps": 30.0,
                    "has_audio": True,
                    "codec": "h264",
                    "audio_codec": "aac",
                    "file_size_bytes": 15,
                    "issues": [],
                },
                "visual_spotcheck": {"frame_paths": [str(frame)]},
                "subtitle_check": {
                    "subtitles_expected": True,
                    "subtitles_present": True,
                },
            },
            "issues_found": [],
            "recommended_action": "present_to_user",
        },
    )

    result = VideoCompose().execute(
        {
            "operation": "render",
            "output_path": str(tmp_path / "final.mp4"),
            "asset_manifest": {
                "assets": [
                    {"id": "image-1", "type": "image", "path": str(image)},
                    {
                        "id": "narration-1",
                        "type": "audio",
                        "subtype": "narration",
                        "path": str(narration),
                    },
                    {"id": "subtitle-1", "type": "subtitle", "path": str(subtitles)},
                ]
            },
            "edit_decisions": {
                "render_runtime": "remotion",
                "renderer_family": "explainer-data",
                "cuts": [
                    {
                        "id": "cut-1",
                        "source": "image-1",
                        "in_seconds": 0,
                        "out_seconds": 3,
                    }
                ],
                "audio": {"narration": {"segments": [{"asset_id": "narration-1", "start_seconds": 0}]}},
                "subtitles": {"enabled": True, "source": "subtitle-1"},
            },
        }
    )

    assert result.success
    report = result.data["render_report"]
    assert report["outputs"][0]["codec"] == "h264"
    assert report["outputs"][0]["audio_codec"] == "aac"
    assert report["metadata"]["has_audio"] is True
    assert report["metadata"]["caption_count"] > 0
    assert Path(result.data["poster_path"]).is_file()
    assert str(subtitles) in result.artifacts
    assert result.data["poster_path"] in result.artifacts
