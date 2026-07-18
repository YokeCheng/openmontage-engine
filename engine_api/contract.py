"""Canonical OpenMontage Engine API contract metadata and export helpers.

The FastAPI OpenAPI document is the transport contract.  The committed JSON
schemas under ``schemas/`` remain the source of truth for durable engine and
pipeline artifacts.  This module joins both sources into one deterministic
manifest that CouncilForge can pin and validate at startup.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI


ENGINE_API_VERSION = "1.0.0"
ENGINE_SCHEMA_VERSION = "1.0"
CONTRACT_MANIFEST_VERSION = "1.0"

REPO_ROOT = Path(__file__).resolve().parents[1]
API_SCHEMA_PATH = REPO_ROOT / "schemas" / "api" / "engine_api.schema.json"
ARTIFACT_SCHEMA_ROOT = REPO_ROOT / "schemas" / "artifacts"
PLATFORM_SCHEMA_ROOT = REPO_ROOT / "schemas" / "platform"
PIPELINE_SCHEMA_ROOT = REPO_ROOT / "schemas" / "pipelines"

# These operation IDs are the stable CouncilForge capability boundary.  The
# legacy engine-managed /v1/jobs endpoints remain public during migration, but
# are intentionally not required by the stage-Agent runtime.
REQUIRED_CAPABILITY_OPERATIONS: dict[str, tuple[str, str]] = {
    "get_engine_contract": ("GET", "/v1/contract"),
    "get_engine_health": ("GET", "/v1/health"),
    "get_engine_capabilities": ("GET", "/v1/capabilities"),
    "list_engine_providers": ("GET", "/v1/providers"),
    "configure_engine_runtime": ("PUT", "/v1/runtime/config"),
    "list_engine_pipelines": ("GET", "/v1/pipelines"),
    "get_engine_pipeline_bundle": ("GET", "/v1/pipelines/{pipeline_name}/bundle"),
    "list_engine_tools": ("GET", "/v1/tools"),
    "get_engine_tool": ("GET", "/v1/tools/{tool_name}"),
    "get_engine_agent_skill": ("GET", "/v1/agent-skills/{skill_name}"),
    "create_engine_workspace": ("POST", "/v1/workspaces"),
    "get_engine_workspace": ("GET", "/v1/workspaces/{workspace_id}"),
    "get_engine_stage_context": ("GET", "/v1/workspaces/{workspace_id}/stages/{stage_name}/context"),
    "create_engine_tool_execution": ("POST", "/v1/workspaces/{workspace_id}/executions"),
    "get_engine_tool_execution": ("GET", "/v1/workspaces/{workspace_id}/executions/{execution_id}"),
    "cancel_engine_tool_execution": ("POST", "/v1/workspaces/{workspace_id}/executions/{execution_id}/cancel"),
    "cancel_engine_workspace": ("POST", "/v1/workspaces/{workspace_id}/cancel"),
    "list_engine_workspace_events": ("GET", "/v1/workspaces/{workspace_id}/events"),
    "write_engine_workspace_checkpoint": ("PUT", "/v1/workspaces/{workspace_id}/checkpoint"),
    "get_engine_workspace_checkpoint": ("GET", "/v1/workspaces/{workspace_id}/checkpoint"),
    "list_engine_workspace_artifacts": ("GET", "/v1/workspaces/{workspace_id}/artifacts"),
    "get_engine_workspace_artifact_content": ("GET", "/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content"),
}


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically for committed snapshots and hashes."""

    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _operation_catalog(openapi: dict[str, Any]) -> list[dict[str, str]]:
    operations: list[dict[str, str]] = []
    for path, path_item in sorted((openapi.get("paths") or {}).items()):
        for method, operation in sorted(path_item.items()):
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            if not path.startswith("/v1/"):
                continue
            operations.append(
                {
                    "operation_id": str(operation.get("operationId") or ""),
                    "method": method.upper(),
                    "path": path,
                }
            )
    return operations


def build_contract_manifest(app: FastAPI, *, openapi: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the deterministic contract manifest published to CouncilForge."""

    document = openapi or app.openapi()
    artifact_schemas = {
        path.relative_to(REPO_ROOT).as_posix(): sha256_file(path)
        for path in sorted(ARTIFACT_SCHEMA_ROOT.glob("*.schema.json"))
    }
    platform_schemas = {
        path.relative_to(REPO_ROOT).as_posix(): sha256_file(path)
        for path in sorted(PLATFORM_SCHEMA_ROOT.glob("*.schema.json"))
    }
    pipeline_schemas = {
        path.relative_to(REPO_ROOT).as_posix(): sha256_file(path)
        for path in sorted(PIPELINE_SCHEMA_ROOT.glob("*.schema.json"))
    }
    required_operations = [
        {"operation_id": operation_id, "method": method, "path": path}
        for operation_id, (method, path) in REQUIRED_CAPABILITY_OPERATIONS.items()
    ]
    return {
        "manifest_version": CONTRACT_MANIFEST_VERSION,
        "api_version": ENGINE_API_VERSION,
        "schema_version": ENGINE_SCHEMA_VERSION,
        "compatibility": {
            "url_major": 1,
            "backward_compatible_changes": [
                "add_optional_field",
                "add_operation",
                "add_enum_only_when_consumer_declares_extensible_enum",
            ],
            "breaking_changes_require": "/v2 and api major version 2",
        },
        "sources": {
            "openapi": {
                "path": "schemas/api/engine_api.openapi.json",
                "sha256": sha256_bytes(canonical_json_bytes(document)),
            },
            "object_schema": {
                "path": API_SCHEMA_PATH.relative_to(REPO_ROOT).as_posix(),
                "sha256": sha256_file(API_SCHEMA_PATH),
            },
            "artifact_schemas": {
                "sha256": sha256_bytes(canonical_json_bytes(artifact_schemas)),
                "files": artifact_schemas,
            },
            "platform_schemas": {
                "sha256": sha256_bytes(canonical_json_bytes(platform_schemas)),
                "files": platform_schemas,
            },
            "pipeline_schemas": {
                "sha256": sha256_bytes(canonical_json_bytes(pipeline_schemas)),
                "files": pipeline_schemas,
            },
        },
        "required_capability_operations": required_operations,
        "operations": _operation_catalog(document),
    }


def validate_required_operations(openapi: dict[str, Any]) -> list[str]:
    """Return deterministic errors for missing or renamed capability operations."""

    discovered = {
        (item["method"], item["path"]): item["operation_id"]
        for item in _operation_catalog(openapi)
    }
    errors: list[str] = []
    for operation_id, (method, path) in REQUIRED_CAPABILITY_OPERATIONS.items():
        actual = discovered.get((method, path))
        if actual is None:
            errors.append(f"missing {method} {path} ({operation_id})")
        elif actual != operation_id:
            errors.append(f"operationId drift for {method} {path}: expected {operation_id}, got {actual}")
    return errors
