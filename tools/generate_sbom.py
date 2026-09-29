#!/usr/bin/env python3
"""Generate a CycloneDX 1.5 SBOM for a built LocalGate distribution.

Generation is stdlib-only: component metadata is read straight from the wheel
so the SBOM always describes exactly what was built. The SBOM contains:

- the LocalGate release itself (root component, type "application");
- one library component per declared RUNTIME optional dependency of the wheel
  (the `yaml` and `pdf` extras). The runtime core itself uses only the Python
  standard library; declared version constraints are recorded as component
  properties, never faked as component versions or invalid purls;
- the `dev` extra (pytest, ruff, mypy, build tooling, ...) is deliberately
  EXCLUDED: those packages are development toolchain, not part of any runtime
  surface of the release (see the `localgate:dependency-policy` property).

Dependency graph: the root `dependsOn` every runtime-extra component.

Also writes `SHA256SUMS.txt` covering exactly the expected release attachments
(the wheel, the sdist and the SBOM itself - nothing else that may happen to
sit in the dist directory) and supports `--verify` to re-check an existing
sums file: exact attachment set and exact hashes.

Validation of the produced SBOM against the official CycloneDX 1.5 schema is
done by tools/validate_sbom.py (dev/release toolchain; jsonschema is a dev
dependency, never a runtime one).

Usage:
    python tools/generate_sbom.py dist
    python tools/generate_sbom.py dist --verify
"""

from __future__ import annotations

import email.parser
import hashlib
import json
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone

SBOM_NAME = "sbom.cdx.json"
SUMS_NAME = "SHA256SUMS.txt"
SPEC_VERSION = "1.5"
RUNTIME_EXTRAS = ("yaml", "pdf")
EXCLUDED_EXTRAS = ("dev",)

_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*(?P<spec>[^;]*?)\s*(?:;\s*(?P<marker>.*))?$")
_EXTRA_RE = re.compile(r"\bextra\s*==\s*['\"]([^'\"]+)['\"]")


def canonical_name(name: str) -> str:
    """PEP 503 normalization: casefold, collapse -_. runs."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requires_dist(req: str) -> tuple[str, str, str | None]:
    """Split one PEP 508 requirement into (canonical name, version spec, extra).

    'pyyaml>=6; extra == "yaml"' -> ('pyyaml', '>=6', 'yaml')
    """
    m = _REQ_RE.match(req)
    if not m:
        raise ValueError(f"unparseable Requires-Dist entry: {req!r}")
    marker = (m.group("marker") or "").strip()
    extra = None
    if marker:
        em = _EXTRA_RE.search(marker)
        if em:
            extra = em.group(1)
    return canonical_name(m.group("name")), (m.group("spec") or "").strip(), extra


def wheel_metadata(wheel_path: str) -> dict:
    with zipfile.ZipFile(wheel_path) as z:
        name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        msg = email.parser.Parser().parsestr(z.read(name).decode("utf-8"))
    return {
        "name": msg["Name"],
        "version": msg["Version"],
        "requires": msg.get_all("Requires-Dist") or [],
    }


def runtime_dependencies(requires: list[str]) -> list[tuple[str, str]]:
    """Unique (canonical name, declared spec) pairs for the runtime extras."""
    deps: dict[str, str] = {}
    for req in requires:
        name, spec, extra = parse_requires_dist(req)
        if extra in RUNTIME_EXTRAS:
            deps.setdefault(name, spec)  # same dep across extras: first wins
    return sorted(deps.items())


def build_sbom(meta: dict) -> dict:
    root_purl = f"pkg:pypi/{canonical_name(meta['name'])}@{meta['version']}"
    root = {
        "type": "application",
        "bom-ref": root_purl,
        "name": meta["name"],
        "version": meta["version"],
        "purl": root_purl,
        "scope": "required",
        "licenses": [{"license": {"id": "MIT"}}],
        "description": "Privacy-first local retrieval gateway (stdlib core).",
    }
    components: list[dict] = []
    depends_on: list[str] = []
    for name, spec in runtime_dependencies(meta["requires"]):
        # purl without version: no version was resolved at build time, and a
        # declared constraint must never be dressed up as one
        purl = f"pkg:pypi/{name}"
        components.append({
            "type": "library",
            "bom-ref": purl,
            "name": name,
            "purl": purl,
            "scope": "optional",
            "properties": [{
                "name": "localgate:declared-requirement",
                "value": f"{name}{spec}" if spec else name,
            }],
        })
        depends_on.append(purl)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z"),
            "component": root,
            "properties": [
                {"name": "localgate:runtime-core",
                 "value": "The LocalGate runtime core uses only the Python "
                          "standard library."},
                {"name": "localgate:dependency-policy",
                 "value": "Components list the wheel's declared runtime "
                          "optional dependencies (extras: "
                          f"{', '.join(RUNTIME_EXTRAS)}) with their declared "
                          "version constraints as properties; no "
                          "environment-resolved versions are claimed. The "
                          f"dev extra ({', '.join(EXCLUDED_EXTRAS)}: build/"
                          "test tooling) is excluded from the release SBOM."},
            ],
        },
        "components": components,
        "dependencies": [{"ref": root_purl, "dependsOn": depends_on}],
    }


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def expected_attachments(dist_dir: str) -> list[str]:
    """The exact set of files a release dist consists of (and SHA256SUMS.txt
    must cover): one wheel, one sdist, and the SBOM."""
    names = sorted(os.listdir(dist_dir))
    wheels = [n for n in names if n.endswith(".whl")]
    sdists = [n for n in names if n.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"dist must contain exactly one wheel and one sdist; found "
            f"wheels={wheels} sdists={sdists}")
    return [wheels[0], sdists[0], SBOM_NAME]


def write_sums(dist_dir: str) -> str:
    sums_path = os.path.join(dist_dir, SUMS_NAME)
    with open(sums_path, "w", encoding="utf-8") as f:
        for n in expected_attachments(dist_dir):
            f.write(f"{sha256_file(os.path.join(dist_dir, n))}  {n}\n")
    return sums_path


def verify_sums(dist_dir: str) -> int:
    """Self-check: the sums file must cover exactly the expected attachments
    with exactly matching hashes. Returns a process exit code."""
    sums_path = os.path.join(dist_dir, SUMS_NAME)
    if not os.path.isfile(sums_path):
        print(f"no {SUMS_NAME} in {dist_dir}", file=sys.stderr)
        return 2
    expected = set(expected_attachments(dist_dir))
    if not os.path.isfile(os.path.join(dist_dir, SBOM_NAME)):
        print(f"no {SBOM_NAME} in {dist_dir}; run generate first",
              file=sys.stderr)
        return 2
    problems = 0
    strays = sorted(set(os.listdir(dist_dir)) - expected - {SUMS_NAME})
    for name in strays:
        print(f"dist contains unexpected file {name} (not a release "
              f"attachment; clean the dist directory)", file=sys.stderr)
        problems += 1
    seen: dict[str, str] = {}
    with open(sums_path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[1].strip() or len(parts[0]) != 64:
                print(f"{SUMS_NAME}:{ln}: malformed line", file=sys.stderr)
                return 1
            digest, name = parts[0], parts[1].strip()
            if name in seen:
                print(f"{SUMS_NAME}:{ln}: duplicate entry for {name}",
                      file=sys.stderr)
                return 1
            seen[name] = digest
    for name in sorted(expected - set(seen)):
        print(f"{SUMS_NAME}: missing entry for expected attachment {name}",
              file=sys.stderr)
        problems += 1
    for name in sorted(set(seen) - expected):
        print(f"{SUMS_NAME}: entry for unexpected file {name}",
              file=sys.stderr)
        problems += 1
    for name in sorted(set(seen) & expected):
        actual = sha256_file(os.path.join(dist_dir, name))
        if actual != seen[name]:
            print(f"{SUMS_NAME}: HASH MISMATCH for {name}: "
                  f"expected {seen[name]}, actual {actual}", file=sys.stderr)
            problems += 1
    if problems:
        return 1
    print(f"{SUMS_NAME} verified: {len(seen)} attachments match exactly")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--verify"]
    verify = "--verify" in argv[1:]
    dist_dir = args[0] if args else "dist"
    if not os.path.isdir(dist_dir):
        print(f"no dist directory at {dist_dir}", file=sys.stderr)
        return 2
    if verify:
        return verify_sums(dist_dir)
    wheels = [os.path.join(dist_dir, n) for n in sorted(os.listdir(dist_dir))
              if n.endswith(".whl")]
    if not wheels:
        print("no wheel found in dist", file=sys.stderr)
        return 2
    sbom = build_sbom(wheel_metadata(wheels[0]))
    sbom_path = os.path.join(dist_dir, SBOM_NAME)
    with open(sbom_path, "w", encoding="utf-8") as f:
        json.dump(sbom, f, indent=2, ensure_ascii=False)
        f.write("\n")
    sums_path = write_sums(dist_dir)
    print(f"wrote {sbom_path} and {sums_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
