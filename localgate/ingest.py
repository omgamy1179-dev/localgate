"""Ingest pipeline: scan whitelisted dirs, extract, chunk, embed, store.

Every action here is READ against user files; writes go only to the index dir.
Single-file failures are isolated: one bad file never stops the scan.
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Any

from . import extract as extract_mod
from .chunker import chunk_text
from .fsutil import fingerprint, iter_files
from .ocr import OcrUnavailable

# recognized OCR text kept per image (bounds memory before chunking)
MAX_OCR_TEXT_CHARS = 256 * 1024

# wall-clock budget per file for text extraction. Size caps already bound the
# work; this deadline guarantees a pathological parse cannot wedge the scan.
# On timeout the file is recorded as a parse error and the pipeline moves on;
# the (daemon) worker thread finishes in the background, bounded by the same
# size caps, and ingests stay sequential so at most one thread lingers.
EXTRACT_DEADLINE_S = 30


def _extract_with_deadline(path: str, kind: str,
                           deadline_s: float | None = None) -> tuple[str, str, dict]:
    """Run extraction with a hard wall-clock deadline (fail closed to error)."""
    if deadline_s is None:
        deadline_s = EXTRACT_DEADLINE_S
    outcome: dict = {}

    def _target() -> None:
        try:
            outcome["value"] = extract_mod.extract(path, kind)
        except BaseException as e:  # noqa: BLE001 - re-raised in caller thread
            outcome["error"] = e

    worker = threading.Thread(target=_target, name="localgate-extract", daemon=True)
    worker.start()
    worker.join(deadline_s)
    if worker.is_alive():
        raise extract_mod.ExtractError(
            f"extraction exceeded {deadline_s}s processing deadline")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class IngestProgress:
    """Thread-safe progress snapshot for the status API."""

    def __init__(self):
        self.lock = threading.Lock()
        self.scanning = False
        self.queue = 0
        self.processed = 0
        self.errors = 0
        self.last_scan_at: str | None = None
        self.last_file: str | None = None
        self.ocr_failures = 0
        self.parse_failures = 0
        self.ingest_errors: list[str] = []

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "scanning": self.scanning,
                "queue": self.queue,
                "processed": self.processed,
                "errors": self.errors,
                "last_scan_at": self.last_scan_at,
                "last_file": self.last_file,
                "ocr_failures": self.ocr_failures,
                "parse_failures": self.parse_failures,
                "recent_errors": list(self.ingest_errors[-10:]),
            }

    def note_error(self, path: str, msg: str) -> None:
        with self.lock:
            self.errors += 1
            self.ingest_errors.append(f"{path}: {msg}"[:400])
            self.ingest_errors = self.ingest_errors[-50:]


def doc_id_for(path: str) -> str:
    return hashlib.sha1(os.path.abspath(path).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def confirmed_gone(path: str) -> bool:
    """True only when `path` is verifiably deleted: its parent directory still
    exists (and lists) while the file itself does not. A missing parent means
    a volume/mount/permission problem - treated as 'unknown', never as
    user-initiated deletion."""
    parent = os.path.dirname(os.path.abspath(path))
    try:
        return os.path.isdir(parent) and not os.path.exists(path)
    except OSError:
        return False


class Ingestor:
    def __init__(self, cfg: dict, store, embedder, ocr_engine, progress: IngestProgress,
                 protected_dirs: list[str]):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.ocr = ocr_engine
        self.progress = progress
        self.protected_dirs = protected_dirs

    # ------------------------------------------------------------------ one file

    def ingest_file(self, path: str) -> dict:
        """Index a single file. Returns the doc record. Never raises for
        per-file problems: failures become doc statuses."""
        doc_id = doc_id_for(path)
        ext = os.path.splitext(path)[1].lower()
        kind = extract_mod.classify(path)
        try:
            fp = fingerprint(path, self.cfg["index"]["max_file_mb"])
        except OSError as e:
            self.progress.note_error(path, f"fingerprint failed: {e}")
            return {"doc_id": doc_id, "path": path, "status": "error", "error": str(e)}

        meta = {
            "doc_id": doc_id, "path": os.path.abspath(path), "ext": ext, "kind": kind,
            "size": fp["size"], "mtime_ns": fp["mtime_ns"], "sha256": fp["sha256"],
            "status": "ok", "indexed_at": _now_iso(),
        }

        if kind == "image":
            try:
                text, engine = self.ocr.recognize(path)
                if len(text) > MAX_OCR_TEXT_CHARS:
                    text = text[:MAX_OCR_TEXT_CHARS]
                    meta["truncated"] = True
                meta["ocr"] = {"engine": engine, "chars": len(text)}
                if not text:
                    meta["status"] = "ocr_empty"
            except OcrUnavailable as e:
                meta["status"] = "ocr_unavailable"
                meta["error"] = str(e)
                with self.progress.lock:
                    self.progress.ocr_failures += 1
                self.store.upsert_doc(doc_id, meta, [], None)
                return meta
            except Exception as e:
                meta["status"] = "ocr_failed"
                meta["error"] = str(e)[:300]
                with self.progress.lock:
                    self.progress.ocr_failures += 1
                self.store.upsert_doc(doc_id, meta, [], None)
                return meta
        else:
            try:
                text, kind, extra = _extract_with_deadline(path, kind)
                meta["kind"] = kind
                if extra:
                    meta["extract"] = {k: v for k, v in extra.items()}
            except Exception as e:
                # any per-file parse failure (corrupt archive, bomb guard,
                # unsupported shape, unexpected parser error) stays contained
                meta["status"] = "parse_error"
                meta["error"] = f"{type(e).__name__}: {e}"[:300]
                with self.progress.lock:
                    self.progress.parse_failures += 1
                self.store.upsert_doc(doc_id, meta, [], None)
                self.progress.note_error(path, f"parse failed: {e}")
                return meta
            if not text or not text.strip():
                meta["status"] = "empty"
                self.store.upsert_doc(doc_id, meta, [], None)
                return meta

        icfg = self.cfg["index"]
        chunks = chunk_text(text, icfg["chunk_size"], icfg["chunk_overlap"])
        vectors: list[list[float]] | None = None
        try:
            vectors = self.embedder.embed(chunks)
        except Exception as e:
            # keep the doc searchable via full-text; self-check retries embedding
            meta["embed_failed"] = True
            meta["error"] = f"embedding failed: {e}"[:300]
        self.store.upsert_doc(doc_id, meta, chunks, vectors)
        with self.progress.lock:
            self.progress.processed += 1
            self.progress.last_file = path
        self.store.set_doc_status(doc_id, meta["status"],
                                  meta.get("error"))
        return meta

    def remove_path(self, path: str) -> bool:
        doc_id = doc_id_for(path)
        return self.store.remove_doc(doc_id)

    # ------------------------------------------------------------------ scan

    def scan_whitelist(self, reason: str = "manual") -> dict:
        """Full pass over the whitelist. Incremental: only new/changed files
        are re-ingested; files verifiably gone from disk are dropped from the
        index. If NO whitelist root is currently accessible (unmounted volume,
        transient failure) the scan is skipped and the index is left untouched
        - temporary unavailability must never be misread as mass deletion."""
        paths = self.cfg["paths"]
        protected = self.protected_dirs
        with self.progress.lock:
            if self.progress.scanning:
                return {"skipped": "scan already running"}
            self.progress.scanning = True
        summary: dict[str, Any] = {"reason": reason, "ingested": 0,
                                   "removed": 0, "failed": 0, "skipped": 0}
        try:
            whitelist = paths["whitelist"]
            accessible = [r for r in whitelist if os.path.isdir(r)]
            if whitelist and not accessible:
                with self.progress.lock:
                    self.progress.last_scan_at = _now_iso()
                summary["skipped"] = ("no whitelist directory accessible; "
                                      "index left unchanged")
                return summary
            current: dict[str, dict] = {}
            for f in iter_files(
                    whitelist, paths["blacklist"], paths["exclude_names"],
                    extract_mod.INDEXABLE_EXTS, self.cfg["index"]["max_file_mb"],
                    protected_dirs=protected,
                    on_error=lambda p, m: self.progress.note_error(p, m)):
                try:
                    fp = fingerprint(f, self.cfg["index"]["max_file_mb"])
                except OSError as e:
                    self.progress.note_error(f, f"fingerprint failed: {e}")
                    summary["failed"] += 1
                    continue
                current[os.path.abspath(f)] = fp
                existing = self.store.doc_by_path(os.path.abspath(f))
                if existing and existing.get("sha256") == fp["sha256"] and \
                        existing.get("status") == "ok" and \
                        not existing.get("embed_failed"):
                    summary["skipped"] += 1
                    continue
                with self.progress.lock:
                    self.progress.queue += 1
                try:
                    meta = self.ingest_file(f)
                    if meta.get("status") in ("error", "parse_error", "read_error"):
                        summary["failed"] += 1
                    else:
                        summary["ingested"] += 1
                finally:
                    with self.progress.lock:
                        self.progress.queue = max(0, self.progress.queue - 1)
            # remove docs whose files are no longer whitelist-visible - but
            # only when the removal is confirmable: the file's parent
            # directory must still exist and list. A vanished parent (mount
            # point gone, permission lost) is kept instead of purged.
            indexed_paths = set(self.store.all_doc_paths())
            for p in sorted(indexed_paths - set(current.keys())):
                if p and confirmed_gone(p) and self.remove_path(p):
                    summary["removed"] += 1
            self.store.save_all()
            with self.progress.lock:
                self.progress.last_scan_at = _now_iso()
            return summary
        finally:
            with self.progress.lock:
                self.progress.scanning = False

    def retry_failed_docs(self, only_doc_ids: list[str] | None = None) -> dict:
        """Re-ingest specific docs (single-file repair policy)."""
        with self.store.lock:
            if only_doc_ids is None:
                targets = [d for d in self.store.docs.values()
                           if d.get("status") in ("parse_error", "ocr_failed", "embed_failed")
                           or d.get("embed_failed")]
            else:
                targets = [self.store.docs[d] for d in only_doc_ids if d in self.store.docs]
        retried, fixed, still_broken, dropped = 0, 0, 0, 0
        for d in targets:
            p = d.get("path")
            if not p:
                self.store.remove_doc(d["doc_id"])
                dropped += 1
                continue
            if not os.path.exists(p):
                if confirmed_gone(p):
                    # verifiably deleted by the user: drop the index entry
                    self.store.remove_doc(d["doc_id"])
                    dropped += 1
                # else: unreachable (unmounted volume etc.) - keep entry, retry later
                continue
            retried += 1
            meta = self.ingest_file(p)
            if meta.get("status") == "ok" and not meta.get("embed_failed"):
                fixed += 1
            else:
                still_broken += 1
        if retried or dropped:
            self.store.save_all()
        return {"retried": retried, "fixed": fixed, "still_broken": still_broken,
                "dropped": dropped}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
