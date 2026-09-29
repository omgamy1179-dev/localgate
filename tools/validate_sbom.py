#!/usr/bin/env python3
"""Validate a CycloneDX SBOM against the official CycloneDX 1.5 JSON schema.

Release toolchain only: jsonschema is a dev dependency and the vendored
schemas live in tools/schemas/ (see its README). Validation is hermetic -
every external $ref is resolved from the vendored files via a referencing
Registry, so no network access happens. The LocalGate runtime never imports
jsonschema.

Usage: python tools/validate_sbom.py [path/to/sbom.cdx.json]
       (default: dist/sbom.cdx.json)
"""

from __future__ import annotations

import json
import os
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(TOOLS_DIR, "schemas", "bom-1.5.schema.json")

_VENDORED_URIS = (
    ("http://cyclonedx.org/schema/bom-1.5.schema.json", "bom-1.5.schema.json"),
    ("http://cyclonedx.org/schema/spdx.schema.json", "spdx.schema.json"),
    ("http://cyclonedx.org/schema/jsf-0.82.schema.json", "jsf-0.82.schema.json"),
)


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_document(doc: dict) -> None:
    """Validate `doc` against the vendored CycloneDX 1.5 schema. Raises
    jsonschema.ValidationError on any violation. Hermetic: remote reference
    retrieval is disabled; all refs resolve to tools/schemas/."""
    import jsonschema
    from referencing import Registry, Resource

    resources = [(uri, Resource.from_contents(_load_json(
        os.path.join(TOOLS_DIR, "schemas", fname))))
        for uri, fname in _VENDORED_URIS]
    registry = Registry().with_resources(resources)
    schema = _load_json(SCHEMA_PATH)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, registry=registry)
    validator.validate(doc)


def main(argv: list[str]) -> int:
    sbom_path = argv[1] if len(argv) > 1 else os.path.join("dist", "sbom.cdx.json")
    if not os.path.isfile(sbom_path):
        print(f"no SBOM at {sbom_path}", file=sys.stderr)
        return 2
    if not os.path.isfile(SCHEMA_PATH):
        print(f"vendored schema missing at {SCHEMA_PATH}", file=sys.stderr)
        return 2
    try:
        import jsonschema
    except ImportError:
        print("jsonschema is required (install the [dev] extra)",
              file=sys.stderr)
        return 2
    doc = _load_json(sbom_path)
    try:
        validate_document(doc)
    except jsonschema.ValidationError as e:
        loc = "/".join(str(p) for p in e.absolute_path)
        print(f"SBOM FAILED CycloneDX 1.5 schema validation at '{loc}': "
              f"{e.message}", file=sys.stderr)
        return 1
    print(f"{sbom_path}: valid CycloneDX 1.5 document "
          f"({doc.get('serialNumber')}, {len(doc.get('components', []))} components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
