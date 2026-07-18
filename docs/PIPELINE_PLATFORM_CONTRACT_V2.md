# Pipeline Platform Contract v2

`platform_contract` declares whether a host platform can turn its own intake object into a Pipeline run. It does not replace the Pipeline manifest: stages, Director Skills, tool allowlists and produced artifacts remain owned by the original manifest.

The canonical JSON Schema is `schemas/platform/pipeline_platform_contract.schema.json`. Version 2 separates engine integration from product acceptance:

- `requires_input_adapter`: the platform cannot yet supply the Pipeline input;
- `validation`: the adapter and contract exist, but no accepted real end-to-end production evidence has been recorded;
- `ready`: the Pipeline has the required real evidence and may be offered as a product capability;
- `disabled`: the Pipeline is intentionally unavailable.

`ready` requires `acceptance.status=validated`, an evidence ID and validation timestamp. An endpoint, tool, mock render or isolated unit test is not acceptance evidence.

## Required declaration

A v2 contract names:

- the intake adapter and intake schema;
- supported input modes and output formats;
- required and optional Brief fields;
- required source materials;
- capability dependencies, whether each is mandatory, its stage and fallback;
- human approval stages;
- the structured artifact contract;
- the evidence required before product readiness.

Every stage referenced by capability or approval policy must exist in the Pipeline manifest. Every structured artifact named in a stage's `produces` list must have a committed JSON Schema under `schemas/artifacts/`.

## CouncilForge validation lifecycle

CouncilForge rejects `validation` Pipelines during normal product operation. An explicit `VIDEO_PIPELINE_VALIDATION_MODE=true` enables them only for real acceptance work. After one new task proves the complete checkpoint chain, actual tool executions, artifact manifest, media probe, platform playback/download and restart recovery, the evidence identity can be committed and readiness changed to `ready`.

`animated-explainer` is the first v2 contract. It remains `validation` until the CouncilForge WBS 2.7 task passes; no other Pipeline is expanded as part of that acceptance.
