from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from engine_api.platform_contract import PipelinePlatformContractError, REPO_ROOT, validate_pipeline_platform_contract


def _animated_explainer() -> dict:
    return yaml.safe_load((REPO_ROOT / "pipeline_defs" / "animated-explainer.yaml").read_text(encoding="utf-8"))


def test_animated_explainer_v2_contract_is_valid_but_awaits_real_e2e() -> None:
    contract = validate_pipeline_platform_contract(_animated_explainer())

    assert contract["version"] == "2.0"
    assert contract["readiness"] == "validation"
    assert contract["intake_adapter"] == "councilforge-video-brief-v1"
    assert contract["acceptance"]["status"] == "pending_real_e2e"
    assert {item["capability"] for item in contract["capability_requirements"] if item["required"]} == {
        "video_post",
        "tts",
        "image_generation",
    }


def test_ready_requires_real_e2e_evidence_identity_and_timestamp() -> None:
    manifest = _animated_explainer()
    manifest["platform_contract"]["readiness"] = "ready"
    manifest["platform_contract"]["acceptance"]["status"] = "validated"

    with pytest.raises(PipelinePlatformContractError, match="evidence_id"):
        validate_pipeline_platform_contract(manifest)

    manifest["platform_contract"]["acceptance"].update(
        {"evidence_id": "acceptance_job_1", "validated_at": "2026-07-18T00:00:00Z"}
    )
    assert validate_pipeline_platform_contract(manifest)["readiness"] == "ready"


def test_capability_and_approval_stages_must_exist() -> None:
    manifest = _animated_explainer()
    manifest["platform_contract"]["capability_requirements"][0]["stage"] = "unknown"

    with pytest.raises(PipelinePlatformContractError, match="unknown stages"):
        validate_pipeline_platform_contract(manifest)


def test_every_produced_structured_artifact_requires_a_schema() -> None:
    manifest = _animated_explainer()
    manifest["stages"][0]["produces"].append("missing_platform_artifact")

    with pytest.raises(PipelinePlatformContractError, match="no schema"):
        validate_pipeline_platform_contract(manifest)


def test_validation_contract_cannot_omit_required_evidence_policy() -> None:
    manifest = deepcopy(_animated_explainer())
    manifest["platform_contract"]["acceptance"].pop("required_evidence")

    with pytest.raises(PipelinePlatformContractError, match="required_evidence"):
        validate_pipeline_platform_contract(manifest)
