"""Focal-aware delivery variants stay deterministic inside OpenMontage."""

from __future__ import annotations

import pytest

from engine_api.models import ReframeInstruction, RenderSpec
from engine_api.renderer import _render_contract


@pytest.mark.parametrize(
    ("aspect_ratio", "expected"),
    [
        ("16:9", (1920, 1080)),
        ("9:16", (1080, 1920)),
        ("1:1", (1080, 1080)),
    ],
)
def test_render_spec_normalizes_standard_delivery_dimensions(
    aspect_ratio: str,
    expected: tuple[int, int],
) -> None:
    render = RenderSpec.model_validate({"aspect_ratio": aspect_ratio})

    assert (render.width, render.height) == expected


def test_reframe_instruction_rejects_out_of_bounds_focal_point() -> None:
    with pytest.raises(ValueError):
        ReframeInstruction(
            scene_id="scene-01",
            focal_point=(1.1, 0.5),
            safe_region=(0.1, 0.1, 0.8, 0.8),
        )


def test_render_contract_binds_focal_and_safe_regions_to_the_scene_cut() -> None:
    manifest = {
        "title": "Square delivery",
        "objective": "Keep the speaker visible",
        "format": "knowledge_explainer",
        "language": "en-US",
        "scenes": [
            {
                "scene_id": "scene-01",
                "title": "Speaker",
                "duration_seconds": 5,
                "narration": "Keep the speaker centered.",
            }
        ],
        "render": {"aspect_ratio": "1:1", "duration_seconds": 5},
        "delivery": {
            "reframe_instructions": [
                {
                    "scene_id": "scene-01",
                    "focal_point": [0.4, 0.5],
                    "safe_region": [0.1, 0.1, 0.8, 0.8],
                    "allow_crop": True,
                    "allow_padding": False,
                }
            ]
        },
        "pipeline_artifacts": {
            "edit_decisions": {
                "cuts": [
                    {
                        "id": "scene-01",
                        "in_seconds": 0,
                        "out_seconds": 5,
                        "type": "text_card",
                        "text": "Speaker",
                    }
                ]
            }
        },
    }

    composition, props = _render_contract(manifest)

    assert composition == "Explainer"
    assert props["render"]["width"] == 1080
    assert props["render"]["height"] == 1080
    assert props["cuts"][0]["focalPoint"] == {"x": 0.4, "y": 0.5}
    assert props["cuts"][0]["safeRegion"] == {
        "x": 0.1,
        "y": 0.1,
        "width": 0.8,
        "height": 0.8,
    }
    assert props["cuts"][0]["allowCrop"] is True
    assert props["cuts"][0]["allowPadding"] is False
