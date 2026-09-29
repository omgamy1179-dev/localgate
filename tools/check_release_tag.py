#!/usr/bin/env python3
"""Validate a LocalGate release tag before anything gets published.

A release tag MUST be strict SemVer (`vMAJOR.MINOR.PATCH` - no pre-release,
no build metadata, no other prefix) and MUST equal `localgate.__version__` at
the tagged commit. The release workflow runs this BEFORE any verify or
publish job; a failing check aborts the whole release.

Usage: python tools/check_release_tag.py [TAG]
       (TAG defaults to $GITHUB_REF_NAME, i.e. how the workflow calls it)
Exit codes: 0 valid, 1 invalid, 2 usage error.
"""

from __future__ import annotations

import os
import re
import sys

TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def localgate_version() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from localgate import __version__
    return __version__


def check_tag(tag: str, version: str) -> list[str]:
    """Return a list of problems (empty means the tag is valid)."""
    problems: list[str] = []
    if not TAG_RE.match(tag):
        problems.append(
            f"tag {tag!r} is not strict SemVer: expected vMAJOR.MINOR.PATCH")
    if tag != f"v{version}":
        problems.append(
            f"tag {tag!r} does not match localgate.__version__ {version!r} "
            f"(expected tag 'v{version}')")
    return problems


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    tag = args[0] if args else os.environ.get("GITHUB_REF_NAME", "")
    if not tag:
        print("no tag given (pass one or set GITHUB_REF_NAME)", file=sys.stderr)
        return 2
    problems = check_tag(tag, localgate_version())
    if problems:
        for p in problems:
            print(f"REFUSED release tag: {p}", file=sys.stderr)
        return 1
    print(f"release tag {tag} is valid and matches localgate "
          f"__version__ {localgate_version()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
