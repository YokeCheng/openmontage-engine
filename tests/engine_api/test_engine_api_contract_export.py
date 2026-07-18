from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from engine_api.app import create_app
from engine_api.contract import (
    API_SCHEMA_PATH,
    ENGINE_API_VERSION,
    ENGINE_SCHEMA_VERSION,
    REQUIRED_CAPABILITY_OPERATIONS,
    build_contract_manifest,
    canonical_json_bytes,
    sha256_file,
    validate_required_operations,
)
from scripts.export_engine_api_contract import export_contract


ROOT = Path(__file__).resolve().parents[2]


def test_capability_operations_have_stable_operation_ids(tmp_path: Path) -> None:
    app = create_app(tmp_path / "runtime")
    openapi = app.openapi()

    assert validate_required_operations(openapi) == []
    operation_ids = {
        operation["operationId"]
        for path_item in openapi["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert set(REQUIRED_CAPABILITY_OPERATIONS).issubset(operation_ids)


def test_contract_manifest_hashes_transport_and_schema_sources(tmp_path: Path) -> None:
    app = create_app(tmp_path / "runtime")
    openapi = app.openapi()
    manifest = build_contract_manifest(app, openapi=openapi)

    assert manifest["api_version"] == ENGINE_API_VERSION
    assert manifest["schema_version"] == ENGINE_SCHEMA_VERSION
    assert manifest["sources"]["object_schema"]["sha256"] == sha256_file(API_SCHEMA_PATH)
    assert len(manifest["sources"]["openapi"]["sha256"]) == 64
    assert manifest["required_capability_operations"] == [
        {"operation_id": operation_id, "method": method, "path": path}
        for operation_id, (method, path) in REQUIRED_CAPABILITY_OPERATIONS.items()
    ]


def test_contract_endpoint_publishes_same_manifest(tmp_path: Path) -> None:
    app = create_app(tmp_path / "runtime")
    expected = build_contract_manifest(app)

    with TestClient(app) as client:
        response = client.get("/v1/contract")

    assert response.status_code == 200
    assert response.json() == expected


def test_contract_export_is_deterministic_and_committed(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_openapi, first_manifest = export_contract(first)
    second_openapi, second_manifest = export_contract(second)

    assert first_openapi.read_bytes() == second_openapi.read_bytes()
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    assert json.loads(first_manifest.read_text(encoding="utf-8"))["sources"]["openapi"]["sha256"] == hashlib.sha256(
        canonical_json_bytes(json.loads(first_openapi.read_text(encoding="utf-8")))
    ).hexdigest()
    assert first_openapi.read_bytes() == (ROOT / "schemas" / "api" / "engine_api.openapi.json").read_bytes()
    assert first_manifest.read_bytes() == (ROOT / "schemas" / "api" / "engine_api.contract.json").read_bytes()
