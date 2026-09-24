"""LocalGate CLI (also exposed as the installed `localgate` console script).

  localgate serve  [--config config.yaml]      run the gateway (HTTP API +
                                               watcher + self-check daemon)
  localgate mcp    [--config ...] [--api URL]  run the stdio MCP server
  localgate index  [--config ...]              one-shot whitelist scan, exit
  localgate status [--config ...] [--api URL]  print gateway status JSON

Hard rules: read-only against user files; loops back to 127.0.0.1 only;
no privilege escalation; nothing is indexed without an explicit whitelist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__


def _cfg(path: str | None) -> dict:
    from .config import ConfigError, load_config
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"[localgate] config error: {e}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_serve(args) -> int:
    from .service import LocalGateService
    cfg = _cfg(args.config)
    svc = LocalGateService(cfg)
    svc.install_signal_handlers()
    svc.start()
    svc.wait_forever()
    return 0


def cmd_mcp(args) -> int:
    from .mcp import run_mcp_server
    return run_mcp_server(api_base=args.api, config_path=args.config)


def cmd_index(args) -> int:
    from .embedding import make_embedder
    from .ingest import Ingestor, IngestProgress
    from .ocr import OcrEngine
    from .store import IndexStore
    cfg = _cfg(args.config)
    if not cfg["paths"]["whitelist"]:
        print("[localgate] whitelist is empty; nothing to index.", file=sys.stderr)
        return 1
    # one-shot indexing: build the pipeline only, no watcher/HTTP/self-check
    store = IndexStore(cfg["index"]["data_dir"])
    embedder = make_embedder(cfg)
    ocr = OcrEngine(mode=cfg["ocr"]["mode"], languages=cfg["ocr"]["languages"],
                    timeout_s=cfg["ocr"]["timeout_s"],
                    helper_cache_dir=os.path.join(cfg["index"]["data_dir"], "bin"))
    ingestor = Ingestor(cfg, store, embedder, ocr, IngestProgress(),
                        protected_dirs=[cfg["index"]["data_dir"], cfg["logs"]["dir"]])
    summary = ingestor.scan_whitelist(reason="cli-one-shot")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args) -> int:
    from .httpclient import urlopen_noproxy
    cfg = _cfg(args.config)
    base = args.api or f"http://{cfg['server']['host']}:{cfg['server']['port']}"
    try:
        with urlopen_noproxy(base + "/api/status", timeout=5) as resp:
            print(json.dumps(json.loads(resp.read().decode("utf-8")),
                             ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(f"[localgate] gateway not reachable at {base}: {e}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="localgate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"localgate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--config", "-c", default=None,
                       help="path to config.yaml (default: ./config.yaml)")

    p_serve = sub.add_parser("serve", help="run the gateway service")
    add_common(p_serve)
    p_serve.set_defaults(func=cmd_serve)

    p_mcp = sub.add_parser("mcp", help="run the stdio MCP server")
    add_common(p_mcp)
    p_mcp.add_argument("--api", default=None,
                       help="gateway base URL (default: from config or 127.0.0.1:8770)")
    p_mcp.set_defaults(func=cmd_mcp)

    p_index = sub.add_parser("index", help="one-shot whitelist scan")
    add_common(p_index)
    p_index.set_defaults(func=cmd_index)

    p_status = sub.add_parser("status", help="print gateway status JSON")
    add_common(p_status)
    p_status.add_argument("--api", default=None, help="gateway base URL override")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
