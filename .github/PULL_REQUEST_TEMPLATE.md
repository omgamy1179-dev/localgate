# Pull request checklist

Thank you for contributing to LocalGate. Please confirm each item — the
privacy boundaries are product requirements, not suggestions.

## What does this PR change?

<!-- One or two sentences. Reference the issue number if there is one. -->

## Testing

- [ ] Added or updated tests that cover the change
- [ ] `python -m pytest tests --cov=localgate` passes locally
- [ ] `python tests/run_scenarios.py` (end-to-end) passes locally
- [ ] `python -m ruff check localgate tests main.py` and `python -m mypy` pass

## Privacy boundaries

- [ ] No user files are modified, created, deleted or renamed
- [ ] Nothing is indexed without an explicit whitelist entry
- [ ] No data is sent to any address other than loopback (`127.0.0.1` / `::1`)
- [ ] No telemetry, remote fonts, CDNs or cloud error reporting were added
- [ ] New dependencies (if any) are optional, justified, and license-reviewed

## Compatibility

- [ ] Works on Python 3.10+ (the standard library only, for the core)
- [ ] Works on Linux, macOS and Windows (or is properly skipped there)
- [ ] Config files and APIs stay backward compatible, or the break is
      documented in CHANGELOG.md

## Documentation

- [ ] README / config comments updated if behaviour or endpoints changed
- [ ] CHANGELOG.md entry added under "Unreleased"
