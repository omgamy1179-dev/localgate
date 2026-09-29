# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.1] - 2026-09-29

Security and release-engineering hardening release (no API changes).

### Fixed

- Extraction deadlines are now actually enforceable: text extraction runs in
  a dedicated, terminable child process. A parser that blows past the
  per-file 30s deadline is terminated and reaped instead of lingering as a
  daemon thread; consecutive pathological files can no longer accumulate
  background workers, and indexing continues with the next file.
- `max_file_mb` is now enforced at every read entry: the fingerprint hash
  runs under a byte budget (a file that grows past the cap mid-read aborts
  instead of being read without bound), single-file ingest re-checks the cap
  before dispatching to OCR or the parser, the parser re-checks in its own
  process, and self-check retry refuses oversized files with a diagnosable,
  cheaply-retryable `too_large` status. The retry path could previously
  bypass the size cap for failed docs that later grew.
- Index deletion confirmation is now provable: the parent directory must
  actually be listable, and whitelist roots carry a persisted volume
  identity (backward compatible) so a hollow mount point or a recreated
  root directory can never purge root-direct index entries.

### Changed

- Release tags are now gated: only strict `vMAJOR.MINOR.PATCH` tags matching
  `localgate.__version__` can produce a GitHub Release, and every release
  first runs the complete CI quality gates (lint, mypy, pip-audit, full test
  matrix, coverage >= 90%, build, twine, clean-install smoke) plus CycloneDX
  SBOM schema validation and checksum verification before anything public is
  written. The combined coverage gate rises from 85% to 90%.
- The release SBOM is now a truthful CycloneDX 1.5 document: it lists the
  wheel's declared runtime optional dependencies (`yaml`, `pdf` extras),
  records declared constraints as properties instead of fake component
  versions, carries a valid unique UUID serial per build, validates against
  the official CycloneDX 1.5 schema, and `SHA256SUMS.txt` covers exactly the
  release attachments with a self-verifying format.
- Packaging metadata migrates to PEP 639 (SPDX `License-Expression: MIT`,
  `license-files`); the deprecated license classifier is gone and builds no
  longer emit license deprecation warnings.
- The Code of Conduct enforcement contact is now concrete (maintainer on
  GitHub), explicitly separated from the private security reporting channel.

## [1.0.0] - 2026-09-25

First public release.

### Added

- Privacy-first local retrieval gateway: whitelist-scoped indexing of
  Markdown, plain text, source code, PDF, DOCX, chat-export JSON and images
  (local OCR).
- Hybrid search engine (BM25 full-text + hashed vector embeddings, weighted
  fusion) with graceful degradation to full-text when the embedding backend
  is unavailable.
- Loopback-only HTTP API with Host/Origin validation, Content-Type checks and
  bounded request bodies, queries and log reads.
- Native stdio MCP server (JSON-RPC 2.0) speaking protocol revisions
  2024-11-05, 2025-03-26 and 2025-06-18, with `localgate_search`,
  `localgate_status` and `localgate_get_document` tools.
- Config hardening: strict loopback-only validation for the Ollama URL
  (DNS-rebinding-safe) and for the MCP gateway URL; relative paths always
  resolve against the config file's directory.
- Parser resource limits for DOCX/PDF/JSON/text (zip-bomb guards, page and
  byte caps, output truncation) with per-file failure isolation.
- Incremental file watcher with rescan that works (and reports honest state)
  whether the watcher is enabled or not.
- Self-check daemon with seven check items, safe automatic repairs, JSONL
  structured logs, backoff on consecutive errors and conservative stale-file
  handling (an unavailable volume never mass-clears the index).
- Standard packaging (`pyproject.toml`, console script `localgate`), CI
  matrix (Linux/macOS/Windows, Python 3.10/3.13), CodeQL, Dependabot,
  dependency review, release workflow with SBOM and SHA-256 checksums.

[Unreleased]: https://github.com/omgamy1179-dev/localgate/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/omgamy1179-dev/localgate/releases/tag/v1.0.0
