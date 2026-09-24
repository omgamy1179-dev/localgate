# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
