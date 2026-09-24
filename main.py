#!/usr/bin/env python3
"""Deprecated entry shim: the CLI lives in localgate.cli (installed as the
`localgate` console script). Kept so `python main.py ...` keeps working from
a source checkout."""

from __future__ import annotations

from localgate.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
