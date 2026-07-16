"""Contract checks for the CouncilForge-to-OpenMontage Engine v1 boundary."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "api" / "engine_api.schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator(definition: str) -> Draft202012Validator:
    schema = _schema()
    return Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{definition}",
        },
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def test_engine_api_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(_schema())


def test_job_contract_accepts_representative_snapshot() -> None:
    job = {
        "schema_version": "1.0",
        "job_id": "job_019",
        "request_id": "req_019",
        "tenant_id": "tenant_123",
        "created_by": "user_456",
        "pipeline": {"name": "framework-smoke", "version": "1.0"},
        "status": "running",
        "stage": "compose",
        "progress": {
            "percent": 75,
            "message": "Composing preview",
            "updated_at": "2026-07-16T08:00:00Z",
        },
        "input": {"title": "Contract smoke test"},
        "config_version": "councilforge-config-42",
        "approval": None,
        "artifacts": [],
        "error": None,
        "created_at": "2026-07-16T07:50:00Z",
        "updated_at": "2026-07-16T08:00:00Z",
    }

    _validator("job").validate(job)


def test_job_statuses_are_frozen_for_v1() -> None:
    statuses = _schema()["$defs"]["job_status"]["enum"]

    assert statuses == [
        "created",
        "planning",
        "running",
        "waiting_approval",
        "waiting_action",
        "rendering",
        "succeeded",
        "failed",
        "cancelled",
    ]


def test_event_contract_requires_per_job_sequence() -> None:
    event = {
        "schema_version": "1.0",
        "event_id": "evt_019",
        "job_id": "job_019",
        "tenant_id": "tenant_123",
        "sequence": 1,
        "type": "job.created",
        "occurred_at": "2026-07-16T07:50:00Z",
        "correlation_id": "corr_019",
        "data": {},
    }

    _validator("event").validate(event)
