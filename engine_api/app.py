"""FastAPI application implementing the CouncilForge/OpenMontage v1 contract."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .models import ApproveRequest, CancelRequest, CreateJobRequest, ResolveActionRequest
from .scheduler import ExecutionScheduler
from .store import EngineStore, TERMINAL, new_id, utc_now


REPO_ROOT = Path(__file__).resolve().parents[1]


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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scheduler.recover()
        yield
        scheduler.shutdown()

    app = FastAPI(title="OpenMontage Engine API", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.scheduler = scheduler

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
            "role": "deterministic-video-executor",
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
        }

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
        action.update({"status": "resolved", "resolution": body.resolution, "resolved_by": body.resolved_by, "resolved_at": utc_now()})
        if job["status"] == "waiting_action":
            job["status"] = "running"
        store.save_job(job)
        store.append_event(job, "action.resolved", {"action_id": action_id, "resolution": body.resolution})
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
