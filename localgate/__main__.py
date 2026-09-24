"""Module entry: `python -m localgate` behaves like the `localgate` CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
