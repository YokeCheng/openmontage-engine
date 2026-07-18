"""Export the deterministic OpenMontage Engine OpenAPI and contract manifest."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine_api.app import create_app  # noqa: E402
from engine_api.contract import build_contract_manifest, canonical_json_bytes, validate_required_operations  # noqa: E402


def export_contract(output_dir: Path) -> tuple[Path, Path]:
    with tempfile.TemporaryDirectory(prefix="openmontage-contract-") as runtime:
        app = create_app(Path(runtime))
        openapi = app.openapi()
    errors = validate_required_operations(openapi)
    if errors:
        raise RuntimeError("Invalid Engine API contract:\n" + "\n".join(errors))

    output_dir.mkdir(parents=True, exist_ok=True)
    openapi_path = output_dir / "engine_api.openapi.json"
    manifest_path = output_dir / "engine_api.contract.json"
    openapi_path.write_bytes(canonical_json_bytes(openapi))
    manifest = build_contract_manifest(app, openapi=openapi)
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return openapi_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "schemas" / "api",
        help="Directory for the committed OpenAPI and contract manifest",
    )
    parser.add_argument("--check", action="store_true", help="Fail when committed exports are stale")
    args = parser.parse_args()

    if args.check:
        with tempfile.TemporaryDirectory(prefix="openmontage-contract-check-") as temporary:
            generated_openapi, generated_manifest = export_contract(Path(temporary))
            expected = {
                "engine_api.openapi.json": generated_openapi.read_bytes(),
                "engine_api.contract.json": generated_manifest.read_bytes(),
            }
        stale = [name for name, content in expected.items() if not (args.output_dir / name).is_file() or (args.output_dir / name).read_bytes() != content]
        if stale:
            print("Stale Engine API contract exports: " + ", ".join(stale))
            print("Run: python scripts/export_engine_api_contract.py")
            return 1
        print("Engine API contract exports are current")
        return 0

    openapi_path, manifest_path = export_contract(args.output_dir)
    print(json.dumps({"openapi": str(openapi_path), "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
