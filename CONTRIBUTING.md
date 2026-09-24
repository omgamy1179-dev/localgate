# Contributing to LocalGate

Thank you for considering a contribution! LocalGate is a privacy-first local
retrieval gateway, so contributions are held to a slightly stricter bar than
usual: **the privacy boundaries are product requirements, not suggestions.**

## Non-negotiable boundaries

A PR will be rejected regardless of other merit if it:

- modifies, creates, deletes or renames any user file inside the whitelist;
- indexes anything without an explicit whitelist entry;
- sends file text, chunks, vectors, index data or logs to any address that is
  not loopback (`127.0.0.1`, `::1`) — including "optional" telemetry, CDNs,
  remote fonts or cloud error reporting;
- weakens input validation, resource caps or error handling to make a test
  pass;
- binds the HTTP service to a non-loopback address by default or makes that
  easier to do accidentally.

`localgate/httpclient.py::validate_loopback_http_url` is the single gate for
outbound URLs; `localgate/httpapi.py` enforces the Host/Origin/Content-Type
gates for inbound requests. Changes to either require a regression test that
fails before your fix and passes after.

## Development environment

```bash
git clone <your fork>
cd localgate
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -e .[dev]
```

Python 3.10+ is supported. The runtime core is standard-library-only;
`pyyaml`, `pypdf` and `mcp` are optional extras used by tests.

## Running the checks locally

```bash
python -m pytest tests -p no:cacheprovider --cov=localgate   # unit + security tests
python tests/run_scenarios.py                                # end-to-end scenarios
python -m ruff check localgate tests main.py                 # lint
python -m mypy                                               # type check
python -m build && python -m twine check dist/*              # packaging
```

CI runs the same matrix on Linux, macOS and Windows with Python 3.10 and
3.13. New features should keep total coverage at or above 85% (security
modules ≥ 90%).

## Branches and commits

- Branch from `main`; keep one logical change per PR.
- Commit messages: imperative mood, e.g. `httpapi: reject non-JSON content
  types with 415`.
- Keep `data/`, `logs/`, caches and build artifacts out of commits (they are
  git-ignored; never force-add them).

## Pull requests

The PR template asks you to confirm tests, privacy boundaries, compatibility
and documentation. Please fill it in — a PR whose checks pass but that has no
test for its behaviour will usually be asked to add one.

## Release notes

Releases use GitHub's auto-generated release notes ("Generate release
notes"), so please write PR titles that read well in a changelog and add the
`breaking:` prefix in the title when applicable.

## Reporting security issues

Please use private vulnerability reporting (see [SECURITY.md](SECURITY.md)) —
never a public issue.
