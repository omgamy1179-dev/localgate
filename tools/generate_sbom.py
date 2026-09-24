#!/usr/bin/env python3
"""Generate a CycloneDX 1.5 SBOM for a built LocalGate distribution.

Stdlib-only: reads component metadata straight from the wheel so the SBOM
always describes exactly what was built, plus the declared optional
dependencies from the wheel METADATA. Emits `sbom.cdx.json` and
`SHA256SUMS.txt` (one line per artifact in the given directory).

Usage: python tools/generate_sbom.py dist [dist]
"""

from __future__ import annotations

import email.parser
import hashlib
import json
import os
import sys
import zipfile


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def wheel_metadata(wheel_path: str) -> dict:
    with zipfile.ZipFile(wheel_path) as z:
        name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        msg = email.parser.Parser().parsestr(z.read(name).decode("utf-8"))
    return {
        "name": msg["Name"],
        "version": msg["Version"],
        "requires": msg.get_all("Requires-Dist") or [],
    }


def main(argv: list[str]) -> int:
    dist_dir = argv[1] if len(argv) > 1 else "dist"
    if not os.path.isdir(dist_dir):
        print(f"no dist directory at {dist_dir}", file=sys.stderr)
        return 2
    wheels = [os.path.join(dist_dir, n) for n in sorted(os.listdir(dist_dir))
              if n.endswith(".whl")]
    if not wheels:
        print("no wheel found in dist", file=sys.stderr)
        return 2
    meta = wheel_metadata(wheels[0])

    components = [{
        "type": "application",
        "bom-ref": f"pkg:pypi/{meta['name']}@{meta['version']}",
        "name": meta["name"],
        "version": meta["version"],
        "purl": f"pkg:pypi/{meta['name']}@{meta['version']}",
        "licenses": [{"license": {"id": "MIT"}}],
        "description": "Privacy-first local retrieval gateway (stdlib core).",
    }]
    for req in meta["requires"]:
        if "extra ==" in req:
            continue
        name = req.split(";")[0].split("(")[0].strip()
        base = name.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        version = "*" if "=" not in name else name
        components.append({
            "type": "library",
            "bom-ref": f"pkg:pypi/{base.lower()}",
            "name": base,
            "version": version,
            "purl": f"pkg:pypi/{base.lower()}",
            "scope": "optional",
        })

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:localgate-release-sbom",
        "version": 1,
        "metadata": {
            "component": components[0],
            "properties": [{
                "name": "localgate:note",
                "value": "The LocalGate runtime core uses only the Python "
                         "standard library; listed libraries are optional "
                         "accelerators (pip extras).",
            }],
        },
        "components": components,
    }
    sbom_path = os.path.join(dist_dir, "sbom.cdx.json")
    with open(sbom_path, "w", encoding="utf-8") as f:
        json.dump(sbom, f, indent=2, ensure_ascii=False)
        f.write("\n")

    sums_path = os.path.join(dist_dir, "SHA256SUMS.txt")
    with open(sums_path, "w", encoding="utf-8") as f:
        for n in sorted(os.listdir(dist_dir)):
            p = os.path.join(dist_dir, n)
            if os.path.isfile(p) and n != "SHA256SUMS.txt":
                f.write(f"{sha256_file(p)}  {n}\n")
    print(f"wrote {sbom_path} and {sums_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
