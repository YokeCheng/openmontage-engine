"""Validation for host-platform Pipeline contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_CONTRACT_SCHEMA_PATH = REPO_ROOT / "schemas" / "platform" / "pipeline_platform_contract.schema.json"
_VALIDATOR = Draft202012Validator(json.loads(PLATFORM_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8")))


class PipelinePlatformContractError(ValueError):
    """Raised when a v2 Pipeline platform declaration cannot be trusted."""


def validate_pipeline_platform_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    contract = manifest.get("platform_contract")
    if not isinstance(contract, dict):
        raise PipelinePlatformContractError("platform_contract is required")
    if str(contract.get("version") or "") != "2.0":
        raise PipelinePlatformContractError("platform_contract version 2.0 is required")

    errors = sorted(_VALIDATOR.iter_errors(contract), key=lambda error: list(error.absolute_path))
    if errors:
        rendered = "; ".join(
            f"{'.'.join(str(item) for item in error.absolute_path) or '$'}: {error.message}"
            for error in errors
        )
        raise PipelinePlatformContractError(rendered)

    stages = {
        str(stage.get("name"))
        for stage in manifest.get("stages", [])
        if isinstance(stage, dict) and stage.get("name")
    }
    if not stages:
        raise PipelinePlatformContractError("Pipeline must declare stages")

    referenced_stages = {
        str(item.get("stage"))
        for item in contract.get("capability_requirements", [])
        if isinstance(item, dict)
    } | set(contract.get("approval_policy", {}).get("human_approval_stages", []))
    unknown_stages = sorted(referenced_stages - stages)
    if unknown_stages:
        raise PipelinePlatformContractError(f"platform contract references unknown stages: {', '.join(unknown_stages)}")

    produced = {
        str(artifact)
        for stage in manifest.get("stages", [])
        if isinstance(stage, dict)
        for artifact in stage.get("produces", [])
    }
    missing_schemas = sorted(
        artifact
        for artifact in produced
        if not (REPO_ROOT / "schemas" / "artifacts" / f"{artifact}.schema.json").is_file()
    )
    if missing_schemas:
        raise PipelinePlatformContractError(f"produced artifacts have no schema: {', '.join(missing_schemas)}")

    if contract["readiness"] == "ready" and contract["acceptance"]["status"] != "validated":
        raise PipelinePlatformContractError("ready pipelines require validated real E2E evidence")
    return contract
