"""Ingest pipeline: scan whitelisted dirs, extract, chunk, embed, store.

Every action here is READ against user files; writes go only to the index dir.
Single-file failures are isolated: one bad file never stops the scan.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import threading
from typing import Any

from . import extract as extract_mod
from .chunker import chunk_text
from .fsutil import (
    FileTooLarge,
    confirmed_gone,
    fingerprint,
    is_under,
    iter_files,
)
from .ocr import OcrUnavailable

# recognized OCR text kept per image (bounds memory before chunking)
MAX_OCR_TEXT_CHARS = 256 * 1024

# wall-clock budget per file for text extraction. Size caps already bound the
# work; this deadline guarantees a pathological parse cannot wedge the scan.
# Extraction runs in a dedicated child process (see _ExtractWorkerPool): on
# timeout the child is terminated and reaped, so a wedged parser cannot keep
# burning CPU after its file was recorded as a parse error, and consecutive
# timeouts cannot accumulate workers.
EXTRACT_DEADLINE_S = 30

# terminate() -> wait -> kill() escalation window for a timed-out worker
_EXTRACT_KILL_GRACE_S = 5.0
# how long _start_worker waits for the child's ready handshake
_EXTRACT_BOOT_GRACE_S = 30.0
_READY = "__ready__"


def _worker_main(conn) -> None:
    """Extraction worker entry point. Module-level so Windows spawn can pickle
    it by qualified name (closures cannot cross a spawn boundary).

    Serves one extraction request at a time until the pipe closes. The parent
    owns the process lifetime and kills it when a request overruns its
    deadline; nothing here holds state worth surviving."""
    try:
        conn.send((_READY,))  # bootstrap handshake: only after this may the
    except OSError:           # parent close its copy of this pipe end
        return
    while True:
        try:
            req = conn.recv()
        except (EOFError, OSError):
            return
        if req is None:
            return
        path, kind, max_bytes = req
        try:
            # final-entry size gate in the process that actually reads the
            # file (the orchestrator checks independently before dispatch)
            size = os.path.getsize(path)
            if size > max_bytes:
                raise extract_mod.ExtractError(
                    f"file size {size} exceeds max_file_mb cap of {max_bytes} bytes")
            text, kind2, extra = extract_mod.extract(path, kind)
            conn.send((True, text, kind2, extra))
        except BaseException as e:  # noqa: BLE001 - reported to the parent
            try:
                conn.send((False, str(e)))
            except Exception:
                return  # pipe is gone; the parent sees EOF


class _ExtractWorkerPool:
    """Single-worker extraction pool with a hard, enforceable deadline.

    At most ONE extraction child exists at any time. A request that overruns
    its wall-clock deadline gets its child killed (terminate, then kill) and
    reaped; the next request starts a fresh child, so consecutive pathological
    files cannot accumulate workers. The child is a daemon process:
    multiprocessing's exit handler terminates it when the service exits, so
    shutdown never hangs on a wedged parser."""

    def __init__(self, worker_target=None):
        self._target = worker_target or _worker_main
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._proc = None
        self._conn = None

    def _start_worker(self) -> bool:
        """Spawn one worker and wait for its ready handshake. The parent-side
        handle of the child pipe end is closed only AFTER the child has
        signalled readiness (the child receives that end through the spawn
        fd-passing machinery, so closing too early races the bootstrap on
        some entry points). Returns False on bootstrap failure."""
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(target=self._target, args=(child_conn,),
                                 name="localgate-extract", daemon=True)
        self._proc = proc
        self._conn = parent_conn
        try:
            proc.start()
        except OSError:
            self._discard_worker(kill=False)
            return False
        try:
            if not parent_conn.poll(_EXTRACT_BOOT_GRACE_S):
                self._discard_worker(kill=True)
                return False
            if parent_conn.recv()[:1] != (_READY,):
                self._discard_worker(kill=True)
                return False
        except (EOFError, OSError, ValueError):
            self._discard_worker(kill=True)
            return False
        finally:
            try:
                child_conn.close()  # child owns its end from here on
            except OSError:
                pass
        return True

    def _discard_worker(self, kill: bool) -> None:
        proc, conn = self._proc, self._conn
        self._proc = None
        self._conn = None
        if proc is None:
            return
        if kill and proc.is_alive():
            try:
                proc.terminate()
                proc.join(_EXTRACT_KILL_GRACE_S)
                if proc.is_alive():
                    proc.kill()
            except (OSError, ValueError):
                pass
        try:
            proc.join(_EXTRACT_KILL_GRACE_S)  # reap: no zombie is left behind
        except (OSError, ValueError):
            pass
        try:
            if conn is not None:
                conn.close()
        except OSError:
            pass

    def extract(self, path: str, kind: str, deadline_s: float,
                max_bytes: int | None = None) -> tuple[str, str, dict]:
        """One extraction with a hard deadline. Raises ExtractError on parse
        failure, worker death or timeout; the caller keeps its sequential,
        fail-closed semantics."""
        if max_bytes is None:
            max_bytes = extract_mod.MAX_INPUT_BYTES
        with self._lock:
            if self._proc is None or not self._proc.is_alive():
                self._discard_worker(kill=False)  # reap a dead worker first
                if not self._start_worker():
                    raise extract_mod.ExtractError(
                        "extraction worker failed to start")
            conn = self._conn
            if conn is None:  # pragma: no cover - _start_worker sets it
                raise extract_mod.ExtractError("extraction worker unavailable")
            try:
                conn.send((path, kind, max_bytes))
            except (OSError, ValueError) as e:
                # a crashed child can leave the pipe broken: retry once on a
                # fresh worker before reporting failure
                self._discard_worker(kill=True)
                if not self._start_worker():
                    raise extract_mod.ExtractError(
                        f"extraction worker unavailable: {e}") from e
                conn = self._conn
                try:
                    if conn is None:
                        raise extract_mod.ExtractError(
                            "extraction worker unavailable")
                    conn.send((path, kind, max_bytes))
                except (OSError, ValueError) as e2:
                    self._discard_worker(kill=True)
                    raise extract_mod.ExtractError(
                        f"extraction worker unavailable: {e2}") from e2
            if not conn.poll(max(0.0, float(deadline_s))):
                self._discard_worker(kill=True)
                raise extract_mod.ExtractError(
                    f"extraction exceeded {deadline_s}s processing deadline")
            try:
                msg = conn.recv()
                ok, rest = msg[0], msg[1:]
            except (EOFError, OSError):
                self._discard_worker(kill=True)
                raise extract_mod.ExtractError(
                    "extraction worker terminated unexpectedly") from None
            except (ValueError, TypeError, IndexError) as e:
                self._discard_worker(kill=True)
                raise extract_mod.ExtractError(
                    f"extraction worker protocol error: {e}") from e
            if not ok:
                self._discard_worker(kill=False)  # healthy worker: keep warm
                raise extract_mod.ExtractError(str(rest[0]) if rest
                                               else "extraction failed")
            text, kind2, extra = rest
            return text, kind2, extra

    def close(self) -> None:
        """Kill and reap the worker (idempotent; restarted on demand)."""
        with self._lock:
            self._discard_worker(kill=True)


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


class Ingestor:
    def __init__(self, cfg: dict, store, embedder, ocr_engine, progress: IngestProgress,
                 protected_dirs: list[str],
                 extract_pool: _ExtractWorkerPool | None = None):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.ocr = ocr_engine
        self.progress = progress
        self.protected_dirs = protected_dirs
        self._pool = extract_pool
        self.extract_deadline_s = EXTRACT_DEADLINE_S

    def _extract_pool(self) -> _ExtractWorkerPool:
        if self._pool is None:
            self._pool = _ExtractWorkerPool()
        return self._pool

    def close(self) -> None:
        """Release the extraction worker (idempotent; restarted on demand)."""
        if self._pool is not None:
            self._pool.close()

    # ------------------------------------------------------------------ one file

    def _record_preflight_failure(self, path: str, meta: dict, status: str,
                                  error: str) -> dict:
        """Persist a per-file failure that happened before any content was
        read (oversize or stat problems): diagnosable, retryable, contained."""
        meta["status"] = status
        meta["error"] = error[:300]
        self.store.upsert_doc(meta["doc_id"], meta, [], None)
        self.progress.note_error(path, f"{status}: {error}")
        return meta

    def ingest_file(self, path: str) -> dict:
        """Index a single file. Returns the doc record. Never raises for
        per-file problems: failures become doc statuses."""
        doc_id = doc_id_for(path)
        ext = os.path.splitext(path)[1].lower()
        kind = extract_mod.classify(path)
        meta: dict[str, Any] = {
            "doc_id": doc_id, "path": os.path.abspath(path), "ext": ext, "kind": kind,
            "size": None, "mtime_ns": None, "sha256": None,
            "status": "ok", "indexed_at": _now_iso(),
        }

        try:
            fp = fingerprint(path, self.cfg["index"]["max_file_mb"])
        except FileTooLarge as e:
            return self._record_preflight_failure(path, meta, "too_large", str(e))
        except OSError as e:
            return self._record_preflight_failure(path, meta, "error",
                                                  f"fingerprint failed: {e}")
        meta.update({"size": fp["size"], "mtime_ns": fp["mtime_ns"],
                     "sha256": fp["sha256"]})

        # independent final-entry gate before anything else may read the file
        # (the hash loop and the extraction worker each re-check on their own)
        cap_mb = int(self.cfg["index"]["max_file_mb"])
        cap_bytes = cap_mb * 1024 * 1024
        try:
            over = os.path.getsize(path) > cap_bytes
        except OSError as e:
            return self._record_preflight_failure(path, meta, "error",
                                                  f"size check failed: {e}")
        if over:
            return self._record_preflight_failure(
                path, meta, "too_large", f"file exceeds max_file_mb={cap_mb}")

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
                text, kind, extra = self._extract_pool().extract(
                    path, kind, self.extract_deadline_s, cap_bytes)
                meta["kind"] = kind
                if extra:
                    meta["extract"] = {k: v for k, v in extra.items()}
            except Exception as e:
                # any per-file parse failure (corrupt archive, bomb guard,
                # deadline, unsupported shape, unexpected parser error,
                # extraction worker death) stays contained
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
            # Observe each accessible root's current volume identity. Cleanup
            # below deliberately compares against the identities recorded at
            # INDEX time (store.root_identities): a root that was swapped out
            # between scans (unmounted volume leaving a hollow mount point)
            # must not have its fresh identity adopted before deletion
            # confirmation ran - otherwise the hollow root would purge the
            # entries it can no longer truly account for.
            identities_now: dict[str, tuple[int, int]] = {}
            for r in accessible:
                try:
                    st = os.stat(r)
                    identities_now[os.path.abspath(r)] = (st.st_dev, st.st_ino)
                except OSError:
                    continue
            current: dict[str, dict] = {}
            roots_with_files: set[str] = set()
            for f in iter_files(
                    whitelist, paths["blacklist"], paths["exclude_names"],
                    extract_mod.INDEXABLE_EXTS, self.cfg["index"]["max_file_mb"],
                    protected_dirs=protected,
                    on_error=lambda p, m: self.progress.note_error(p, m)):
                try:
                    fp = fingerprint(f, self.cfg["index"]["max_file_mb"])
                except FileTooLarge as e:
                    self.progress.note_error(f, f"file exceeds cap: {e}")
                    summary["failed"] += 1
                    continue
                except OSError as e:
                    self.progress.note_error(f, f"fingerprint failed: {e}")
                    summary["failed"] += 1
                    continue
                current[os.path.abspath(f)] = fp
                for r in whitelist:
                    if is_under(f, r):
                        roots_with_files.add(os.path.abspath(r))
                        break
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
                    if meta.get("status") in ("error", "parse_error", "read_error",
                                              "too_large"):
                        summary["failed"] += 1
                    else:
                        summary["ingested"] += 1
                finally:
                    with self.progress.lock:
                        self.progress.queue = max(0, self.progress.queue - 1)
            # remove docs whose files are no longer whitelist-visible - but
            # only when the removal is confirmable: the file's parent
            # directory must still exist AND list, and a whitelist root must
            # still have the identity it had at index time. A vanished or
            # swapped parent (mount point gone/hollow, permission lost) is
            # kept instead of purged.
            indexed_paths = set(self.store.all_doc_paths())
            for p in sorted(indexed_paths - set(current.keys())):
                if p and confirmed_gone(p, self.store.root_identities) \
                        and self.remove_path(p):
                    summary["removed"] += 1
            # adopt the observed identity only for roots that actually served
            # files this pass (they proved they are the real volume again);
            # hollow roots keep their index-time identity
            refresh = {r: ident for r, ident in identities_now.items()
                       if r in roots_with_files}
            if refresh:
                self.store.note_root_identities(refresh)
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
                           if d.get("status") in ("parse_error", "ocr_failed",
                                                  "embed_failed", "too_large")
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
                if confirmed_gone(p, self.store.root_identities):
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
