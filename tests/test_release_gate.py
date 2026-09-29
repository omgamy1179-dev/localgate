"""Tests for the release-gate helpers (audit P0-4): strict tag validation and
tag/version consistency before anything is published."""

from __future__ import annotations

import unittest

from localgate import __version__
from tools.check_release_tag import check_tag


class TestReleaseTagGate(unittest.TestCase):
    def test_version_importable_for_gate(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_valid_tag_passes(self):
        self.assertEqual(check_tag(f"v{__version__}", __version__), [])

    def test_non_semver_tags_are_refused(self):
        for tag in ("v1", "v1.0", "v1.0.0.0", "1.0.0", "v1.0.0-beta",
                    "v01.0.0", "release-1.0.0", "v1.0.0+build5", "vv1.0.0"):
            with self.subTest(tag=tag):
                problems = check_tag(tag, "1.0.0")
                self.assertTrue(any("SemVer" in p for p in problems), tag)

    def test_version_mismatch_is_refused(self):
        problems = check_tag("v1.0.1", "1.0.2")
        self.assertTrue(any("does not match" in p for p in problems))
        # a well-formed tag whose version simply differs from the code
        problems = check_tag("v9.9.9", __version__)
        self.assertTrue(any("does not match" in p for p in problems))

    def test_cli_rejects_wrong_tag(self):
        import contextlib
        import io

        from tools.check_release_tag import main
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["check_release_tag.py", "v0.0.0-wrong"])
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err.getvalue())

    def test_cli_accepts_current_version(self):
        from tools.check_release_tag import main
        self.assertEqual(main(["check_release_tag.py", f"v{__version__}"]), 0)


if __name__ == "__main__":
    unittest.main()
