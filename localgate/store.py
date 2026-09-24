"""Local index storage: document metadata, chunk metadata, BM25 full-text index
and a segmented vector store. All data lives under the configured data dir.

Concurrency: one RLock guards all mutations and reads (search vs ingest vs
self-check). Persistence uses atomic tmp+rename writes; corrupt lines are
tolerated on load and reported for the self-check loop to repair.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

BM25_K1 = 1.4
BM25_B = 0.75
SEGMENT_MAX_ENTRIES = 512
SEGMENT_MERGE_THRESHOLD = 64  # segments smaller than this are merge candidates


def tokenize(text: str) -> list[str]:
    """ASCII word tokens + CJK char bigrams (single char kept if alone)."""
    text = (text or "").lower()
    tokens: list[str] = []
    for m in _WORD_RE.finditer(text):
        tokens.append(m.group(0))
    i, n = 0, len(text)
    while i < n:
        if ord(text[i]) > 127:
            j = i
            while j < n and ord(text[j]) > 127:
                j += 1
            run = text[i:j]
            if len(run) == 1:
                tokens.append(run)
            else:
                for k in range(len(run) - 1):
                    tokens.append(run[k:k + 2])
            i = j
        else:
            i += 1
    return tokens


class IndexStore:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.docs_dir = os.path.join(self.data_dir, "index")
        self.vec_dir = os.path.join(self.docs_dir, "vectors")
        self.docs_path = os.path.join(self.docs_dir, "docs.jsonl")
        self.chunks_path = os.path.join(self.docs_dir, "chunks.jsonl")
        self.bm25_path = os.path.join(self.docs_dir, "bm25.json")
        os.makedirs(self.vec_dir, exist_ok=True)

        self.lock = threading.RLock()
        self.docs: dict[str, dict] = {}        # doc_id -> metadata (+path, status)
        self.chunks: dict[str, dict] = {}      # chunk_id -> {doc_id, ordinal, text}
        self.postings: dict[str, dict[str, int]] = {}  # term -> {chunk_id: tf}
        self.chunk_len: dict[str, int] = {}    # chunk_id -> token count
        self.total_len = 0
        self.vectors: dict[str, list[float]] = {}  # chunk_id -> vector (mirror)
        self.load_errors: list[str] = []
        self._load()

    # ------------------------------------------------------------ persistence

    def _atomic_write(self, path: str, data: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _load(self) -> None:
        self.load_errors = []

        def load_jsonl(path: str, sink: dict[str, dict], id_field: str) -> None:
            if not os.path.exists(path):
                return
            with open(path, encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        rec_id = rec.get(id_field)
                        if not isinstance(rec, dict) or not rec_id:
                            raise ValueError(f"missing {id_field}")
                        sink[rec_id] = rec
                    except ValueError as e:
                        self.load_errors.append(f"{os.path.basename(path)}:{ln}: {e}")

        load_jsonl(self.docs_path, self.docs, "doc_id")
        load_jsonl(self.chunks_path, self.chunks, "chunk_id")
        # drop chunks whose doc vanished
        self.chunks = {cid: c for cid, c in self.chunks.items() if c.get("doc_id") in self.docs}

        if os.path.exists(self.bm25_path):
            try:
                with open(self.bm25_path, encoding="utf-8") as f:
                    data = json.load(f)
                self.postings = data.get("postings", {})
                self.chunk_len = data.get("chunk_len", {})
                self.total_len = int(data.get("total_len", 0))
            except (ValueError, OSError) as e:
                self.load_errors.append(f"bm25.json: {e}")
                self.postings, self.chunk_len, self.total_len = {}, {}, 0

        for seg in self._segment_paths():
            try:
                with open(seg, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            cid, vec = rec.get("chunk_id"), rec.get("vector")
                            if cid and isinstance(vec, list):
                                self.vectors[cid] = [float(x) for x in vec]
                            else:
                                raise ValueError("bad vector record")
                        except ValueError as e:
                            self.load_errors.append(f"{os.path.basename(seg)}: {e}")
            except OSError as e:
                self.load_errors.append(f"{os.path.basename(seg)}: {e}")
        # keep only vectors belonging to live chunks
        self.vectors = {cid: v for cid, v in self.vectors.items() if cid in self.chunks}

        # cross-file consistency recovery: bm25.json is persisted separately
        # from docs/chunks, so a crash (or partial write) between saves can
        # leave postings/chunk_len referencing chunks that no longer exist.
        # Detect and prune now; record what was repaired.
        self._prune_after_load()

    def _prune_after_load(self) -> None:
        live = set(self.chunks.keys())
        stale_stats = sum(1 for cid in list(self.chunk_len) if cid not in live)
        stale_postings = 0
        for term in list(self.postings.keys()):
            posting = self.postings[term]
            dead = [cid for cid in posting if cid not in live]
            for cid in dead:
                del posting[cid]
                stale_postings += 1
            if not posting:
                del self.postings[term]
        # total_len is rebuilt from surviving chunk lengths
        self.total_len = sum(self.chunk_len[cid] for cid in self.chunk_len
                             if cid in live)
        self.chunk_len = {cid: n for cid, n in self.chunk_len.items() if cid in live}
        if stale_stats or stale_postings:
            self.load_errors.append(
                f"recovered partial write: pruned {stale_postings} stale postings, "
                f"{stale_stats} stale chunk stats")

    def _segment_paths(self) -> list[str]:
        try:
            names = sorted(n for n in os.listdir(self.vec_dir)
                           if n.startswith("seg_") and n.endswith(".jsonl"))
        except OSError:
            return []
        return [os.path.join(self.vec_dir, n) for n in names]

    def save_all(self) -> None:
        with self.lock:
            docs_data = "".join(
                json.dumps(self.docs[d], ensure_ascii=False, default=str) + "\n"
                for d in sorted(self.docs))
            self._atomic_write(self.docs_path, docs_data)
            chunks_data = "".join(
                json.dumps(self.chunks[c], ensure_ascii=False, default=str) + "\n"
                for c in sorted(self.chunks))
            self._atomic_write(self.chunks_path, chunks_data)
            bm25_data = json.dumps(
                {"postings": self.postings, "chunk_len": self.chunk_len,
                 "total_len": self.total_len},
                ensure_ascii=False, default=str)
            self._atomic_write(self.bm25_path, bm25_data)
            self._save_vectors()

    def _save_vectors(self) -> None:
        """Write the in-memory vector mirror into segment files (atomic per file)."""
        items = sorted(self.vectors.items())
        segs: list[list[tuple[str, list[float]]]] = []
        for i in range(0, len(items), SEGMENT_MAX_ENTRIES):
            segs.append(items[i:i + SEGMENT_MAX_ENTRIES])
        # reuse existing filenames first, delete extras
        existing = self._segment_paths()
        for idx, seg in enumerate(segs):
            path = existing[idx] if idx < len(existing) else \
                os.path.join(self.vec_dir, f"seg_{idx + 1:06d}.jsonl")
            data = "".join(
                json.dumps({"chunk_id": cid, "vector": vec}) + "\n" for cid, vec in seg)
            self._atomic_write(path, data)
        for path in existing[len(segs):]:
            try:
                os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------ mutations

    def _add_ft(self, chunk_id: str, text: str) -> None:
        toks = tokenize(text)
        self.chunk_len[chunk_id] = len(toks)
        self.total_len += len(toks)
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for term, count in tf.items():
            self.postings.setdefault(term, {})[chunk_id] = count

    def _remove_ft(self, chunk_id: str) -> None:
        length = self.chunk_len.pop(chunk_id, None)
        if length is not None:
            self.total_len = max(0, self.total_len - length)
        for term in list(self.postings.keys()):
            posting = self.postings[term]
            if chunk_id in posting:
                del posting[chunk_id]
                if not posting:
                    del self.postings[term]

    def upsert_doc(self, doc_id: str, meta: dict, chunk_texts: list[str],
                   vectors: list[list[float]] | None) -> None:
        """Replace the doc's chunks (and optionally vectors) atomically in memory."""
        with self.lock:
            for cid in list(self.chunks.keys()):
                if self.chunks[cid].get("doc_id") == doc_id:
                    self._remove_ft(cid)
                    del self.chunks[cid]
                    self.vectors.pop(cid, None)
            new_chunks: dict[str, dict] = {}
            for i, text in enumerate(chunk_texts):
                cid = f"{doc_id}#c{i}"
                new_chunks[cid] = {"chunk_id": cid, "doc_id": doc_id,
                                   "ordinal": i, "text": text}
                self._add_ft(cid, text)
                if vectors is not None and i < len(vectors):
                    self.vectors[cid] = vectors[i]
            self.chunks.update(new_chunks)
            record = dict(meta)
            record["doc_id"] = doc_id
            record["chunk_count"] = len(chunk_texts)
            record["vector_count"] = sum(
                1 for cid in new_chunks if cid in self.vectors)
            record["updated_at"] = record.get("updated_at") or _now_iso()
            self.docs[doc_id] = record

    def remove_doc(self, doc_id: str) -> bool:
        with self.lock:
            if doc_id not in self.docs:
                return False
            for cid in [c for c, ch in self.chunks.items() if ch.get("doc_id") == doc_id]:
                self._remove_ft(cid)
                del self.chunks[cid]
                self.vectors.pop(cid, None)
            del self.docs[doc_id]
            return True

    def doc_by_path(self, path: str) -> dict | None:
        with self.lock:
            for d in self.docs.values():
                if d.get("path") == path:
                    return d
            return None

    def all_doc_paths(self) -> list[str]:
        """Locked snapshot of indexed paths. Callers must not iterate
        self.docs directly - scans and self-check repairs run concurrently."""
        with self.lock:
            return [d["path"] for d in self.docs.values()
                    if isinstance(d.get("path"), str)]

    def materialize_results(self, chunk_ids: list[str]) -> list[tuple[dict, dict]]:
        """Locked (chunk, doc) lookup for search result building. Returns only
        entries that still exist at snapshot time."""
        with self.lock:
            out: list[tuple[dict, dict]] = []
            for cid in chunk_ids:
                chunk = self.chunks.get(cid)
                if not chunk:
                    continue
                out.append((chunk, self.docs.get(chunk.get("doc_id", ""), {})))
            return out

    def set_doc_status(self, doc_id: str, status: str, error: str | None = None) -> None:
        with self.lock:
            d = self.docs.get(doc_id)
            if not d:
                return
            d["status"] = status
            if error is None:
                d.pop("error", None)
            else:
                d["error"] = str(error)[:300]

    # ------------------------------------------------------------ search

    def _avg_len(self) -> float:
        if not self.chunk_len:
            return 0.0
        return self.total_len / len(self.chunk_len)

    def search_fulltext(self, query: str, top_n: int = 50) -> list[tuple[str, float]]:
        with self.lock:
            if not self.chunks or not self.postings:
                return []
            terms = tokenize(query)
            if not terms:
                return []
            n = len(self.chunks)
            avg = self._avg_len() or 1.0
            scores: dict[str, float] = {}
            seen: set[str] = set()
            for term in terms:
                posting = self.postings.get(term)
                if not posting:
                    continue
                seen.update(posting.keys())
                df = len(posting)
                idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
                for cid, tf in posting.items():
                    dl = self.chunk_len.get(cid, 0) or 1
                    denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg)
                    scores[cid] = scores.get(cid, 0.0) + idf * tf * (BM25_K1 + 1) / denom
            # small phrase bonus: chunks containing the full query string
            ql = " ".join(terms)
            for cid in seen:
                ch = self.chunks.get(cid)
                if ch and ql and ql in " ".join(tokenize(ch.get("text", ""))):
                    scores[cid] = scores.get(cid, 0.0) * 1.25
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
            return ranked

    def search_vector(self, qvec: list[float], top_n: int = 50) -> list[tuple[str, float]]:
        with self.lock:
            if not self.vectors:
                return []
            qnorm = math.sqrt(sum(x * x for x in qvec)) or 1.0
            scored: list[tuple[str, float]] = []
            for cid, vec in self.vectors.items():
                if len(vec) != len(qvec):
                    continue
                dot = sum(a * b for a, b in zip(qvec, vec, strict=False))
                vnorm = math.sqrt(sum(x * x for x in vec)) or 1.0
                scored.append((cid, dot / (qnorm * vnorm)))
            scored.sort(key=lambda kv: kv[1], reverse=True)
            return scored[:top_n]

    # ------------------------------------------------------------ maintenance

    def stale_docs_missing_files(self) -> tuple[list[str], list[str]]:
        """Docs whose backing file is confirmed deleted vs unreachable.

        Returns (confirmed, unreachable):
        - confirmed: parent directory still exists and lists, file is gone ->
          safe to remove from the index;
        - unreachable: parent directory missing/not listable (unmounted
          volume, permission loss) -> treated as UNKNOWN, never removed, so a
          transient failure cannot mass-clear the index."""
        with self.lock:
            confirmed: list[str] = []
            unreachable: list[str] = []
            for doc_id, d in self.docs.items():
                p = d.get("path")
                if not p:
                    confirmed.append(doc_id)
                    continue
                if os.path.exists(p):
                    continue
                parent = os.path.dirname(os.path.abspath(p))
                try:
                    parent_ok = os.path.isdir(parent)
                except OSError:
                    parent_ok = False
                if parent_ok:
                    confirmed.append(doc_id)
                else:
                    unreachable.append(doc_id)
            return confirmed, unreachable

    def broken_docs(self) -> list[str]:
        """Docs with missing chunks, chunk-count mismatch or missing vectors."""
        with self.lock:
            broken = []
            for doc_id, d in self.docs.items():
                if d.get("status") in ("parse_error", "ocr_failed"):
                    continue  # handled by single-file reparse policy separately
                expected = int(d.get("chunk_count", 0))
                own = [c for c in self.chunks.values() if c.get("doc_id") == doc_id]
                if len(own) != expected:
                    broken.append(doc_id)
                    continue
                embed_failed = d.get("embed_failed")
                if not embed_failed:
                    if any(c["chunk_id"] not in self.vectors for c in own):
                        broken.append(doc_id)
            return broken

    def orphan_cleanup(self) -> dict:
        """Safe automatic optimization: drop index entries with no backing doc."""
        with self.lock:
            live_chunk_ids = set(self.chunks.keys())
            removed = {"vectors": 0, "postings": 0, "chunk_stats": 0}
            for cid in list(self.chunk_len.keys()):
                if cid not in live_chunk_ids:
                    self.chunk_len.pop(cid, None)
                    removed["chunk_stats"] += 1
            for term in list(self.postings.keys()):
                posting = self.postings[term]
                dead = [cid for cid in posting if cid not in live_chunk_ids]
                for cid in dead:
                    del posting[cid]
                    removed["postings"] += 1
                if not posting:
                    del self.postings[term]
            for cid in list(self.vectors.keys()):
                if cid not in live_chunk_ids:
                    del self.vectors[cid]
                    removed["vectors"] += 1
            return removed

    def merge_small_segments(self, min_entries: int = SEGMENT_MERGE_THRESHOLD) -> int:
        """Merge fragmented small vector segments. Non-destructive to user data."""
        with self.lock:
            segs = self._segment_paths()
            if len(segs) <= 1:
                return 0
            sizes = []
            for p in segs:
                try:
                    with open(p, encoding="utf-8") as f:
                        sizes.append(sum(1 for ln in f if ln.strip()))
                except OSError:
                    sizes.append(0)
            small = [p for p, s in zip(segs, sizes, strict=False) if 0 < s < min_entries]
            if not small:
                return 0
            # simply rewrite all vectors through _save_vectors (canonical segments)
            before = len(segs)
            self._save_vectors()
            after = len(self._segment_paths())
            return max(0, before - after)

    def index_size_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.data_dir):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        return total

    def stats(self) -> dict:
        with self.lock:
            by_status: dict[str, int] = {}
            by_kind: dict[str, int] = {}
            for d in self.docs.values():
                by_status[d.get("status", "?")] = by_status.get(d.get("status", "?"), 0) + 1
                by_kind[d.get("kind", "?")] = by_kind.get(d.get("kind", "?"), 0) + 1
            embed_failed = sum(1 for d in self.docs.values() if d.get("embed_failed"))
            return {
                "docs": len(self.docs),
                "chunks": len(self.chunks),
                "vectors": len(self.vectors),
                "terms": len(self.postings),
                "segments": len(self._segment_paths()),
                "docs_by_status": by_status,
                "docs_by_kind": by_kind,
                "embed_failed_docs": embed_failed,
                "index_size_bytes": self.index_size_bytes(),
                "load_errors": len(self.load_errors),
            }

    def get_document(self, doc_id: str) -> dict | None:
        with self.lock:
            d = self.docs.get(doc_id)
            if not d:
                return None
            own = sorted(
                (c for c in self.chunks.values() if c.get("doc_id") == doc_id),
                key=lambda c: c.get("ordinal", 0))
            return {"doc": d, "chunks": own}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
