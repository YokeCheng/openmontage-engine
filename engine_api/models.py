"""Public v1 request models for the deterministic video engine boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SceneVisual(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["motion_graphics", "image", "video", "stock"] = "motion_graphics"
    prompt: str = ""


class VideoScene(BaseModel):
    model_config = ConfigDict(extra="allow")
    scene_id: str
    title: str
    duration_seconds: float = Field(gt=0, le=600)
    narration: str = ""
    visual: SceneVisual = Field(default_factory=SceneVisual)


class RenderSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    aspect_ratio: Literal["16:9", "9:16"] = "16:9"
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=320, le=3840)
    fps: int = Field(default=30, ge=12, le=60)
    duration_seconds: float = Field(default=30, gt=0, le=600)


class VideoExecutionManifest(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["1.0"] = "1.0"
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=4000)
    format: Literal["product_intro", "knowledge_explainer"]
    language: Literal["zh-CN", "en-US"] = "zh-CN"
    script: dict[str, Any]
    scenes: list[VideoScene] = Field(min_length=1, max_length=100)
    audio: dict[str, Any] = Field(default_factory=dict)
    render: RenderSpec = Field(default_factory=RenderSpec)
    budget: dict[str, Any] = Field(default_factory=dict)
    fallback_policy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scene_duration(self) -> "VideoExecutionManifest":
        total = sum(scene.duration_seconds for scene in self.scenes)
        if total <= 0:
            raise ValueError("scene duration must be positive")
        return self


class PipelineRef(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    version: str = "1.0"


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(min_length=1, max_length=240)
    tenant_id: str = Field(min_length=1, max_length=240)
    created_by: str = Field(min_length=1, max_length=240)
    pipeline: PipelineRef
    input: VideoExecutionManifest
    config_version: str = Field(min_length=1, max_length=240)
    execution_mode: Literal["engine_managed", "platform_managed"] = "engine_managed"
    credential_grants: dict[str, Any] | None = Field(default=None, exclude=True)


class ApproveRequest(BaseModel):
    approval_id: str
    decision: Literal["approved", "approved_with_changes"]
    decided_by: str
    comment: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    requested_by: str = "councilforge-user"


class ResolveActionRequest(BaseModel):
    resolution: str
    resolved_by: str
