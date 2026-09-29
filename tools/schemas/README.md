# Vendored CycloneDX 1.5 JSON schemas

These files are the official CycloneDX specification schemas, fetched from
https://github.com/CycloneDX/specification (tag `1.5`, Apache License 2.0):

- `bom-1.5.schema.json` (with its `spdx.schema.json` and
  `jsf-0.82.schema.json` dependencies)

They are used ONLY by release/dev tooling (`tools/validate_sbom.py`, tests)
to validate the generated SBOM. They are not part of the LocalGate runtime
and are not installed with the package.
