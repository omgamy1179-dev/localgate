# Support

## Where to ask

- **Questions / setup help**: [GitHub Discussions](https://github.com/omgamy1179-dev/localgate/discussions)
- **Bug reports**: [GitHub Issues](https://github.com/omgamy1179-dev/localgate/issues) (use the bug template)
- **Security issues**: private vulnerability reporting via the
  [Security tab](https://github.com/omgamy1179-dev/localgate/security/advisories/new) —
  see [SECURITY.md](SECURITY.md). Never open a public issue for these.

This is a community-maintained open-source project; support is provided on a
best-effort basis.

## What is supported

| Scope | Support |
| --- | --- |
| Latest tagged release | bug fixes and security fixes |
| `main` branch | best-effort review |
| Older releases | no; please upgrade |

Platforms: Linux, macOS and Windows with Python 3.10+. The runtime core uses
only the Python standard library; `pyyaml` and `pypdf` are optional extras.

## Before asking

1. Check that your `config.yaml` loads (`localgate index -c config.yaml`
   prints a config error, not a traceback, when something is wrong).
2. Look at `GET /api/status` and the structured logs (`GET
   /api/logs/service`) — they are designed to explain failures without
   exposing file content.
3. Search existing issues and discussions for your error message.

## What we cannot help with

- Indexing content you do not own or have no right to process.
- Bypassing the privacy boundaries (non-loopback embedding endpoints,
  non-loopback binds, extracting other users' data).
- Debugging third-party MCP clients, Ollama installations or OCR engine
  builds beyond what the README documents.
