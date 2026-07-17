"""FastAPI application implementing the CouncilForge/OpenMontage v1 contract."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .capability_gateway import CapabilityGateway
from .models import (
    ApproveRequest,
    CancelRequest,
    CreateJobRequest,
    CreateToolExecutionRequest,
    CreateWorkspaceRequest,
    ResolveActionRequest,
    RuntimeConfigRequest,
    WriteWorkspaceCheckpointRequest,
)
from .scheduler import ExecutionScheduler
from .store import EngineStore, TERMINAL, new_id, utc_now


REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_CONFIG_LOCK = threading.RLock()
_SECRET_FIELD_PATTERN = re.compile(
    r"\b(?:[A-Z][A-Z0-9_]*(?:API_KEY|KEY|TOKEN|SECRET|CREDENTIALS|URL)|VIDEO_GEN_LOCAL_ENABLED|MUSIC_LIBRARY_DIR)\b"
)


def _pipeline_catalog() -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for path in sorted((REPO_ROOT / "pipeline_defs").glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        stages = data.get("stages") or []
        catalog.append(
            {
                "name": data.get("name") or path.stem,
                "version": str(data.get("version") or "1.0"),
                "description": data.get("description") or "",
                "stability": data.get("stability") or ("test" if path.stem == "framework-smoke" else "production"),
                "stages": [stage.get("name") if isinstance(stage, dict) else stage for stage in stages],
                "execution_contract": "normalized-video-manifest",
                "agent_execution_contract": "capability-gateway-v1",
            }
        )
    return catalog


def _provider_capabilities() -> dict[str, Any]:
    try:
        from tools.tool_registry import registry

        registry.ensure_discovered()
        return registry.provider_menu_summary()
    except Exception as exc:
        return {
            "composition_runtimes": {
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "remotion": (REPO_ROOT / "remotion-composer" / "node_modules").exists(),
                "hyperframes": False,
            },
            "capabilities": [],
            "runtime_warnings": [f"Tool registry unavailable: {type(exc).__name__}"],
            "setup_offers": [],
        }


def _provider_catalog() -> dict[str, Any]:
    """Return the registry-backed provider configuration catalog.

    OpenMontage tools remain the source of truth.  Credential field names are
    derived from each tool contract instead of being duplicated in the API.
    """

    from tools.tool_registry import registry

    registry.ensure_discovered()
    menu = registry.provider_menu()
    providers: dict[str, dict[str, Any]] = {}
    for capability, group in menu.items():
        for availability in ("available", "unavailable"):
            for tool in group.get(availability, []):
                provider = str(tool.get("provider") or "unknown")
                if provider in {"selector", "multi"}:
                    continue
                entry = providers.setdefault(
                    provider,
                    {
                        "provider": provider,
                        "capabilities": set(),
                        "tools": [],
                        "credential_fields": set(),
                        "configured": False,
                    },
                )
                entry["capabilities"].add(capability)
                entry["tools"].append(
                    {
                        "name": tool.get("name"),
                        "capability": capability,
                        "runtime": tool.get("runtime"),
                        "status": tool.get("status"),
                        "best_for": tool.get("best_for", []),
                        "install_instructions": tool.get("install_instructions", ""),
                    }
                )
                entry["configured"] = entry["configured"] or availability == "available"
                dependencies = tool.get("dependencies") or []
                for dependency in dependencies:
                    if isinstance(dependency, str) and dependency.startswith("env:"):
                        entry["credential_fields"].add(dependency.removeprefix("env:"))
                setup_offer = tool.get("setup_offer") or {}
                if isinstance(setup_offer.get("env_var"), str):
                    entry["credential_fields"].add(setup_offer["env_var"])
                instructions = str(tool.get("install_instructions") or "")
                entry["credential_fields"].update(_SECRET_FIELD_PATTERN.findall(instructions))

    result: list[dict[str, Any]] = []
    for provider in sorted(providers):
        entry = providers[provider]
        result.append(
            {
                **entry,
                "capabilities": sorted(entry["capabilities"]),
                "credential_fields": sorted(entry["credential_fields"]),
            }
        )
    return {"providers": result}


def _allowed_runtime_fields() -> set[str]:
    return {
        field
        for provider in _provider_catalog()["providers"]
        for field in provider.get("credential_fields", [])
    }


def _pipeline_bundle(pipeline_name: str) -> dict[str, Any]:
    """Load a pipeline manifest and the stage instructions its Agent follows."""

    from lib.pipeline_loader import load_pipeline

    manifest = load_pipeline(pipeline_name)
    stages: list[dict[str, Any]] = []
    for stage in manifest.get("stages", []):
        skill_ref = stage.get("skill")
        instruction = ""
        if skill_ref:
            skill_path = (REPO_ROOT / "skills" / f"{skill_ref}.md").resolve()
            skills_root = (REPO_ROOT / "skills").resolve()
            if skills_root not in skill_path.parents or not skill_path.is_file():
                raise FileNotFoundError(f"Pipeline skill is unavailable: {skill_ref}")
            instruction = skill_path.read_text(encoding="utf-8")
        stages.append(
            {
                "name": stage.get("name"),
                "skill": skill_ref,
                "instruction": instruction,
                "produces": stage.get("produces", []),
                "tools_available": stage.get("tools_available", []),
                "human_approval_default": bool(stage.get("human_approval_default", False)),
                "review_focus": stage.get("review_focus", []),
                "success_criteria": stage.get("success_criteria", []),
            }
        )
    return {"manifest": manifest, "stages": stages}


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(job))
    result.pop("correlation_id", None)
    for artifact in result.get("artifacts", []):
        artifact.pop("storage_name", None)
    return result


def problem(status: int, title: str, detail: str, code: str, request: Request | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"https://openmontage.dev/problems/{code.lower().replace('_', '-')}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": str(request.url.path) if request else None,
            "error_code": code,
            "retryable": status >= 500,
        },
    )


def create_app(runtime_root: Path | None = None) -> FastAPI:
    root = runtime_root or Path(os.getenv("OPENMONTAGE_ENGINE_RUNTIME", REPO_ROOT / ".engine-runtime"))
    store = EngineStore(root)
    scheduler = ExecutionScheduler(store, REPO_ROOT)
    gateway = CapabilityGateway(
        root / "capability-gateway",
        REPO_ROOT,
        max_workers=max(1, int(os.getenv("OPENMONTAGE_TOOL_MAX_WORKERS", "2"))),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scheduler.recover()
        gateway.recover()
        yield
        scheduler.shutdown()
        gateway.shutdown()

    app = FastAPI(title="OpenMontage Engine API", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.capability_gateway = gateway

    async def authorize_service(
        authorization: str | None = Header(default=None),
    ) -> None:
        expected = os.getenv("OPENMONTAGE_ENGINE_TOKEN")
        if expected and authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="Invalid service token")

    async def authorize(
        _: None = Depends(authorize_service),
        x_tenant_id: str | None = Header(default=None),
    ) -> str:
        if not x_tenant_id:
            raise HTTPException(status_code=400, detail="X-Tenant-ID is required")
        return x_tenant_id

    def owned(job_id: str, tenant_id: str) -> dict[str, Any]:
        job = store.load_job(job_id)
        if not job or job.get("tenant_id") != tenant_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "ready": True, "version": "1.0.0"}

    @app.get("/v1/capabilities")
    async def capabilities(_: None = Depends(authorize_service)) -> dict[str, Any]:
        menu = await asyncio.to_thread(_provider_capabilities)
        return {
            "schema_version": "1.0",
            "role": "model-free-video-capability-environment",
            "formats": ["product_intro", "knowledge_explainer"],
            "scheduler": {"kind": "bounded-worker-pool", "max_workers": scheduler.max_workers, "restart_recovery": True},
            # These are the runtimes implemented by the normalized manifest
            # adapter, not every runtime installed in the upstream toolbox.
            "renderers": [
                {
                    "name": "remotion",
                    "available": bool((menu.get("composition_runtimes") or {}).get("remotion")),
                }
            ],
            "toolbox_composition_runtimes": menu.get("composition_runtimes", {}),
            "capabilities": menu.get("capabilities", []),
            "setup_offers": menu.get("setup_offers", []),
            "runtime_warnings": menu.get("runtime_warnings", []),
            "requires_model_credentials": False,
            "agent_host": "external",
            "tool_execution_api": "capability-gateway-v1",
        }

    @app.get("/v1/providers")
    async def providers(_: None = Depends(authorize_service)) -> dict[str, Any]:
        return await asyncio.to_thread(_provider_catalog)

    @app.put("/v1/runtime/config")
    async def configure_runtime(
        body: RuntimeConfigRequest,
        request: Request,
        _: None = Depends(authorize_service),
    ) -> JSONResponse:
        allowed = await asyncio.to_thread(_allowed_runtime_fields)
        unknown = sorted(set(body.values) - allowed)
        if unknown:
            return problem(
                422,
                "Unsupported provider setting",
                f"Unknown runtime configuration fields: {', '.join(unknown)}",
                "RUNTIME_CONFIG_FIELD_UNSUPPORTED",
                request,
            )
        with _RUNTIME_CONFIG_LOCK:
            for key, value in body.values.items():
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)
            from tools.tool_registry import registry

            registry.clear()
            registry.discover()
        return JSONResponse(
            content={
                "configured_fields": sorted(key for key, value in body.values.items() if value),
                "capabilities": _provider_capabilities(),
            }
        )

    @app.get("/v1/pipelines")
    async def pipelines(_: None = Depends(authorize_service)) -> dict[str, Any]:
        return {
            "pipelines": _pipeline_catalog(),
            "adapter": {
                "name": "councilforge-platform",
                "version": "2.0",
                "formats": ["product_intro", "knowledge_explainer"],
                "description": "Executes an approved CouncilForge manifest using the selected OpenMontage capability path.",
            },
        }

    @app.get("/v1/pipelines/{pipeline_name}/bundle")
    async def pipeline_bundle(
        pipeline_name: str,
        request: Request,
        _: None = Depends(authorize_service),
    ) -> JSONResponse:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", pipeline_name):
            return problem(404, "Pipeline not found", "The requested pipeline does not exist.", "PIPELINE_NOT_FOUND", request)
        try:
            bundle = await asyncio.to_thread(_pipeline_bundle, pipeline_name)
        except (FileNotFoundError, ValueError):
            return problem(404, "Pipeline not found", "The requested pipeline does not exist.", "PIPELINE_NOT_FOUND", request)
        return JSONResponse(content=bundle)

    @app.get("/v1/tools")
    async def tools_catalog(_: None = Depends(authorize_service)) -> dict[str, Any]:
        return {"tools": await asyncio.to_thread(gateway.tool_catalog)}

    @app.get("/v1/tools/{tool_name}")
    async def tool_contract(tool_name: str, _: None = Depends(authorize_service)) -> dict[str, Any]:
        tool = await asyncio.to_thread(gateway.tool_info, tool_name)
        if tool is None:
            raise HTTPException(status_code=404, detail="Tool not found")
        return tool

    @app.get("/v1/agent-skills/{skill_name}")
    async def agent_skill(skill_name: str, _: None = Depends(authorize_service)) -> dict[str, Any]:
        skill = await asyncio.to_thread(gateway.agent_skill, skill_name)
        if skill is None:
            raise HTTPException(status_code=404, detail="Agent skill not found")
        return skill

    @app.post("/v1/workspaces", status_code=201)
    async def create_workspace(
        body: CreateWorkspaceRequest,
        request: Request,
        tenant_id: str = Depends(authorize),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        if not idempotency_key:
            return problem(400, "Missing idempotency key", "Idempotency-Key is required.", "IDEMPOTENCY_KEY_REQUIRED", request)
        try:
            workspace, created = await asyncio.to_thread(
                gateway.create_workspace,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                request_id=body.request_id,
                title=body.title,
                pipeline=body.pipeline,
                metadata=body.metadata,
            )
        except FileNotFoundError:
            return problem(404, "Pipeline not found", "The requested pipeline does not exist.", "PIPELINE_NOT_FOUND", request)
        except ValueError as exc:
            if str(exc) == "IDEMPOTENCY_KEY_REUSED":
                return problem(409, "Idempotency key reused", "This key was already used with a different request body.", "IDEMPOTENCY_KEY_REUSED", request)
            return problem(422, "Invalid workspace", str(exc), "WORKSPACE_INVALID", request)
        return JSONResponse(status_code=201 if created else 200, content=workspace)

    @app.get("/v1/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        workspace = await asyncio.to_thread(gateway.load_workspace, workspace_id, tenant_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        return workspace

    @app.get("/v1/workspaces/{workspace_id}/stages/{stage_name}/context")
    async def stage_context(workspace_id: str, stage_name: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(gateway.stage_context, workspace_id, tenant_id, stage_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=f"Pipeline skill is unavailable: {exc}") from exc

    @app.post("/v1/workspaces/{workspace_id}/executions", status_code=202)
    async def create_tool_execution(
        workspace_id: str,
        body: CreateToolExecutionRequest,
        request: Request,
        tenant_id: str = Depends(authorize),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        if not idempotency_key:
            return problem(400, "Missing idempotency key", "Idempotency-Key is required.", "IDEMPOTENCY_KEY_REQUIRED", request)
        try:
            execution, created = await asyncio.to_thread(
                gateway.create_execution,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                stage_name=body.stage,
                tool_name=body.tool_name,
                inputs=body.inputs,
            )
        except KeyError as exc:
            return problem(404, "Capability not found", str(exc.args[0]), str(exc.args[0]), request)
        except PermissionError:
            return problem(403, "Tool is not allowed", "The pipeline stage does not allow this tool.", "TOOL_NOT_ALLOWED_FOR_STAGE", request)
        except ValueError as exc:
            code = str(exc)
            status = 409 if code == "IDEMPOTENCY_KEY_REUSED" else 422
            return problem(status, "Tool execution rejected", code, code, request)
        except RuntimeError as exc:
            return problem(409, "Tool is unavailable", str(exc), "TOOL_UNAVAILABLE", request)
        return JSONResponse(status_code=202 if created else 200, content=execution)

    @app.get("/v1/workspaces/{workspace_id}/executions/{execution_id}")
    async def get_tool_execution(workspace_id: str, execution_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(gateway.get_execution, workspace_id, tenant_id, execution_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc

    @app.post("/v1/workspaces/{workspace_id}/executions/{execution_id}/cancel")
    async def cancel_tool_execution(workspace_id: str, execution_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(gateway.cancel_execution, workspace_id, tenant_id, execution_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc

    @app.post("/v1/workspaces/{workspace_id}/cancel")
    async def cancel_workspace(workspace_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(gateway.cancel_workspace, workspace_id, tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc

    @app.get("/v1/workspaces/{workspace_id}/events")
    async def workspace_events(
        workspace_id: str,
        request: Request,
        after_sequence: int = Query(default=0, ge=0),
        tenant_id: str = Depends(authorize),
    ) -> Any:
        if await asyncio.to_thread(gateway.load_workspace, workspace_id, tenant_id) is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        if "text/event-stream" not in request.headers.get("accept", ""):
            items = await asyncio.to_thread(gateway.events, workspace_id, tenant_id, after_sequence)
            return {"events": items, "next_sequence": items[-1]["sequence"] if items else after_sequence}

        async def stream_workspace_events() -> AsyncIterator[str]:
            cursor = after_sequence
            while not await request.is_disconnected():
                items = await asyncio.to_thread(gateway.events, workspace_id, tenant_id, cursor)
                for event in items:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream_workspace_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.put("/v1/workspaces/{workspace_id}/checkpoint")
    async def write_workspace_checkpoint(
        workspace_id: str,
        body: WriteWorkspaceCheckpointRequest,
        request: Request,
        tenant_id: str = Depends(authorize),
    ) -> JSONResponse:
        try:
            checkpoint = await asyncio.to_thread(gateway.write_checkpoint, workspace_id, tenant_id, body.model_dump())
        except KeyError:
            return problem(404, "Workspace not found", "The requested workspace does not exist.", "WORKSPACE_NOT_FOUND", request)
        except (ValueError, TypeError) as exc:
            return problem(422, "Checkpoint rejected", str(exc), "CHECKPOINT_INVALID", request)
        return JSONResponse(content=checkpoint)

    @app.get("/v1/workspaces/{workspace_id}/checkpoint")
    async def latest_workspace_checkpoint(workspace_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        try:
            checkpoint = await asyncio.to_thread(gateway.latest_checkpoint, workspace_id, tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workspace not found") from exc
        return {"checkpoint": checkpoint}

    @app.get("/v1/workspaces/{workspace_id}/artifacts")
    async def workspace_artifacts(workspace_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        try:
            items = await asyncio.to_thread(gateway.artifacts, workspace_id, tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workspace not found") from exc
        return {"artifacts": items}

    @app.get("/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content")
    async def workspace_artifact_content(
        workspace_id: str,
        artifact_id: str,
        request: Request,
        tenant_id: str = Depends(authorize),
    ) -> Response:
        path = await asyncio.to_thread(gateway.artifact_path, workspace_id, tenant_id, artifact_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        range_header = request.headers.get("range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            size = path.stat().st_size
            if not match:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            start_text, end_text = match.groups()
            if not start_text and not end_text:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            if start_text:
                start = int(start_text)
                end = min(int(end_text), size - 1) if end_text else size - 1
            else:
                suffix = min(int(end_text), size)
                start, end = size - suffix, size - 1
            if start >= size or end < start:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            with path.open("rb") as handle:
                handle.seek(start)
                content = handle.read(end - start + 1)
            return Response(content, status_code=206, media_type=media_type, headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(len(content))})
        return FileResponse(path, media_type=media_type, filename=path.name, headers={"Accept-Ranges": "bytes"})

    @app.get("/v1/jobs")
    async def list_jobs(tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        return {"jobs": [_public_job(job) for job in store.list_jobs(tenant_id)]}

    @app.post("/v1/jobs", status_code=202)
    async def create_job(
        body: CreateJobRequest,
        request: Request,
        tenant_id: str = Depends(authorize),
        idempotency_key: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
    ) -> JSONResponse:
        if not idempotency_key:
            return problem(400, "Missing idempotency key", "Idempotency-Key is required.", "IDEMPOTENCY_KEY_REQUIRED", request)
        if body.tenant_id != tenant_id:
            return problem(403, "Tenant mismatch", "The body tenant does not match X-Tenant-ID.", "TENANT_MISMATCH", request)
        supported_pipelines = {item["name"] for item in _pipeline_catalog()} | {"councilforge-platform"}
        if body.pipeline.name not in supported_pipelines:
            return problem(
                400,
                "Unknown pipeline",
                f"Pipeline '{body.pipeline.name}' is not registered by this engine.",
                "PIPELINE_NOT_REGISTERED",
                request,
            )
        try:
            job, created = store.create_job(body.model_dump(), idempotency_key, x_correlation_id or f"corr_{uuid.uuid4().hex}")
        except ValueError:
            return problem(409, "Idempotency key reused", "This key was already used with a different request body.", "IDEMPOTENCY_KEY_REUSED", request)
        if created:
            job["status"] = "planning"
            job["stage"] = "script"
            job["progress"] = {"percent": 16, "message": "Validating script and budget", "updated_at": utc_now()}
            store.save_job(job)
            store.append_event(job, "job.status_changed", {"status": "planning"})
            if body.execution_mode == "platform_managed":
                # CouncilForge owns the human approval, budget policy and
                # business lifecycle. The engine receives an immutable,
                # already-approved manifest and starts deterministic work.
                job["status"] = "running"
                job["stage"] = "production"
                job["progress"] = {"percent": 40, "message": "Preparing deterministic render", "updated_at": utc_now()}
                store.save_job(job)
                store.append_event(job, "job.status_changed", {"status": "running", "approved_by": "platform"})
                scheduler.submit(job["job_id"])
            else:
                approval_id = new_id("approval")
                job["status"] = "waiting_approval"
                job["stage"] = "script"
                job["progress"] = {"percent": 28, "message": "Script and budget ready for approval", "updated_at": utc_now()}
                job["approval"] = {
                    "approval_id": approval_id,
                    "checkpoint_id": "script-budget-v1",
                    "stage": "script",
                    "status": "pending",
                    "summary": "Review the script, storyboard, and budget before rendering.",
                    "requested_at": utc_now(),
                    "expires_at": None,
                    "review_artifacts": [],
                }
                store.save_job(job)
                store.append_event(job, "approval.required", {"approval_id": approval_id, "stage": "script"})
        return JSONResponse(status_code=202 if created else 200, content=_public_job(store.load_job(job["job_id"]) or job))

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        return _public_job(owned(job_id, tenant_id))

    @app.post("/v1/jobs/{job_id}/approve")
    async def approve(job_id: str, body: ApproveRequest, request: Request, tenant_id: str = Depends(authorize)) -> JSONResponse:
        job = owned(job_id, tenant_id)
        approval = job.get("approval")
        if approval and approval.get("approval_id") == body.approval_id and approval.get("status") in {"approved", "approved_with_changes"}:
            return JSONResponse(content=_public_job(job))
        if job.get("status") != "waiting_approval" or not approval or approval.get("approval_id") != body.approval_id:
            return problem(409, "Invalid job state transition", "Only the current pending approval can be resolved.", "JOB_INVALID_TRANSITION", request)
        approval["status"] = body.decision
        approval["decided_by"] = body.decided_by
        approval["decided_at"] = utc_now()
        approval["comment"] = body.comment
        job["status"] = "running"
        job["stage"] = "production"
        job["progress"] = {"percent": 48, "message": "Preparing deterministic render", "updated_at": utc_now()}
        store.save_job(job)
        store.append_event(job, "approval.resolved", {"approval_id": body.approval_id, "decision": body.decision})
        scheduler.submit(job_id)
        return JSONResponse(content=_public_job(store.load_job(job_id) or job))

    @app.post("/v1/jobs/{job_id}/cancel", status_code=202)
    async def cancel(job_id: str, body: CancelRequest, tenant_id: str = Depends(authorize)) -> JSONResponse:
        job = owned(job_id, tenant_id)
        if job["status"] not in TERMINAL:
            job["status"] = "cancelled"
            job["stage"] = "cancelled"
            job["progress"] = {**job["progress"], "message": "Job cancelled", "updated_at": utc_now()}
            store.save_job(job)
            store.append_event(job, "job.cancelled", {"requested_by": body.requested_by})
        return JSONResponse(status_code=200 if job["status"] in TERMINAL else 202, content=_public_job(job))

    @app.get("/v1/jobs/{job_id}/actions")
    async def actions(job_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        return {"actions": _public_job(owned(job_id, tenant_id)).get("actions", [])}

    @app.post("/v1/jobs/{job_id}/actions/{action_id}/resolve")
    async def resolve_action(job_id: str, action_id: str, body: ResolveActionRequest, request: Request, tenant_id: str = Depends(authorize)) -> JSONResponse:
        job = owned(job_id, tenant_id)
        action = next((item for item in job.get("actions", []) if item.get("action_id") == action_id), None)
        if not action:
            return problem(404, "Action not found", "The requested pending action does not exist.", "ACTION_NOT_FOUND", request)
        if action.get("status") == "resolved":
            return JSONResponse(content=_public_job(job))
        if action.get("status") != "pending":
            return problem(409, "Action cannot be resolved", "The action is not pending.", "ACTION_INVALID_STATE", request)
        allowed_resolutions = {
            str(option.get("value"))
            for option in action.get("options", [])
            if isinstance(option, dict) and option.get("value")
        }
        if allowed_resolutions and body.resolution not in allowed_resolutions:
            return problem(
                422,
                "Unsupported action resolution",
                "Choose one of the resolutions offered by the pending action.",
                "ACTION_RESOLUTION_UNSUPPORTED",
                request,
            )
        action.update({"status": "resolved", "resolution": body.resolution, "resolved_by": body.resolved_by, "resolved_at": utc_now()})
        if body.resolution == "cancel":
            job["status"] = "cancelled"
            job["stage"] = "cancelled"
            job["progress"] = {**job["progress"], "message": "Job cancelled", "updated_at": utc_now()}
        elif job["status"] == "waiting_action":
            if body.resolution == "use_motion_graphics":
                context = action.get("context") or {}
                capability = context.get("capability")
                policy = job.get("input", {}).setdefault("media_policy", {})
                if capability in {"ai_image", "ai_video", "stock"}:
                    policy["visual_source"] = "motion_graphics"
                elif capability == "tts":
                    policy["voice_provider"] = "none"
                elif capability == "music_generation":
                    policy["music_provider"] = "none"
                policy["fallback"] = "motion_graphics"
            job["status"] = "running"
            job["stage"] = "production"
            job["progress"] = {"percent": 48, "message": "Resuming approved production", "updated_at": utc_now()}
        store.save_job(job)
        store.append_event(job, "action.resolved", {"action_id": action_id, "resolution": body.resolution})
        if job["status"] == "running":
            scheduler.submit(job_id)
        return JSONResponse(content=_public_job(job))

    @app.get("/v1/jobs/{job_id}/events")
    async def events(
        job_id: str,
        request: Request,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        tenant_id: str = Depends(authorize),
    ) -> Any:
        owned(job_id, tenant_id)
        if "text/event-stream" not in request.headers.get("accept", ""):
            items = store.events(job_id, after_sequence)[:limit]
            return {"events": items, "next_sequence": items[-1]["sequence"] if items else after_sequence}

        async def stream() -> AsyncIterator[str]:
            cursor = after_sequence
            while True:
                items = store.events(job_id, cursor)
                for event in items:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                job = store.load_job(job_id)
                if not job or job["status"] in TERMINAL:
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/v1/jobs/{job_id}/artifacts")
    async def artifacts(job_id: str, tenant_id: str = Depends(authorize)) -> dict[str, Any]:
        return {"artifacts": _public_job(owned(job_id, tenant_id)).get("artifacts", [])}

    @app.get("/v1/jobs/{job_id}/artifacts/{artifact_id}/content")
    async def artifact_content(
        job_id: str,
        artifact_id: str,
        request: Request,
        tenant_id: str = Depends(authorize),
    ) -> Response:
        job = owned(job_id, tenant_id)
        artifact = next(
            (
                item
                for item in job.get("artifacts", [])
                if item.get("artifact_id") == artifact_id
            ),
            None,
        )
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = store.artifact_path(job_id, artifact_id)
        if not path or not path.exists():
            raise HTTPException(status_code=404, detail="Artifact not found")
        media_type = str(artifact.get("media_type") or "application/octet-stream")
        range_header = request.headers.get("range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            size = path.stat().st_size
            if not match:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            start_text, end_text = match.groups()
            if not start_text and not end_text:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            if start_text:
                start = int(start_text)
                end = min(int(end_text), size - 1) if end_text else size - 1
            else:
                suffix = min(int(end_text), size)
                start, end = size - suffix, size - 1
            if start >= size or end < start:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            with path.open("rb") as handle:
                handle.seek(start)
                content = handle.read(end - start + 1)
            return Response(
                content,
                status_code=206,
                media_type=media_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Length": str(len(content)),
                },
            )
        return FileResponse(
            path,
            media_type=media_type,
            filename=path.name,
            headers={"Accept-Ranges": "bytes"},
        )

    return app


app = create_app()
