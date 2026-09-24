"""Configuration loading: config.yaml (or config.json) merged over safe defaults.

Privacy defaults: empty whitelist -> nothing is ever indexed. Every relative
path (index.data_dir, logs.dir, whitelist/blacklist entries) resolves against
the directory that contains the config file - never against the current
working directory - so the same config behaves identically from any CWD.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from .httpclient import validate_loopback_http_url

DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8770, "read_timeout_s": 30},
    "index": {
        "data_dir": "./data",
        "max_file_mb": 20,
        "chunk_size": 500,
        "chunk_overlap": 80,
    },
    "paths": {
        "whitelist": [],
        "blacklist": [],
        "exclude_names": [".git", "node_modules", "__pycache__", ".DS_Store",
                          ".venv", "venv", "__MACOSX"],
    },
    "embedding": {
        "backend": "local",  # local | ollama
        "ollama_url": "http://127.0.0.1:11434",
        "model": "nomic-embed-text",
        "dim": 512,
        "timeout_s": 20,
    },
    "ocr": {
        "mode": "auto",  # auto | off
        "languages": ["zh-Hans", "en-US"],
        "timeout_s": 60,
    },
    "search": {
        "fulltext_weight": 0.55,
        "vector_weight": 0.45,
        "top_k": 8,
    },
    "watcher": {"enabled": True, "interval_s": 10},
    "selfcheck": {
        "enabled": True,
        "interval_s": 300,
        "item_delay_ms": 50,
        "consecutive_error_threshold": 3,
        "backoff_max_s": 1800,
        "index_size_warn_mb": 2048,
    },
    "logs": {"dir": "./logs", "max_mb": 10, "backups": 5},
}


class ConfigError(Exception):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _coerce_scalar(text: str) -> Any:
    t = text.strip()
    if not t:
        return ""
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        return t[1:-1]
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", "none"):
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    # inline list ["a", "b"]
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip() for p in inner.split(",")]
        return [_coerce_scalar(p) for p in parts if p]
    return t


def _mini_yaml_parse(text: str) -> dict:
    """Tiny fallback YAML-subset parser: nested maps, lists of scalars/maps,
    comments, quoted/int/float/bool scalars. Used only when PyYAML is missing."""

    def strip_comment(line: str) -> str:
        out, in_s = [], None
        for ch in line:
            if in_s:
                out.append(ch)
                if ch == in_s:
                    in_s = None
            elif ch in "\"'":
                in_s = ch
                out.append(ch)
            elif ch == "#":
                break
            else:
                out.append(ch)
        return "".join(out).rstrip()

    entries: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = strip_comment(raw)
        if not line.strip():
            continue
        entries.append((len(line) - len(line.lstrip(" ")), line.strip()))

    def parse_block(idx: int, indent: int):
        """Parse a block starting at entries[idx] with the given indent.
        Returns (value, next_index)."""
        if idx < len(entries) and entries[idx][1].startswith("- "):
            # list block
            items = []
            i = idx
            while i < len(entries) and entries[i][0] == indent \
                    and entries[i][1].startswith("- "):
                items.append(_coerce_scalar(entries[i][1][2:]))
                i += 1
            return items, i
        # mapping block
        out: dict = {}
        i = idx
        while i < len(entries):
            line_indent, content = entries[i]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise ConfigError(f"unexpected indent in config line: {content!r}")
            if ":" not in content:
                raise ConfigError(f"cannot parse config line: {content!r}")
            key, _, rest = content.partition(":")
            key = key.strip().strip("\"'")
            rest = rest.strip()
            if rest == "":
                if i + 1 < len(entries) and entries[i + 1][0] > line_indent:
                    value, i = parse_block(i + 1, entries[i + 1][0])
                    out[key] = value
                    continue
                out[key] = None
                i += 1
            else:
                out[key] = _coerce_scalar(rest)
                i += 1
        return out, i

    value, _ = parse_block(0, entries[0][0] if entries else 0)
    if not isinstance(value, dict):
        raise ConfigError("config root must be a mapping")
    return value


def _load_raw(config_path: str | None) -> tuple[dict, str]:
    if config_path is None:
        # shipped default config next to package root, if present
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for cand in (os.path.join(here, "config.yaml"), os.path.join(here, "config.json")):
            if os.path.exists(cand):
                config_path = cand
                break
    if config_path is None or not os.path.exists(config_path):
        return {}, "(defaults: no config file found)"
    with open(config_path, encoding="utf-8") as f:
        text = f.read()
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            return json.loads(text), config_path
        except ValueError as e:
            raise ConfigError(f"invalid JSON config {config_path}: {e}") from e
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"config root must be a mapping: {config_path}")
        return data, config_path
    except ImportError:
        try:
            return _mini_yaml_parse(text), config_path
        except ConfigError:
            raise
        except Exception as e:  # pragma: no cover
            raise ConfigError(f"failed to parse {config_path}: {e}") from e
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"failed to parse YAML config {config_path}: {e}") from e


def _as_int(v: Any, name: str, minimum: int = 0) -> int:
    try:
        iv = int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {v!r}") from None
    if iv < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {iv}")
    return iv


def _as_float(v: Any, name: str, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        fv = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number, got {v!r}") from None
    if fv < minimum or fv > maximum:
        raise ConfigError(f"{name} must be within [{minimum}, {maximum}], got {fv}")
    return fv


def _as_str_list(v: Any, name: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ConfigError(f"{name} must be a list of strings")
    return list(v)


def _resolve_path(p: str, base_dir: str) -> str:
    """Expand '~' and resolve a relative path against the config's directory.

    Absolute paths pass through unchanged; '~...' always expands to the user's
    home regardless of base_dir; everything else is base_dir-relative.
    """
    p = os.path.expanduser(str(p))
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(base_dir, p))


def load_config(config_path: str | None = None) -> dict:
    raw, src = _load_raw(config_path)
    cfg = _deep_merge(DEFAULTS, raw)

    cfg["_config_source"] = src

    srv = cfg["server"]
    srv["host"] = str(srv["host"]).strip()
    if srv["host"] not in ("127.0.0.1", "localhost", "::1"):
        # privacy: only loopback binds make sense for this gateway
        raise ConfigError(f"server.host must be a loopback address, got {srv['host']!r}")
    srv["port"] = _as_int(srv["port"], "server.port", 1)
    if srv["port"] > 65535:
        raise ConfigError("server.port must be <= 65535")
    srv["read_timeout_s"] = _as_int(srv.get("read_timeout_s", 30),
                                    "server.read_timeout_s", 1)

    # every relative path in the config resolves against the config file's
    # directory (or the CWD when running on pure defaults with no config file)
    if os.path.isfile(src):
        base_dir = os.path.dirname(os.path.abspath(src))
    else:
        base_dir = os.getcwd()

    idx = cfg["index"]
    idx["max_file_mb"] = _as_int(idx["max_file_mb"], "index.max_file_mb", 1)
    idx["chunk_size"] = _as_int(idx["chunk_size"], "index.chunk_size", 50)
    idx["chunk_overlap"] = _as_int(idx["chunk_overlap"], "index.chunk_overlap", 0)
    if idx["chunk_overlap"] >= idx["chunk_size"]:
        raise ConfigError("index.chunk_overlap must be smaller than index.chunk_size")
    idx["data_dir"] = _resolve_path(idx["data_dir"], base_dir)

    paths = cfg["paths"]
    paths["_base_dir"] = base_dir
    paths["whitelist"] = [
        _resolve_path(p, base_dir) for p in _as_str_list(paths["whitelist"], "paths.whitelist")
    ]
    paths["blacklist"] = [
        _resolve_path(p, base_dir) for p in _as_str_list(paths["blacklist"], "paths.blacklist")
    ]
    paths["exclude_names"] = _as_str_list(paths["exclude_names"], "paths.exclude_names")

    emb = cfg["embedding"]
    if emb["backend"] not in ("local", "ollama"):
        raise ConfigError(f"embedding.backend must be 'local' or 'ollama', got {emb['backend']!r}")
    emb["dim"] = _as_int(emb["dim"], "embedding.dim", 16)
    emb["timeout_s"] = _as_int(emb["timeout_s"], "embedding.timeout_s", 1)
    # privacy boundary: the Ollama endpoint must be this machine, enforced at
    # config-load time (fail closed) regardless of the selected backend
    try:
        emb["ollama_url"] = validate_loopback_http_url(
            str(emb["ollama_url"]).rstrip("/"), name="embedding.ollama_url")
    except ValueError as e:
        raise ConfigError(str(e)) from None

    ocr = cfg["ocr"]
    if ocr["mode"] not in ("auto", "off"):
        raise ConfigError(f"ocr.mode must be 'auto' or 'off', got {ocr['mode']!r}")
    ocr["timeout_s"] = _as_int(ocr["timeout_s"], "ocr.timeout_s", 5)

    sch = cfg["search"]
    sch["fulltext_weight"] = _as_float(sch["fulltext_weight"], "search.fulltext_weight")
    sch["vector_weight"] = _as_float(sch["vector_weight"], "search.vector_weight")
    sch["top_k"] = _as_int(sch["top_k"], "search.top_k", 1)

    wat = cfg["watcher"]
    wat["enabled"] = bool(wat["enabled"])
    wat["interval_s"] = _as_int(wat["interval_s"], "watcher.interval_s", 1)

    sc = cfg["selfcheck"]
    sc["enabled"] = bool(sc["enabled"])
    sc["interval_s"] = _as_int(sc["interval_s"], "selfcheck.interval_s", 1)
    sc["item_delay_ms"] = _as_int(sc["item_delay_ms"], "selfcheck.item_delay_ms", 0)
    sc["consecutive_error_threshold"] = _as_int(
        sc["consecutive_error_threshold"], "selfcheck.consecutive_error_threshold", 1)
    sc["backoff_max_s"] = _as_int(sc["backoff_max_s"], "selfcheck.backoff_max_s", 1)
    sc["index_size_warn_mb"] = _as_int(sc["index_size_warn_mb"], "selfcheck.index_size_warn_mb", 1)

    lg = cfg["logs"]
    lg["dir"] = _resolve_path(lg["dir"], base_dir)
    lg["max_mb"] = _as_int(lg["max_mb"], "logs.max_mb", 1)
    lg["backups"] = _as_int(lg["backups"], "logs.backups", 1)

    return cfg


def effective_public_config(cfg: dict) -> dict:
    """Redacted view for the status API (no secrets exist, but strip internals)."""
    pub = copy.deepcopy(cfg)
    pub["paths"]["whitelist"] = list(cfg["paths"]["whitelist"])
    pub.pop("_config_source", None)
    pub["paths"].pop("_base_dir", None)
    return pub
