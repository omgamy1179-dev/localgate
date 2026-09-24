"""Embedding backends. 100% local by default.

- LocalHashEmbedder: deterministic hashed bag-of-features (ASCII word tokens +
  CJK char n-grams), TF weighting, L2-normalized. No downloads, no network.
- OllamaEmbedder: calls a user-configured Ollama endpoint (default loopback) for
  real embedding models. Failures surface as EmbedError; callers degrade.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request

from .httpclient import read_upstream_json, urlopen_noproxy

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

# embedding payloads are small; a larger "response" means something is wrong
_EMBED_MAX_BYTES = 64 * 1024 * 1024


def _features(text: str):
    text = (text or "").lower()
    for m in _WORD_RE.finditer(text):
        w = m.group(0)
        yield "w:" + w
        if len(w) > 4:
            for i in range(len(w) - 2):
                yield "s:" + w[i:i + 3]
    # CJK char bigrams/trigrams over non-ascii runs
    i = 0
    n = len(text)
    while i < n:
        if ord(text[i]) > 127:
            j = i
            while j < n and ord(text[j]) > 127:
                j += 1
            run = text[i:j]
            for k in range(len(run) - 1):
                yield "c:" + run[k:k + 2]
            for k in range(len(run) - 2):
                yield "t:" + run[k:k + 3]
            i = j
        else:
            i += 1


class EmbedError(Exception):
    pass


class LocalHashEmbedder:
    name = "local-hash"

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _bucket(self, feat: str) -> int:
        h = hashlib.sha1(feat.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "big") % self.dim

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        counts: dict[str, int] = {}
        for feat in _features(text):
            counts[feat] = counts.get(feat, 0) + 1
        for feat, tf in counts.items():
            vec[self._bucket(feat)] += 1.0 + math.log(tf)
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]

    def health(self) -> tuple[bool, int, str]:
        t0 = time.monotonic()
        try:
            v = self.embed_one("localgate embedding healthcheck")
            ok = len(v) == self.dim and any(x != 0.0 for x in v)
            return ok, int((time.monotonic() - t0) * 1000), "ok" if ok else "degenerate vector"
        except Exception as e:
            return False, int((time.monotonic() - t0) * 1000), f"error: {e}"


class OllamaEmbedder:
    name = "ollama"

    def __init__(self, url: str, model: str, timeout_s: int = 20):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = max(1, int(timeout_s))
        self._dim_cache: int | None = None

    @property
    def dim(self) -> int:
        return self._dim_cache or 0

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url + "/api/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen_noproxy(req, timeout=self.timeout_s) as resp:
                if resp.status != 200:
                    raise EmbedError(f"ollama http {resp.status}")
                return read_upstream_json(resp, timeout_s=self.timeout_s,
                                          max_bytes=_EMBED_MAX_BYTES)
        except EmbedError:
            raise
        except urllib.error.HTTPError as e:
            raise EmbedError(f"ollama http {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise EmbedError(f"ollama unreachable: {e}") from e

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            data = self._post({"model": self.model, "prompt": t})
            vec = data.get("embedding")
            if not vec or not isinstance(vec, list):
                raise EmbedError("ollama returned no embedding")
            if self._dim_cache is None:
                self._dim_cache = len(vec)
            out.append([float(x) for x in vec])
        return out

    def health(self) -> tuple[bool, int, str]:
        t0 = time.monotonic()
        try:
            vec = self.embed(["healthcheck"])[0]
            ok = len(vec) > 0 and any(x != 0.0 for x in vec)
            return ok, int((time.monotonic() - t0) * 1000), \
                f"ok dim={len(vec)}" if ok else "degenerate vector"
        except (urllib.error.URLError, EmbedError, TimeoutError, OSError) as e:
            return False, int((time.monotonic() - t0) * 1000), f"unreachable: {e}"


def make_embedder(cfg: dict):
    emb = cfg["embedding"]
    if emb["backend"] == "ollama":
        return OllamaEmbedder(emb["ollama_url"], emb["model"], emb["timeout_s"])
    return LocalHashEmbedder(dim=emb["dim"])
