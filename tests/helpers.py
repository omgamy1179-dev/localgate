"""Shared test helpers: dynamic ports, raw HTTP, in-process service factory.

Everything uses temporary directories and free loopback ports; nothing here
depends on developer-machine paths or pre-existing indexes."""

from __future__ import annotations

import json
import os
import socket
import tempfile

from localgate.config import DEFAULTS, _deep_merge


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_port_v6() -> int:
    s = socket.socket(socket.AF_INET6)
    s.bind(("::1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_config(path: str, cfg: dict) -> str:
    import yaml
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    return path


def make_cfg(workdir: str, port: int | None = None, **overrides) -> dict:
    """A full, valid config dict rooted in workdir (merged over DEFAULTS)."""
    cfg = _deep_merge(DEFAULTS, {
        "server": {"port": port or free_port(), "read_timeout_s": 3},
        "index": {"data_dir": os.path.join(workdir, "data")},
        "paths": {"whitelist": [], "blacklist": []},
        "ocr": {"mode": "off"},  # unit tests never exercise OCR; avoids
                                 # compiling the Vision helper on CI runners
        "watcher": {"enabled": False, "interval_s": 1},
        "selfcheck": {"enabled": False, "interval_s": 60, "item_delay_ms": 0},
        "logs": {"dir": os.path.join(workdir, "logs")},
    })
    cfg = _deep_merge(cfg, overrides)
    return cfg


def build_service(workdir: str, **overrides):
    """In-process LocalGateService (nothing started except what the test asks)."""
    from localgate.service import LocalGateService
    return LocalGateService(make_cfg(workdir, **overrides))


def raw_http(port: int, request: bytes, timeout: float = 8.0,
             host: str = "127.0.0.1") -> tuple[int, bytes]:
    """Send one raw request; return (status_line_code, full response bytes)."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(request)
        chunks = []
        while True:
            try:
                chunk = s.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    data = b"".join(chunks)
    head = data.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    parts = head.split(" ")
    code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    return code, data


def json_request(port: int, method: str, path: str, body: dict | None = None,
                 headers: dict | None = None,
                 host: str = "127.0.0.1") -> tuple[int, dict | str]:
    """JSON request through a real socket with an explicit Host header."""
    payload = None if body is None else json.dumps(body).encode()
    hdrs = {"Host": f"{host}:{port}", "Connection": "close"}
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(payload))
    hdrs.update(headers or {})
    req = f"{method} {path} HTTP/1.1\r\n".encode() + "".join(
        f"{k}: {v}\r\n" for k, v in hdrs.items()).encode() + b"\r\n"
    if payload:
        req += payload
    code, data = raw_http(port, req, host=host)
    _, _, rest = data.partition(b"\r\n\r\n")
    try:
        parsed = json.loads(rest.decode("utf-8"))
    except ValueError:
        parsed = rest.decode("utf-8", errors="replace")
    return code, parsed


class ServiceTestCase:
    """Mixin: temp dir + service per test."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workdir = self._td.name
        self.addCleanup(self._td.cleanup)
