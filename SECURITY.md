# Security Policy

LocalGate is a privacy-first tool: its entire premise is that your file
content, chunks, vectors and index data never leave your machine. Reports that
show data flowing to a non-loopback destination are treated as critical.

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## How to report a vulnerability

**Please do NOT open a public issue for security problems.**

Preferred channel: **GitHub private vulnerability reporting** — open the
repository → *Security* tab → *Report a vulnerability*. This is private
between you and the maintainers.

> Maintainer note (pre-release checklist): private vulnerability reporting is
> enabled in the repository's Settings → Code security. Until it is confirmed
> active, if you cannot use the Security tab, contact the repository owner
> directly through GitHub and mark the message as security-sensitive.

## What to include

- Affected version (`localgate --version`) and platform
- A minimal reproduction (config snippet, request, or file that triggers it)
- Which privacy boundary or security invariant is affected, for example:
  - data sent to any address that is not loopback (`127.0.0.1`, `::1`)
  - the HTTP API reading files outside the configured whitelist
  - Host/Origin/Content-Type bypasses on the local API
  - crashes or unbounded resource use from crafted inputs (zip bombs, etc.)

## Response expectations

- Acknowledgement: within **7 days**
- Triage and severity assessment: within **14 days**
- Fix or mitigation for accepted issues: within **90 days** for high
  severity, sooner when exploitability is demonstrated

## Scope notes

In scope: everything under `localgate/` (HTTP API, MCP server, ingest
pipeline, config/URL validation, storage), the shipped `config.yaml`, and the
packaging/CI configuration.

Out of scope: issues that require an attacker to already execute code on the
victim's machine, or social engineering of users into editing their own
config to point at attacker-controlled hosts.
