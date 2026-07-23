"""Public v1 request models for the deterministic video engine boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SceneVisual(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["motion_graphics", "image", "video", "stock"] = "motion_graphics"
    prompt: str = ""


class VideoShotRequest(BaseModel):
    """One immutable, budget-bounded AI video generation operation."""

    model_config = ConfigDict(extra="forbid")
    scene_id: str = Field(min_length=1, max_length=128)
    operation: Literal["text_to_video", "image_to_video"]
    prompt: str = Field(min_length=1, max_length=8000)
    negative_prompt: str = Field(default="", max_length=4000)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=16)
    reference_image_path: str | None = Field(default=None, max_length=4096)
    duration_seconds: int = Field(ge=1, le=15)
    aspect_ratio: Literal["16:9", "9:16", "1:1"]
    provider: str = Field(default="auto", min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    output_path: str | None = Field(default=None, max_length=4096)
    idempotency_key: str = Field(min_length=1, max_length=240)
    maximum_cost_usd: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_reference(self) -> "VideoShotRequest":
        if self.operation == "image_to_video" and not (
            self.reference_asset_ids or self.reference_image_path
        ):
            raise ValueError("image_to_video requires a reference image")
        return self


class MusicRequest(BaseModel):
    """One approved background-music source and deterministic mix contract."""

    model_config = ConfigDict(extra="forbid")
    source: Literal["uploaded", "library", "generated", "none"] = "none"
    asset_id: str | None = Field(default=None, min_length=1, max_length=240)
    style: str = Field(default="", max_length=1000)
    mood: str = Field(default="", max_length=240)
    tempo_bpm: int | None = Field(default=None, ge=40, le=220)
    instruments: list[str] = Field(default_factory=list, max_length=24)
    duration_seconds: float = Field(gt=0, le=3600)
    instrumental: bool = True
    target_lufs: float = Field(default=-16.0, ge=-40, le=-5)
    ducking_db: float = Field(default=-8.0, ge=-40, le=0)
    fade_in_seconds: float = Field(default=0.4, ge=0, le=30)
    fade_out_seconds: float = Field(default=1.2, ge=0, le=30)
    provider: str = Field(default="auto", min_length=1, max_length=64)
    maximum_cost_usd: float = Field(ge=0)
    fallback: Literal["ask", "continue_without_music"] = "ask"

    @model_validator(mode="after")
    def validate_source(self) -> "MusicRequest":
        if self.source in {"uploaded", "library"} and not self.asset_id:
            raise ValueError(f"{self.source} music requires asset_id")
        return self


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
    defer_start: bool = False
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


class RuntimeConfigRequest(BaseModel):
    """Ephemeral provider configuration supplied by the platform control plane.

    Values are applied to the running engine process only.  The engine never
    writes them to its job store, events, manifests, or artifacts.
    """

    model_config = ConfigDict(extra="forbid")
    values: dict[str, str | None] = Field(default_factory=dict, max_length=128)


class CreateWorkspaceRequest(BaseModel):
    """Create an isolated OpenMontage production workspace."""

    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=240)
    pipeline: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateToolExecutionRequest(BaseModel):
    """Run one registry tool within a declared pipeline stage."""

    model_config = ConfigDict(extra="forbid")
    stage: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    tool_name: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    inputs: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    platform_job_id: str | None = Field(default=None, min_length=1, max_length=128)
    stage_attempt: int = Field(default=1, ge=1, le=1000)


class WriteWorkspaceCheckpointRequest(BaseModel):
    """Persist an agent-authored canonical OpenMontage checkpoint."""

    model_config = ConfigDict(extra="forbid")
    stage: str = Field(min_length=1, max_length=120)
    status: Literal["in_progress", "awaiting_human", "completed", "failed"]
    artifacts: dict[str, Any] = Field(default_factory=dict)
    human_approval_required: bool = False
    human_approved: bool = False
    review: dict[str, Any] | None = None
    cost_snapshot: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
