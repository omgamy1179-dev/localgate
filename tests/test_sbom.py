"""Tests for the release SBOM and checksum tooling (audit P0-3).

The generator must produce a CycloneDX 1.5 document that says exactly what
the wheel declares: root component + runtime optional dependencies (yaml/pdf
extras), no fake component versions, a valid unique UUID serialNumber per
run, schema-valid output, and a checksum file that covers exactly the
expected release attachments and detects tampering."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
import zipfile

from tools.generate_sbom import (
    SBOM_NAME,
    SUMS_NAME,
    expected_attachments,
    parse_requires_dist,
    runtime_dependencies,
    wheel_metadata,
)
from tools.generate_sbom import (
    main as sbom_main,
)
from tools.validate_sbom import main as validate_main
from tools.validate_sbom import validate_document

try:
    import jsonschema
except ImportError:  # dev extra missing: schema tests will be skipped
    jsonschema = None

_REAL_REQUIRES = [
    'pyyaml>=6; extra == "yaml"',
    'pypdf>=6.19.0; extra == "pdf"',
    'pytest>=9.1.1; extra == "dev"',
    'pytest-cov>=7.1.0; extra == "dev"',
    'coverage>=7; extra == "dev"',
    'ruff>=0.5; extra == "dev"',
    'mypy>=2.3.1; extra == "dev"',
    'build>=1.2; extra == "dev"',
    'twine>=5; extra == "dev"',
    'pyyaml>=6; extra == "dev"',
    'pypdf>=6.19.0; extra == "dev"',
    'mcp>=2.2.0; extra == "dev"',
]


def make_wheel(path: str, requires: list[str], name: str = "localgate",
               version: str = "1.0.1") -> str:
    body = (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {r}\n" for r in requires))
    dist_info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{dist_info}/METADATA", body)
        z.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
    return path


def make_dist(td: str, requires: list[str] | None = None) -> str:
    dist = os.path.join(td, "dist")
    os.makedirs(dist)
    make_wheel(os.path.join(dist, "localgate-1.0.1-py3-none-any.whl"),
               _REAL_REQUIRES if requires is None else requires)
    with open(os.path.join(dist, "localgate-1.0.1.tar.gz"), "wb") as f:
        f.write(b"fake sdist bytes")
    return dist


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name


class TestRequiresParsing(TempCase):
    def test_name_spec_and_extra_extracted(self):
        self.assertEqual(parse_requires_dist('pyyaml>=6; extra == "yaml"'),
                         ("pyyaml", ">=6", "yaml"))
        self.assertEqual(parse_requires_dist('pypdf>=6.19.0; extra == "pdf"'),
                         ("pypdf", ">=6.19.0", "pdf"))
        self.assertEqual(parse_requires_dist("some-pkg [a,b] >=1,<2 ; python_version>'3'"),
                         ("some-pkg", ">=1,<2", None))
        self.assertEqual(parse_requires_dist("plain-pkg"), ("plain-pkg", "", None))

    def test_unparseable_entry_raises(self):
        with self.assertRaises(ValueError):
            parse_requires_dist("= broken")

    def test_runtime_dependencies_dedup_and_exclude_dev(self):
        deps = runtime_dependencies(_REAL_REQUIRES)
        self.assertEqual(deps, [("pypdf", ">=6.19.0"), ("pyyaml", ">=6")])

    def test_wheel_metadata_roundtrip(self):
        dist = make_dist(self.td)
        meta = wheel_metadata(os.path.join(
            dist, "localgate-1.0.1-py3-none-any.whl"))
        self.assertEqual(meta["name"], "localgate")
        self.assertEqual(meta["version"], "1.0.1")
        self.assertEqual(len(meta["requires"]), len(_REAL_REQUIRES))


class TestSbomContent(TempCase):
    def test_root_runtime_extras_and_no_dev(self):
        dist = make_dist(self.td)
        self.assertEqual(sbom_main(["generate_sbom.py", dist]), 0)
        doc = json.load(open(os.path.join(dist, SBOM_NAME)))
        self.assertEqual(doc["bomFormat"], "CycloneDX")
        self.assertEqual(doc["specVersion"], "1.5")
        root = doc["metadata"]["component"]
        self.assertEqual(root["name"], "localgate")
        self.assertEqual(root["version"], "1.0.1")
        self.assertEqual(root["purl"], "pkg:pypi/localgate@1.0.1")
        names = [c["name"] for c in doc["components"]]
        self.assertEqual(names, ["pypdf", "pyyaml"])
        self.assertEqual(all(c["scope"] == "optional" for c in doc["components"]),
                         True)
        by_name = {c["name"]: c for c in doc["components"]}
        self.assertEqual(by_name["pyyaml"]["purl"], "pkg:pypi/pyyaml")
        # declared constraints live in properties, never as component versions
        self.assertNotIn("version", by_name["pyyaml"])
        props = {p["name"]: p["value"] for p in by_name["pyyaml"]["properties"]}
        self.assertEqual(props["localgate:declared-requirement"], "pyyaml>=6")
        for c in doc["components"]:
            self.assertNotRegex(c["purl"], r"[<>=!~]")
        # dependency graph: root dependsOn the runtime extras
        self.assertEqual(doc["dependencies"],
                         [{"ref": "pkg:pypi/localgate@1.0.1",
                           "dependsOn": ["pkg:pypi/pypdf", "pkg:pypi/pyyaml"]}])
        # dev tooling is excluded, with the exclusion documented in-band
        self.assertNotIn("pytest", json.dumps(doc["components"]))
        policy = [p for p in doc["metadata"]["properties"]
                  if p["name"] == "localgate:dependency-policy"][0]
        self.assertIn("dev", policy["value"])
        self.assertIn("excluded", policy["value"])

    def test_serial_number_valid_and_unique_per_run(self):
        dist = make_dist(self.td)
        serials = set()
        for _ in range(2):
            self.assertEqual(sbom_main(["generate_sbom.py", dist]), 0)
            doc = json.load(open(os.path.join(dist, SBOM_NAME)))
            serial = doc["serialNumber"]
            self.assertTrue(serial.startswith("urn:uuid:"), serial)
            parsed = uuid.UUID(serial.removeprefix("urn:uuid:"))
            self.assertEqual(str(parsed), serial.removeprefix("urn:uuid:"))
            serials.add(serial)
        self.assertEqual(len(serials), 2, "each build gets its own serial")

    @unittest.skipIf(jsonschema is None, "jsonschema (dev extra) missing")
    def test_sbom_passes_official_cyclonedx_15_schema(self):
        dist = make_dist(self.td)
        self.assertEqual(sbom_main(["generate_sbom.py", dist]), 0)
        doc = json.load(open(os.path.join(dist, SBOM_NAME)))
        validate_document(doc)  # raises on any violation; hermetic (no network)
        # and the CLI validator agrees
        self.assertEqual(validate_main(["validate_sbom.py",
                                        os.path.join(dist, SBOM_NAME)]), 0)

    def test_invalid_sbom_fails_validator(self):
        dist = make_dist(self.td)
        self.assertEqual(sbom_main(["generate_sbom.py", dist]), 0)
        path = os.path.join(dist, SBOM_NAME)
        doc = json.load(open(path))
        doc["serialNumber"] = "urn:uuid:localgate-release-sbom"  # the old bug
        json.dump(doc, open(path, "w"))
        self.assertEqual(validate_main(["validate_sbom.py", path]), 1)


class TestChecksums(TempCase):
    def test_sums_cover_exactly_expected_attachments(self):
        dist = make_dist(self.td)
        self.assertEqual(sbom_main(["generate_sbom.py", dist]), 0)
        self.assertEqual(set(expected_attachments(dist)),
                         {"localgate-1.0.1-py3-none-any.whl",
                          "localgate-1.0.1.tar.gz", SBOM_NAME})
        lines: dict[str, str] = {}
        for line in open(os.path.join(dist, SUMS_NAME)).read().splitlines():
            digest, name = line.split(None, 1)
            lines[name.strip()] = digest
        self.assertEqual(set(lines), set(expected_attachments(dist)))
        self.assertEqual(sbom_main(["generate_sbom.py", dist, "--verify"]), 0)

    def test_tampered_attachment_is_detected(self):
        dist = make_dist(self.td)
        self.assertEqual(sbom_main(["generate_sbom.py", dist]), 0)
        wheel = os.path.join(dist, "localgate-1.0.1-py3-none-any.whl")
        with open(wheel, "ab") as f:
            f.write(b"tampered")
        self.assertEqual(sbom_main(["generate_sbom.py", dist, "--verify"]), 1)

    def test_tampered_sbom_is_detected(self):
        dist = make_dist(self.td)
        self.assertEqual(sbom_main(["generate_sbom.py", dist]), 0)
        path = os.path.join(dist, SBOM_NAME)
        doc = json.load(open(path))
        doc["components"].pop()
        json.dump(doc, open(path, "w"))
        self.assertEqual(sbom_main(["generate_sbom.py", dist, "--verify"]), 1)

    def test_stray_dist_file_fails_verification(self):
        dist = make_dist(self.td)
        self.assertEqual(sbom_main(["generate_sbom.py", dist]), 0)
        with open(os.path.join(dist, "stale-artifact.bin"), "wb") as f:
            f.write(b"leftover")
        self.assertEqual(sbom_main(["generate_sbom.py", dist, "--verify"]), 1)

    def test_multiple_wheels_refused(self):
        dist = make_dist(self.td)
        make_wheel(os.path.join(dist, "localgate-1.0.1-py3-none-any2.whl"), [])
        with self.assertRaises(SystemExit):
            expected_attachments(dist)

    def test_real_wheel_metadata_matches_declared_shape(self):
        """The committed wheel in dist/ (if present) must parse with the same
        dependency shape the tests assume - guards against tool drift."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        wheel = os.path.join(repo, "dist", "localgate-1.0.0-py3-none-any.whl")
        if not os.path.exists(wheel):
            self.skipTest("no prebuilt wheel in dist/")
        deps = runtime_dependencies(wheel_metadata(wheel)["requires"])
        self.assertEqual(deps, [("pypdf", ">=6.19.0"), ("pyyaml", ">=6")])


if __name__ == "__main__":
    unittest.main()
