"""HTTP API + minimal debug panel (stdlib only, binds loopback by default).

Endpoints:
  GET  /                      -> minimal read-only status panel (HTML)
  GET  /health                -> liveness for self-check / agents
  GET  /api/status            -> index, watcher, self-check, config summary
  POST /api/search            -> {"query": str, "top_k"?: int, ...}
  GET  /api/document/{id}     -> doc metadata + chunks
  GET  /api/logs/selfcheck    -> recent self-check JSONL entries
  GET  /api/logs/service      -> recent service JSONL entries
  POST /api/rescan            -> trigger one whitelist scan now

Local attack surface hardening:
- Host header must be loopback (blocks DNS-rebinding pages that resolve a
  public name at 127.0.0.1 and then read responses from this port);
- Origin, when sent, must itself be loopback (blocks cross-origin reads and
  cross-site form/fetch POSTs from untrusted pages);
- POST JSON endpoints require Content-Type: application/json;
- request bodies, query strings and numeric parameters are tightly bounded;
- internal errors return a generic message; the detailed traceback goes only
  into the local structured service log.

Hard rules honored: read-only, no user file content in responses beyond search
snippets the user's own index produced; panel shows status/progress/errors only.
"""

from __future__ import annotations

import html
import json
import re
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MAX_BODY_BYTES = 256 * 1024
MAX_BODY_DRAIN_BYTES = 8 * 1024 * 1024
MAX_QUERY_CHARS = 8192
MAX_LOG_LINES = 1000
DEFAULT_LOG_LINES = 50

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class RequestTimeoutError(Exception):
    """Client sent headers but stalled mid-body; the handler must not pin a thread."""


def _host_of(host_header: str) -> str:
    """Hostname part of a Host/Origin authority, lowercased; IPv6 keeps no brackets."""
    authority = (host_header or "").strip().lower()
    if authority.startswith("["):
        return authority.split("]", 1)[0].lstrip("[") or authority
    return authority.rsplit(":", 1)[0] if ":" in authority else authority


class ApiServer:
    def __init__(self, service):
        self.service = service
        host = service.cfg["server"]["host"]
        port = int(service.cfg["server"]["port"])
        self.host, self.port = host, port
        self.read_timeout_s = max(1, int(service.cfg["server"]["read_timeout_s"]))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # bounds idle keep-alive connections AND stalled request bodies:
            # without it a client that promises N bytes and sends none pins a
            # handler thread forever
            timeout = self.read_timeout_s
            server_version = "LocalGate"
            sys_version = ""

            def log_message(self, fmt, *args):  # silence default stderr spam
                pass

            # ---------------- security gates

            def _check_host(self) -> bool:
                """Only loopback Host headers are served (DNS rebinding guard)."""
                host_header = self.headers.get("Host") or ""
                if not host_header:
                    self._api_error(400, "missing Host header")
                    return False
                if _host_of(host_header) not in _LOOPBACK_HOSTS:
                    self._api_error(403, "this API only serves loopback Host headers")
                    return False
                return True

            def _check_origin(self) -> bool:
                """A present Origin must be loopback (cross-site request guard)."""
                origin = (self.headers.get("Origin") or "").strip()
                if not origin or origin == "null":
                    if origin == "null":
                        self._api_error(403, "origin not allowed")
                        return False
                    return True
                try:
                    parts = urlparse(origin)
                except ValueError:
                    self._api_error(403, "origin not allowed")
                    return False
                if parts.scheme != "http" or _host_of(parts.netloc) not in _LOOPBACK_HOSTS:
                    self._api_error(403, "origin not allowed")
                    return False
                return True

            def _gates(self) -> bool:
                return self._check_host() and self._check_origin()

            # ---------------- helpers
            def _send_json(self, obj, status: int = 200) -> None:
                try:
                    data = json.dumps(obj, ensure_ascii=False, default=str) \
                        .encode("utf-8")
                except Exception:
                    # a serialisation failure must never leave a connection
                    # closed without any HTTP response
                    data = b'{"error": "internal server error"}'
                    status = 500
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

            def _send_html(self, body: str, status: int = 200) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

            def _read_json_body(self) -> dict:
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0] \
                    .strip().lower()
                if ctype != "application/json":
                    raise NotJsonContentType()
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                if length > MAX_BODY_BYTES:
                    # drain a bounded amount so well-behaved clients can still
                    # read our 400 instead of dying on a TCP reset; beyond the
                    # drain cap the reset is exactly the protection we want
                    remaining = min(length, MAX_BODY_DRAIN_BYTES)
                    try:
                        while remaining > 0:
                            chunk = self.rfile.read(min(65536, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                    except TimeoutError as e:
                        raise RequestTimeoutError() from e
                    raise ValueError(
                        f"request body too large (>{MAX_BODY_BYTES} bytes)")
                try:
                    raw = self.rfile.read(length)
                except TimeoutError as e:  # socket.timeout: client stalled
                    raise RequestTimeoutError() from e
                if not raw.strip():
                    return {}
                obj = json.loads(raw.decode("utf-8"))
                if not isinstance(obj, dict):
                    raise ValueError("request body must be a JSON object")
                return obj

            def _api_error(self, status: int, message: str) -> None:
                self._send_json({"error": message}, status)

            def _log_internal(self, exc: Exception) -> None:
                """Full detail goes to the local structured log only."""
                try:
                    outer.service.service_log.write({
                        "timestamp": _now_iso(),
                        "event": "http_internal_error",
                        "client": self.client_address[0],
                        "method": self.command,
                        "path": self.path[:500],
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc()[-2000:],
                    })
                except Exception:
                    pass

            # ---------------- routes
            def do_GET(self):
                try:
                    if not self._gates():
                        return
                    parsed = urlparse(self.path)
                    path = parsed.path
                    qs = urllib.parse.parse_qs(parsed.query)
                    svc = outer.service
                    if path == "/health":
                        self._send_json({"status": "ok", "service": "localgate",
                                         "version": svc.version,
                                         "uptime_s": round(svc.uptime(), 1)})
                    elif path == "/api/status":
                        self._send_json(svc.status_snapshot())
                    elif path == "/":
                        self._send_html(render_panel(svc))
                    elif path == "/api/logs/selfcheck":
                        self._serve_log(svc.selfcheck_log, qs)
                    elif path == "/api/logs/service":
                        self._serve_log(svc.service_log, qs)
                    elif path == "/api/config":
                        self._send_json(svc.public_config())
                    else:
                        m = re.fullmatch(r"/api/document/([0-9a-f]{1,64})", path)
                        if m:
                            doc = svc.store.get_document(m.group(1))
                            if doc is None:
                                self._api_error(404, "document not found")
                            else:
                                self._send_json(doc)
                        else:
                            self._api_error(404, "no such endpoint")
                except BrokenPipeError:
                    pass
                except Exception as e:
                    self._log_internal(e)
                    try:
                        self._api_error(500, "internal server error")
                    except Exception:
                        pass

            def _serve_log(self, logger, qs: dict) -> None:
                raw = (qs.get("lines") or [str(DEFAULT_LOG_LINES)])[0]
                try:
                    lines = int(raw)
                except ValueError:
                    self._api_error(400, "'lines' must be an integer")
                    return
                if not (1 <= lines <= MAX_LOG_LINES):
                    self._api_error(400, f"'lines' must be within [1, {MAX_LOG_LINES}]")
                    return
                self._send_json({"entries": logger.read_recent(lines)})

            def _read_body(self) -> tuple[dict | None, bool]:
                """Read+validate the JSON body. Returns (body, False) on
                success; on failure sends the error response and returns
                (None, True)."""
                try:
                    return self._read_json_body(), False
                except RequestTimeoutError:
                    self.close_connection = True
                    self._api_error(408, "request body stalled before completion")
                except NotJsonContentType:
                    self.close_connection = True
                    self._api_error(415, "Content-Type must be application/json")
                except UnicodeDecodeError:
                    self._api_error(400, "request body must be UTF-8 JSON")
                except ValueError as e:  # includes json.JSONDecodeError
                    self._api_error(400, str(e))
                return None, True

            def do_POST(self):
                try:
                    if not self._gates():
                        return
                    path = urlparse(self.path).path
                    svc = outer.service
                    if path == "/api/search":
                        body, err = self._read_body()
                        if err or body is None:
                            return
                        query = body.get("query")
                        if not isinstance(query, str) or not query.strip():
                            self._api_error(400, "field 'query' (non-empty string) is required")
                            return
                        if len(query) > MAX_QUERY_CHARS:
                            self._api_error(400,
                                            f"'query' exceeds {MAX_QUERY_CHARS} characters")
                            return
                        top_k = body.get("top_k")
                        if top_k is not None and (not isinstance(top_k, int)
                                                  or isinstance(top_k, bool) or top_k < 1):
                            self._api_error(400, "'top_k' must be a positive integer")
                            return
                        fw = body.get("fulltext_weight")
                        vw = body.get("vector_weight")
                        for name, v in (("fulltext_weight", fw), ("vector_weight", vw)):
                            if v is not None and (isinstance(v, bool)
                                                  or not isinstance(v, (int, float))
                                                  or not 0 <= float(v) <= 1):
                                self._api_error(400, f"'{name}' must be within [0, 1]")
                                return
                        result = svc.searcher.search(
                            query.strip(), top_k=top_k,
                            fulltext_weight=None if fw is None else float(fw),
                            vector_weight=None if vw is None else float(vw))
                        self._send_json(result)
                    elif path == "/api/rescan":
                        if (self.headers.get("Content-Length") or "0").strip() not in \
                                ("", "0"):
                            _body, err = self._read_body()
                            if err:
                                return
                        self._send_json(svc.request_rescan())
                    else:
                        self._api_error(404, "no such endpoint")
                except BrokenPipeError:
                    pass
                except Exception as e:
                    self._log_internal(e)
                    try:
                        self._api_error(500, "internal server error")
                    except Exception:
                        pass

        self._server = self._make_server(Handler)
        self._server.daemon_threads = True
        self._serving = False

    def _make_server(self, handler) -> ThreadingHTTPServer:
        import socket

        class Server(ThreadingHTTPServer):
            # AF_INET6 for IPv6 loopback literals; explicit single-stack bind
            # (never a wildcard) keeps the loopback-only guarantee
            address_family = socket.AF_INET6 if ":" in self.host else socket.AF_INET

        return Server((self.host, self.port), handler)

    def start(self) -> None:
        if self._serving:
            return
        t = threading.Thread(target=self._serve_forever_guarded,
                             name="localgate-http", daemon=True)
        t.start()

    def _serve_forever_guarded(self) -> None:
        # mark serving BEFORE the accept loop so stop() after start() never
        # calls shutdown() on a server that never entered serve_forever
        # (shutdown() would block forever)
        self._serving = True
        try:
            self._server.serve_forever()
        finally:
            self._serving = False
            try:
                self._server.server_close()
            except OSError:
                pass

    def stop(self) -> None:
        if not self._serving:
            return
        try:
            self._server.shutdown()
        except OSError:
            pass

    @property
    def base_url(self) -> str:
        host = self.host
        if host == "localhost":
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"


class NotJsonContentType(Exception):
    pass


def render_panel(svc) -> str:
    # every dynamic value is HTML-escaped: file paths, log strings and error
    # messages can contain < > & " (a file may literally be named
    # "<script>alert(1).md") and must never land raw in the page
    def esc(v) -> str:
        return html.escape(str(v), quote=True)

    st = svc.status_snapshot()
    sc = st.get("selfcheck", {})
    idx = st.get("index", {})
    ing = st.get("ingest", {})
    ingest_rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in ing.items()
        if k != "recent_errors")
    index_rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in idx.items())
    sc_rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in sc.items())
    errors = "".join(f"<li>{esc(e)}</li>" for e in ing.get("recent_errors", [])) \
        or "<li>none</li>"
    whitelist = "".join(f"<li>{esc(w)}</li>" for w in st.get("config", {}).get(
        "paths", {}).get("whitelist", [])) or "<li>(empty: nothing is indexed)</li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>LocalGate status</title>
<meta http-equiv="refresh" content="5">
<style>
 body {{ font-family: -apple-system, sans-serif; margin: 2rem; background: #fafafa; }}
 h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.5rem; }}
 table {{ border-collapse: collapse; }} td, th {{ border: 1px solid #ddd; padding: 4px 10px;
   font-size: 0.85rem; text-align: left; }}
 .ok {{ color: #1a7f37; font-weight: 600; }} .warn {{ color: #9a6700; font-weight: 600; }}
 .err {{ color: #cf222e; font-weight: 600; }}
 ul {{ font-size: 0.85rem; }}
</style></head><body>
<h1>LocalGate gateway status <span class="{_cls(sc.get('last_status'))}">{esc(sc.get('last_status') or 'pending')}</span></h1>
<p>Version {esc(st.get('version'))} · uptime {esc(st.get('uptime_s'))}s · self-check round {esc(sc.get('rounds', 0))}</p>
<h2>Whitelist (user-configured; nothing is scanned without it)</h2><ul>{whitelist}</ul>
<h2>Index</h2><table>{index_rows}</table>
<h2>Ingest / watcher progress</h2><table>{ingest_rows}</table>
<h2>Self-check</h2><table>{sc_rows}</table>
<h2>Recent ingest errors</h2><ul>{errors}</ul>
<p style="font-size:0.75rem;color:#777">Read-only status panel. LocalGate never
edits user files and never sends data off this machine.</p>
</body></html>"""


def _cls(status: str | None) -> str:
    return {"ok": "ok", "warning": "warn", "error": "err"}.get(status or "", "warn")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
