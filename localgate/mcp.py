"""Native MCP server over stdio (newline-delimited JSON-RPC 2.0).

This process is launched on demand by an MCP client (Claude Code, etc.) and
proxies tool calls to the LocalGate HTTP API on loopback. It holds no index
itself, so multiple agent sessions can share one running gateway.

Run:  python main.py mcp [--config config.yaml] [--api http://127.0.0.1:8770]

Claude Code registration example (claude_desktop_config.json / .mcp.json):
  {"mcpServers": {"localgate": {"command": "python3",
                                "args": ["/path/to/LocalGate/main.py", "mcp"]}}}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from . import __version__
from .httpclient import read_upstream_json, urlopen_noproxy, validate_loopback_http_url

# search responses are text snippets; anything larger means a broken gateway
_API_MAX_BYTES = 32 * 1024 * 1024

# MCP protocol revisions this server speaks (newest first). On initialize the
# server echoes the client's requested version when supported, otherwise it
# replies with LATEST_PROTOCOL_VERSION so the client can decide.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

TOOLS = [
    {
        "name": "localgate_search",
        "description": (
            "Hybrid (full-text + vector) search over the user's local indexed "
            "files: notes, PDFs, docx, source code, chat exports, OCR'd images. "
            "All data stays on this machine."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search query"},
                "top_k": {"type": "integer", "description": "max results (default from config)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "localgate_status",
        "description": "Index health: doc/chunk counts, self-check status, whitelist.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "localgate_get_document",
        "description": "Fetch one indexed document's metadata and chunk texts by doc_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
        },
    },
]


class ApiClient:
    def __init__(self, base_url: str, timeout_s: float = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, body: dict | None = None):
        url = self.base_url + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urlopen_noproxy(req, timeout=self.timeout_s) as resp:
            return resp.status, read_upstream_json(resp, timeout_s=self.timeout_s,
                                                   max_bytes=_API_MAX_BYTES)

    def search(self, params: dict):
        body = {"query": params.get("query")}
        if params.get("top_k") is not None:
            body["top_k"] = params["top_k"]
        return self._call("POST", "/api/search", body)

    def status(self):
        return self._call("GET", "/api/status")

    def document(self, doc_id: str):
        return self._call("GET", f"/api/document/{doc_id}")


def _format_search(result: dict) -> str:
    note = " (vector backend degraded; full-text only)" if result.get("degraded") else ""
    results = result.get("results", [])
    if not results:
        return f"No local results for: {result.get('query', '')}{note}"
    lines = [f"LocalGate search: {len(results)} result(s) for '{result.get('query', '')}'{note}"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n[{i}] score={r.get('score')} path={r.get('path')}")
        lines.append(f"    {r.get('snippet', '')}".replace("\n", " "))
    return "\n".join(lines)


class McpStdioServer:
    def __init__(self, api: ApiClient):
        self.api = api

    def _reply(self, msg_id, result=None, error=None) -> dict:
        resp: dict = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            resp["error"] = error
        else:
            resp["result"] = result
        return resp

    def _handle_call(self, msg: dict) -> dict | None:
        if not isinstance(msg, dict):
            return self._reply(None, error={"code": -32600,
                                            "message": "request must be an object"})
        method = msg.get("method", "")
        msg_id = msg.get("id")

        if method == "initialize":
            params = msg.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                return self._reply(msg_id, error={
                    "code": -32602, "message": "initialize params must be an object"})
            client_version = params.get("protocolVersion")
            negotiated = client_version if (
                isinstance(client_version, str)
                and client_version in PROTOCOL_VERSIONS) else LATEST_PROTOCOL_VERSION
            return self._reply(msg_id, result={
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "localgate", "version": __version__},
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._reply(msg_id, result={})
        if method == "tools/list":
            return self._reply(msg_id, result={"tools": TOOLS})
        if method == "tools/call":
            params = msg.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return self._reply(msg_id, error={
                    "code": -32602,
                    "message": "tools/call params must include a tool name string"})
            args = params.get("arguments")
            if args is not None and not isinstance(args, dict):
                return self._reply(msg_id, error={
                    "code": -32602, "message": "tool arguments must be an object"})
            return self._reply(msg_id, result=self._tools_call(params))
        if method in ("prompts/list", "resources/list"):
            key = "prompts" if method == "prompts/list" else "resources"
            return self._reply(msg_id, result={key: []})
        if method.startswith("notifications/"):
            return None
        return self._reply(msg_id, error={"code": -32601,
                                          "message": f"method not found: {method}"})

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "localgate_search":
                if not isinstance(args.get("query"), str) or not args["query"].strip():
                    return _tool_error("'query' (non-empty string) is required")
                _status, body = self.api.search(args)
                return {"content": [{"type": "text", "text": _format_search(body)}],
                        "isError": False}
            if name == "localgate_status":
                _status, st = self.api.status()
                idx, sc = st.get("index", {}), st.get("selfcheck", {})
                wl = st.get("config", {}).get("paths", {}).get("whitelist", [])
                text = (
                    f"LocalGate index: {idx.get('docs', 0)} docs, {idx.get('chunks', 0)} chunks, "
                    f"{idx.get('vectors', 0)} vectors, {idx.get('index_size_bytes', 0)} bytes\n"
                    f"Self-check: round {sc.get('rounds', 0)}, "
                    f"last status {sc.get('last_status')}\n"
                    f"Whitelist: {', '.join(wl) if wl else '(empty - nothing indexed)'}")
                return {"content": [{"type": "text", "text": text}], "isError": False}
            if name == "localgate_get_document":
                doc_id = args.get("doc_id")
                if not isinstance(doc_id, str) or not doc_id:
                    return _tool_error("'doc_id' is required")
                try:
                    _status, doc = self.api.document(doc_id)
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        return _tool_error(f"document not found: {doc_id}")
                    raise
                d = doc.get("doc", {})
                chunks = doc.get("chunks", [])
                lines = [f"Document {doc_id}: path={d.get('path')} kind={d.get('kind')} "
                         f"status={d.get('status')} chunks={len(chunks)}"]
                for c in chunks[:20]:
                    lines.append(f"\n--- chunk {c.get('ordinal')} ---\n{c.get('text', '')}")
                return {"content": [{"type": "text", "text": "\n".join(lines)}],
                        "isError": False}
            return _tool_error(f"unknown tool: {name}")
        except urllib.error.URLError as e:
            return _tool_error(
                f"LocalGate gateway not reachable at {self.api.base_url} "
                f"({e}). Start it with: python main.py serve")
        except Exception as e:
            return _tool_error(f"{type(e).__name__}: {e}")

    def serve_forever(self, lines=None) -> int:
        # unbuffered line protocol over stdin/stdout; `lines` is injectable
        # for tests (defaults to sys.stdin)
        for raw in (sys.stdin if lines is None else lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                _write_msg(self._reply(None, error={"code": -32700,
                                                    "message": "parse error"}))
                continue
            # JSON-RPC batch: a list of requests/notifications
            batch = parsed if isinstance(parsed, list) else [parsed]
            responses: list[dict] = []
            for item in batch:
                resp: dict | None = None
                try:
                    resp = self._handle_call(item)
                except Exception as e:
                    item_id = item.get("id") if isinstance(item, dict) else None
                    resp = self._reply(item_id, error={
                        "code": -32603, "message": f"internal error: {e}"})
                if resp is not None:
                    responses.append(resp)
            for resp in responses:
                _write_msg(resp)
        return 0


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _write_msg(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def run_mcp_server(api_base: str | None = None, config_path: str | None = None,
                   lines=None) -> int:
    if api_base is None:
        api_base = os.environ.get("LOCALGATE_API")
    if api_base is None:
        try:
            from .config import load_config
            cfg = load_config(config_path)
            api_base = f"http://{cfg['server']['host']}:{cfg['server']['port']}"
        except Exception:
            api_base = "http://127.0.0.1:8770"
    # privacy boundary: index-derived data only ever flows to this machine
    try:
        api_base = validate_loopback_http_url(api_base, name="gateway url")
    except ValueError as e:
        print(f"[localgate] refusing non-loopback gateway url: {e}", file=sys.stderr)
        return 2
    server = McpStdioServer(ApiClient(api_base))
    return server.serve_forever(lines)
