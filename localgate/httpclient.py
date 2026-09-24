"""HTTP helpers for LocalGate's own loopback calls (no proxies, bounded reads).

Three guarantees:
1. Proxy env vars never intercept loopback traffic (a dead proxy must not
   break self-ping or search).
2. Upstream reads have a TOTAL deadline and a size cap. urllib's `timeout=`
   only bounds a single socket read - a server trickling bytes would keep a
   reader blocked forever. A watchdog shuts the socket down once the deadline
   passes, aborting the blocked read, and the body is capped at max_bytes.
3. `validate_loopback_http_url()` is the single gate for every outbound URL
   LocalGate is configured with (Ollama endpoint, gateway base for MCP).
   Data must never leave the machine, so a URL is accepted only if its host
   resolves to loopback addresses exclusively - and the returned URL is
   rewritten to the resolved literal IP so a later DNS re-resolution
   (rebinding) cannot redirect the traffic.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import urllib.parse
import urllib.request


# an opener with an empty ProxyHandler (bypasses env proxies) and redirects
# disabled: a compromised local "ollama" must not be able to bounce our
# embedding payloads to a remote host via an HTTP redirect
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect)

_READ_CHUNK = 65536


def urlopen_noproxy(req: urllib.request.Request | str, timeout: float):
    """urlopen() equivalent that ignores proxy environment variables."""
    return _NO_PROXY_OPENER.open(req, timeout=timeout)


def _is_loopback_host_literal(hostname: str) -> bool | None:
    """True/False if hostname is directly an IP literal, None if it is a name."""
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return None


def _resolve_addrs(hostname: str) -> list[str]:
    """All addresses `hostname` currently resolves to (strings)."""
    infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    addrs: list[str] = []
    for ai in infos:
        addr = str(ai[4][0])
        # strip IPv6 scope/zone id ("fe80::1%en0") - zone adds nothing here
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        addrs.append(addr)
    return addrs


def validate_loopback_http_url(raw: str, *, name: str = "url") -> str:
    """Validate that `raw` is an http:// URL on this machine only.

    Strictly enforced:
    - scheme is exactly `http` (no https, no other schemes);
    - no userinfo, no fragment, no query, no whitespace/backslash tricks,
      path empty or "/" only;
    - a legal explicit port if present;
    - the host resolves (right now) exclusively to loopback addresses -
      a name resolving to any non-loopback address is rejected (fail closed);
    - the returned URL has the host replaced by the resolved literal IP, so
      a later DNS re-resolution cannot redirect the connection elsewhere.

    Raises ValueError with a message safe to show users.
    """
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be a string")
    url = raw.strip()
    if not url:
        raise ValueError(f"{name} is empty")
    if any(c.isspace() for c in url) or "\\" in url or "<" in url or ">" in url:
        raise ValueError(f"{name} contains forbidden characters")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "http":
        raise ValueError(f"{name} scheme must be http, got {parts.scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{name} must not contain userinfo (user:pass@host)")
    if parts.fragment:
        raise ValueError(f"{name} must not contain a fragment (#)")
    if parts.query:
        raise ValueError(f"{name} must not contain a query string (?)")
    if parts.path not in ("", "/"):
        raise ValueError(f"{name} path must be empty, got {parts.path!r}")
    hostname = (parts.hostname or "").strip()
    if not hostname:
        raise ValueError(f"{name} has no host")
    hostname = hostname.rstrip(".")
    if not hostname:
        raise ValueError(f"{name} host is empty")
    try:
        port = parts.port  # raises ValueError for illegal ports
    except ValueError as e:
        raise ValueError(f"{name} has an invalid port: {e}") from None
    if port is not None and not (1 <= port <= 65535):
        raise ValueError(f"{name} port out of range: {port}")

    literal = _is_loopback_host_literal(hostname)
    try:
        addrs = [hostname] if literal is not None else _resolve_addrs(hostname)
    except OSError as e:
        raise ValueError(f"{name} host {hostname!r} could not be resolved: {e}") from None
    if not addrs:
        raise ValueError(f"{name} host {hostname!r} resolved to no addresses")
    loopbacks: list[str] = []
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as e:
            raise ValueError(f"{name} host resolved to a non-IP address: {addr!r}") from e
        if not ip.is_loopback:
            raise ValueError(
                f"{name} host {hostname!r} resolves to non-loopback address {addr}; "
                "only loopback endpoints are allowed")
        loopbacks.append(addr)
    # deterministic rewrite to the resolved literal IP (IPv4 preferred for
    # maximum compatibility with local services)
    chosen = next((a for a in loopbacks if ":" not in a), loopbacks[0])
    host_out = f"[{chosen}]" if ":" in chosen else chosen
    out = f"http://{host_out}" + (f":{port}" if port is not None else "")
    return out


def _find_socket(resp) -> socket.socket | None:
    """Best-effort extraction of the underlying socket from a urllib response."""
    try:
        return resp.fp.raw._sock  # CPython http.client -> BufferedReader -> SocketIO
    except (AttributeError, OSError):
        return None


def read_upstream_text(resp, *, timeout_s: float, max_bytes: int,
                       encoding: str = "utf-8", errors: str = "strict") -> str:
    """Read the whole response body with a total deadline and size cap."""
    sock = _find_socket(resp)
    expired = threading.Event()

    def _abort() -> None:
        expired.set()
        if sock is not None:
            try:
                # SHUT_RDWR reliably wakes a recv() blocked in another thread
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            resp.close()
        except Exception:
            pass

    watchdog = threading.Timer(timeout_s, _abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"upstream body exceeds cap of {max_bytes} bytes")
            chunks.append(chunk)
        if expired.is_set():
            raise TimeoutError(f"upstream read exceeded {timeout_s}s total deadline")
        return b"".join(chunks).decode(encoding, errors)
    except (OSError, ValueError) as e:
        if expired.is_set():
            raise TimeoutError(f"upstream read exceeded {timeout_s}s total deadline") from e
        raise
    finally:
        watchdog.cancel()


def read_upstream_json(resp, *, timeout_s: float, max_bytes: int) -> dict:
    """read_upstream_text + JSON parse."""
    return json.loads(read_upstream_text(resp, timeout_s=timeout_s, max_bytes=max_bytes))
