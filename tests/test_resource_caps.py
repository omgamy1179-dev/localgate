"""Regression tests for the audit's resource-boundary P0/P1 fixes.

- P0-1: text extraction runs in a terminable child process. A parser that
  blows past the wall-clock deadline is killed (no lingering worker, no
  accumulation across consecutive timeouts) and the next file still indexes.
- P0-2: max_file_mb is enforced at every read entry - fingerprint()'s hash
  loop (with a byte budget against post-stat growth), ingest_file()'s final
  gate before OCR/parsing, and the self-check retry path.
- P1-1: deletion confirmation actually lists the parent directory and
  verifies whitelist-root identity before an index entry may be purged.

All tests are loopback/local-filesystem only; nothing outside temp dirs is
touched. Worker-target stubs live at module level so Windows spawn can
pickle them by qualified name.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from localgate.embedding import LocalHashEmbedder
from localgate.extract import ExtractError
from localgate.fsutil import FileTooLarge, confirmed_gone, fingerprint
from localgate.ingest import (
    _READY,
    Ingestor,
    IngestProgress,
    _ExtractWorkerPool,
    _worker_main,
    doc_id_for,
)
from localgate.ocr import OcrEngine
from localgate.store import IndexStore
from tests.helpers import make_cfg
from tests.make_samples import make_docx, make_pdf, write_text

_TIMEOUT_S = 60.0  # never let a wedged child outlive the test suite


def _handshake(conn) -> bool:
    """Mirror the production bootstrap: announce readiness so the parent can
    safely close its copy of this pipe end."""
    try:
        conn.send((_READY,))
        return True
    except OSError:
        return False


# ------------------------------------------------------------ worker stubs
# Spawn requires module-level targets; these simulate parser behaviour that
# the real parsers cannot produce deterministically (an infinite parse).

def _wedge_worker(conn) -> None:
    """Wedge forever on paths containing 'wedged', else run the real parser."""
    from localgate import extract as extract_mod
    if not _handshake(conn):
        return
    while True:
        try:
            req = conn.recv()
        except (EOFError, OSError):
            return
        if req is None:
            return
        path, _kind, _max_bytes = req
        if "wedged" in os.path.basename(str(path)):
            time.sleep(_TIMEOUT_S)
            continue
        _run_real_extract(conn, path, _kind, extract_mod)


def _crash_worker(conn) -> None:
    """Hard-crash the child on paths containing 'crash', else real parser."""
    from localgate import extract as extract_mod
    if not _handshake(conn):
        return
    while True:
        try:
            req = conn.recv()
        except (EOFError, OSError):
            return
        if req is None:
            return
        path, _kind, _max_bytes = req
        if "crash" in os.path.basename(str(path)):
            os._exit(1)
        _run_real_extract(conn, path, _kind, extract_mod)


def _run_real_extract(conn, path: str, kind: str, extract_mod) -> None:
    try:
        text, k2, extra = extract_mod.extract(path, kind)
        conn.send((True, text, k2, extra))
    except BaseException as e:  # noqa: BLE001 - mirrored production protocol
        conn.send((False, str(e)))


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name

    def make_ingestor(self, whitelist: list[str], worker_target=None,
                      deadline_s: float = 30.0,
                      max_file_mb: int = 20,
                      embedder: LocalHashEmbedder | None = None
                      ) -> tuple[Ingestor, IndexStore, LocalHashEmbedder]:
        cfg = make_cfg(self.td, paths={"whitelist": whitelist})
        cfg["index"]["max_file_mb"] = max_file_mb
        store = IndexStore(cfg["index"]["data_dir"])
        emb = embedder or LocalHashEmbedder(64)
        ing = Ingestor(cfg, store, emb, OcrEngine(mode="off"),
                       IngestProgress(), protected_dirs=[cfg["index"]["data_dir"],
                                                         cfg["logs"]["dir"]],
                       extract_pool=(_ExtractWorkerPool(worker_target=worker_target)
                                     if worker_target else None))
        ing.extract_deadline_s = deadline_s
        self.addCleanup(ing.close)
        return ing, store, emb

    @staticmethod
    def wait_until(pred, timeout_s: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.05)
        return pred()


# ------------------------------------------------------------ P0-1: isolation

class TestExtractionIsolation(TempCase):
    def test_all_kinds_through_real_worker(self):
        """Every parser kind produces identical results through the isolated
        worker as in-process (the spawn boundary must not change output)."""
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        files = [
            (write_text(os.path.join(vault, "note.md"), "worker md text"), "text"),
            (write_text(os.path.join(vault, "code.py"), "def f():\n    return 1\n"), "code"),
            (make_pdf(os.path.join(vault, "doc.pdf"), ["worker pdf text"]), "pdf"),
            (make_docx(os.path.join(vault, "doc.docx"), ["worker docx para"]), "docx"),
        ]
        pool = _ExtractWorkerPool()
        self.addCleanup(pool.close)
        for path, kind in files:
            text, k2, _extra = pool.extract(path, kind, _TIMEOUT_S)
            self.assertEqual(k2, kind)
            self.assertTrue(text.strip(), path)
        with self.assertRaises(ExtractError):
            pool.extract(os.path.join(vault, "doc.pdf"), "unknown", _TIMEOUT_S)

    def test_extract_error_propagates_through_worker(self):
        pool = _ExtractWorkerPool()
        self.addCleanup(pool.close)
        bad = os.path.join(self.td, "bad.docx")
        write_text(bad, "definitely not a zip archive")
        with self.assertRaises(ExtractError):
            pool.extract(bad, "docx", _TIMEOUT_S)

    def test_deadline_kills_worker_and_raises(self):
        """A wedged parser is terminated by the deadline: no child process
        survives the call and the caller receives ExtractError."""
        ing, store, _emb = self.make_ingestor([self.td], worker_target=_wedge_worker,
                                              deadline_s=0.5)
        wedged = write_text(os.path.join(self.td, "wedged.txt"), "never parses")
        meta = ing.ingest_file(wedged)
        self.assertEqual(meta["status"], "parse_error")
        self.assertIn("deadline", meta.get("error", ""))
        pool = ing._extract_pool()
        self.assertIsNone(pool._proc)
        # killed AND reaped: a terminated-but-unjoined child would still list
        self.assertEqual(multiprocessing.active_children(), [])

    def test_consecutive_timeouts_do_not_accumulate_workers(self):
        ing, _store, _emb = self.make_ingestor([self.td], worker_target=_wedge_worker,
                                               deadline_s=0.5)
        pool = _PidRecordingPool()
        pool._target = _wedge_worker  # reuse the harness target
        ing.extract_pool = pool
        pids: list[int] = []
        for i in range(3):
            wedged = write_text(os.path.join(self.td, f"wedged{i}.txt"), "w")
            meta = ing.ingest_file(wedged)
            self.assertEqual(meta["status"], "parse_error", i)
            alive = multiprocessing.active_children()
            self.assertLessEqual(len(alive), 1,
                                 f"worker leaked after timeout {i}: {alive}")
            # force a fresh worker so the next pid is observable
            good = write_text(os.path.join(self.td, f"after{i}.txt"), "fine")
            self.assertEqual(ing.ingest_file(good)["status"], "ok")
            pids.extend(pool.pids)
            pool.pids.clear()
        self.assertEqual(len(set(pids)), len(pids), "worker pids must not repeat")
        for pid in pids:
            self.assertTrue(self.wait_until(lambda p=pid: not _pid_alive(p)),
                            f"worker pid {pid} must be gone")

    def test_scan_continues_after_timeout(self):
        """The audit scenario end-to-end: one wedged file hits the deadline,
        the healthy file still indexes, the scan reports both honestly."""
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "wedged.txt"), "this parse never finishes")
        healthy = write_text(os.path.join(vault, "healthy.md"),
                             "healthy after wedge text")
        ing, store, _emb = self.make_ingestor([vault], worker_target=_wedge_worker,
                                              deadline_s=0.5)
        summary = ing.scan_whitelist(reason="deadline")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["ingested"], 1)
        wedged_doc = next(d for d in store.docs.values()
                          if "wedged" in d.get("path", ""))
        self.assertEqual(wedged_doc["status"], "parse_error")
        self.assertIn("deadline", wedged_doc.get("error", ""))
        self.assertTrue(any("healthy.md" in d.get("path", "")
                            for d in store.docs.values()))
        self.assertTrue(os.path.exists(healthy))  # read-only boundary intact

    def test_worker_crash_isolated_and_recovered(self):
        ing, store, _emb = self.make_ingestor([self.td], worker_target=_crash_worker,
                                              deadline_s=_TIMEOUT_S)
        crasher = write_text(os.path.join(self.td, "crash.txt"), "child dies")
        meta = ing.ingest_file(crasher)
        self.assertEqual(meta["status"], "parse_error")
        self.assertIn("worker", meta.get("error", ""))
        good = write_text(os.path.join(self.td, "fine.md"), "recovers fine text")
        meta2 = ing.ingest_file(good)
        self.assertEqual(meta2["status"], "ok")

    def test_pool_close_terminates_child(self):
        pool = _ExtractWorkerPool(worker_target=_wedge_worker)
        path = write_text(os.path.join(self.td, "wedged-close.txt"), "x")
        with self.assertRaises(ExtractError):
            pool.extract(path, "text", 0.3)  # wedge -> timeout -> kill
        pool.close()
        self.assertEqual(multiprocessing.active_children(), [])
        # a closed pool restarts cleanly on a non-wedged file
        other = write_text(os.path.join(self.td, "after-close.txt"),
                           "real text after close")
        text, _k, _e = pool.extract(other, "text", _TIMEOUT_S)
        self.assertIn("real text", text)

    def test_service_exit_does_not_hang_with_warm_worker(self):
        """Service stop (and bare interpreter exit) with a live extraction
        child must terminate promptly on every platform."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        snippet = (
            "import sys, os\n"
            f"sys.path.insert(0, {repo!r})\n"
            "import tempfile\n"
            "from tests.helpers import make_cfg\n"
            "from localgate.embedding import LocalHashEmbedder\n"
            "from localgate.ingest import Ingestor, IngestProgress\n"
            "from localgate.ocr import OcrEngine\n"
            "from localgate.store import IndexStore\n"
            "td = tempfile.mkdtemp()\n"
            "cfg = make_cfg(td, paths={'whitelist': [td]})\n"
            "store = IndexStore(cfg['index']['data_dir'])\n"
            "ing = Ingestor(cfg, store, LocalHashEmbedder(64), OcrEngine(mode='off'),\n"
            "               IngestProgress(), protected_dirs=[cfg['index']['data_dir']])\n"
            "p = os.path.join(td, 'warm.md')\n"
            "open(p, 'w').write('exit must not hang text')\n"
            "ing.ingest_file(p)\n"
            "ing.close()\n"
            "print('stopped-cleanly')\n")
        proc = subprocess.run([sys.executable, "-c", snippet], timeout=90,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stopped-cleanly", proc.stdout)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _PidRecordingPool(_ExtractWorkerPool):
    """Test double that records every worker pid as it is spawned."""

    def __init__(self) -> None:
        super().__init__()
        self.pids: list[int] = []

    def _start_worker(self) -> None:
        super()._start_worker()
        self.pids.append(self._proc.pid)


# ------------------------------------------------------------ P0-2: size caps

class _CountingEmbedder(LocalHashEmbedder):
    def __init__(self, dim: int = 64):
        super().__init__(dim)
        self.calls = 0

    def embed(self, chunks):
        self.calls += 1
        return super().embed(chunks)


class TestFileSizeCaps(TempCase):
    MB = 1024 * 1024

    def test_fingerprint_enforces_cap_boundary(self):
        p = os.path.join(self.td, "exact.bin")
        with open(p, "wb") as f:
            f.write(b"a" * self.MB)  # exactly max_mb=1
        fp = fingerprint(p, 1)
        self.assertEqual(fp["size"], self.MB)
        with open(p, "ab") as f:
            f.write(b"b")  # one byte over
        with self.assertRaises(FileTooLarge):
            fingerprint(p, 1)

    def test_fingerprint_read_budget_against_post_stat_growth(self):
        """If a file grows past the cap after the size check, the hash loop
        must abort at the byte budget instead of reading without bound."""
        p = os.path.join(self.td, "grew.bin")
        with open(p, "wb") as f:
            f.write(b"x" * (2 * self.MB))  # really 2 MB
        real_fstat = os.fstat

        def lying_fstat(fd):
            st = real_fstat(fd)
            return types.SimpleNamespace(st_size=10, st_mtime_ns=st.st_mtime_ns)

        with mock.patch.object(os, "fstat", side_effect=lying_fstat):
            with self.assertRaises(FileTooLarge) as ctx:
                fingerprint(p, 1)
        self.assertIn("grew", str(ctx.exception))

    def test_ingest_file_too_large_never_reaches_extract_or_embed(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        big = os.path.join(vault, "big.md")
        with open(big, "w") as f:
            f.write("z" * (2 * self.MB))
        emb = _CountingEmbedder()
        ing, store, _emb = self.make_ingestor([vault], max_file_mb=1,
                                              embedder=emb)
        meta = ing.ingest_file(big)
        self.assertEqual(meta["status"], "too_large")
        self.assertIn("max_file_mb", meta.get("error", ""))
        self.assertEqual(emb.calls, 0)  # embedder never saw the file
        doc = store.get_document(meta["doc_id"])
        self.assertEqual(doc["doc"]["chunk_count"], 0)
        # diagnosable and retryable, but cheap: the retry does not re-read
        store.set_doc_status(meta["doc_id"], "too_large")
        res = ing.retry_failed_docs()
        self.assertEqual(res["retried"], 1)
        self.assertEqual(res["still_broken"], 1)
        self.assertEqual(store.docs[meta["doc_id"]]["status"], "too_large")
        self.assertEqual(emb.calls, 0)

    def test_retry_oversized_grown_file_then_fixed_after_shrink(self):
        """The audit bypass: a failed doc that GROWS past the cap must not be
        fully re-read on retry; once it shrinks back the retry repairs it."""
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        p = write_text(os.path.join(vault, "victim.md"), "grow me later text")
        emb = _CountingEmbedder()
        ing, store, _emb = self.make_ingestor([vault], max_file_mb=1,
                                              embedder=emb)
        ing.scan_whitelist(reason="init")
        calls_after_init = emb.calls
        doc_id = next(iter(store.docs))
        store.set_doc_status(doc_id, "parse_error", "forced")
        with open(p, "w") as f:
            f.write("w" * (2 * self.MB))  # the failed doc grew past the cap
        res = ing.retry_failed_docs()
        self.assertEqual(res["retried"], 1)
        self.assertEqual(res["still_broken"], 1)
        self.assertEqual(store.docs[doc_id]["status"], "too_large")
        self.assertEqual(emb.calls, calls_after_init)  # never re-read/re-embedded
        with open(p, "w") as f:
            f.write("shrunk back to parseable text")
        res = ing.retry_failed_docs()
        self.assertEqual(res["fixed"], 1)
        self.assertEqual(store.docs[doc_id]["status"], "ok")

    def test_scan_reports_too_large_when_file_grows_mid_scan(self):
        """iter_files filters by size, but a file can grow before fingerprint
        re-reads it: the scan records the failure and keeps going."""
        from localgate import ingest as ingest_mod

        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        a = write_text(os.path.join(vault, "a.md"), "first file text")
        b = write_text(os.path.join(vault, "b.md"), "second file text")
        ing, store, _emb = self.make_ingestor([vault], max_file_mb=1)
        real_fp = ingest_mod.fingerprint

        def growing_fp(path, max_mb):
            if path == b:
                raise FileTooLarge(f"file grew past max_file_mb={max_mb} while reading")
            return real_fp(path, max_mb)

        with mock.patch.object(ingest_mod, "fingerprint", side_effect=growing_fp):
            summary = ing.scan_whitelist(reason="growth")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["ingested"], 1)
        self.assertEqual(store.stats()["docs"], 1)
        self.assertTrue(any("a.md" in d.get("path", "") for d in store.docs.values()))
        # no user data was ever touched: both files still exist on disk
        self.assertTrue(os.path.exists(a) and os.path.exists(b))

    def test_default_scan_path_respects_cap_end_to_end(self):
        """Over-cap files never enter the pipeline through the normal scan."""
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "small.md"), "small file text")
        with open(os.path.join(vault, "huge.md"), "w") as f:
            f.write("h" * (2 * self.MB))
        emb = _CountingEmbedder()
        ing, store, _emb = self.make_ingestor([vault], max_file_mb=1,
                                              embedder=emb)
        summary = ing.scan_whitelist(reason="cap")
        self.assertEqual(summary["ingested"], 1)
        self.assertEqual(emb.calls, 1)
        self.assertEqual(store.stats()["docs"], 1)


# ------------------------------------------------------------ P1-1: deletion

class TestConfirmedGone(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.vault = os.path.join(self.td, "vault")
        os.makedirs(self.vault)
        self.file = write_text(os.path.join(self.vault, "x.md"), "t")

    def _root_identity(self, root: str) -> tuple[int, int]:
        st = os.stat(root)
        return (st.st_dev, st.st_ino)

    def test_listed_parent_confirms_real_deletion(self):
        self.assertFalse(confirmed_gone(self.file))
        os.remove(self.file)
        self.assertTrue(confirmed_gone(self.file))

    def test_unlistable_parent_is_unknown(self):
        os.remove(self.file)
        with mock.patch("os.listdir", side_effect=PermissionError("denied")):
            self.assertFalse(confirmed_gone(self.file))
        with mock.patch("os.listdir", side_effect=OSError("i/o error")):
            self.assertFalse(confirmed_gone(self.file))

    def test_recreated_name_or_symlink_is_not_gone(self):
        os.remove(self.file)
        write_text(self.file, "a different file now")
        self.assertFalse(confirmed_gone(self.file))
        os.remove(self.file)
        os.symlink(os.path.join(self.td, "nonexistent-target"), self.file)
        self.assertFalse(confirmed_gone(self.file))  # broken symlink: not gone

    def test_missing_root_is_unknown(self):
        gone_root = os.path.join(self.td, "no-such-mount", "f.txt")
        self.assertFalse(confirmed_gone(gone_root))

    def test_swapped_root_blocks_root_direct_purge(self):
        """Unmount/remount simulation: the directory now living at the
        whitelist-root path has a DIFFERENT identity than the one recorded at
        index time (hollow mount point, deleted-and-recreated root). A
        root-direct file must not be purged based on that hollow directory."""
        ident = self._root_identity(self.vault)
        stale = {os.path.abspath(self.vault): (ident[0], ident[1] + 1)}
        os.remove(self.file)
        # listing alone confirms a real deletion...
        self.assertTrue(confirmed_gone(self.file))
        # ...but a stale root identity (the root was swapped underneath the
        # index) blocks the purge of root-direct entries
        self.assertFalse(confirmed_gone(self.file, stale),
                         "swapped/empty mount root must never confirm deletion")
        # with the identity that matches the directory on disk, it confirms
        self.assertTrue(confirmed_gone(self.file,
                                       {os.path.abspath(self.vault): ident}))

    def test_swapped_root_only_guards_its_own_files(self):
        """Multi-whitelist: a lost root keeps ITS entries; an intact root's
        real deletions are still confirmed."""
        vault_b = os.path.join(self.td, "vault-b")
        os.makedirs(vault_b)
        file_b = write_text(os.path.join(vault_b, "y.md"), "b")
        ident_a = self._root_identity(self.vault)
        ident_b = self._root_identity(vault_b)
        identities = {os.path.abspath(self.vault): (ident_a[0], ident_a[1] + 999),
                      os.path.abspath(vault_b): ident_b}
        os.remove(self.file)
        os.remove(file_b)
        self.assertFalse(confirmed_gone(self.file, identities))   # root A swapped
        self.assertTrue(confirmed_gone(file_b, identities))       # root B intact

    def test_nested_file_needs_only_parent_listing(self):
        """Nested (non-root) files are guarded by their parent directory: a
        vanished parent means 'unknown', a listed parent means confirmable."""
        sub = os.path.join(self.vault, "sub")
        os.makedirs(sub)
        nested = write_text(os.path.join(sub, "n.md"), "n")
        identities = {os.path.abspath(self.vault): self._root_identity(self.vault)}
        os.remove(nested)
        self.assertTrue(confirmed_gone(nested, identities))
        import shutil
        shutil.rmtree(sub)  # whole subtree vanished (e.g. unmount)
        self.assertFalse(confirmed_gone(nested, identities))

    def test_legacy_call_without_identities_still_works(self):
        os.remove(self.file)
        self.assertTrue(confirmed_gone(self.file, None))
        self.assertTrue(confirmed_gone(self.file, {}))

    def test_stale_docs_partition_uses_full_confirmation(self):
        """store.stale_docs_missing_files must apply the same confirmed_gone
        semantics (listable parent + root identity), not a bare isdir()."""
        vault = os.path.join(self.td, "stale-vault")
        os.makedirs(vault)
        p1 = write_text(os.path.join(vault, "one.md"), "1")
        write_text(os.path.join(vault, "two.md"), "2")
        cfg = make_cfg(self.td, paths={"whitelist": [vault]})
        store = IndexStore(cfg["index"]["data_dir"])
        from localgate.ingest import doc_id_for
        store.upsert_doc(doc_id_for(p1), {"doc_id": doc_id_for(p1), "path": p1},
                         [], None)
        store.save_all()
        # reload so the persisted state (incl. roots.json) round-trips
        store2 = IndexStore(cfg["index"]["data_dir"])
        os.remove(p1)
        confirmed, unreachable = store2.stale_docs_missing_files()
        self.assertIn(doc_id_for(p1), confirmed)
        # now simulate an unmounted root: listdir denied on the parent
        with mock.patch("os.listdir", side_effect=PermissionError("denied")):
            confirmed2, unreachable2 = store2.stale_docs_missing_files()
        self.assertNotIn(doc_id_for(p1), confirmed2)
        self.assertIn(doc_id_for(p1), unreachable2)

    def test_scan_keeps_index_when_root_identity_changes(self):
        """End-to-end: after a scan records the root identity, a file that
        disappears while the root path resolves to a different directory
        (hollow mount point) stays indexed; a genuine deletion is removed."""
        vault = os.path.join(self.td, "identity-vault")
        os.makedirs(vault)
        victim = write_text(os.path.join(vault, "victim.md"), "victim text")
        keeper = write_text(os.path.join(vault, "keeper.md"), "keeper text")
        ing, store, _emb = self.make_ingestor([vault])
        ing.scan_whitelist(reason="init")
        self.assertEqual(store.stats()["docs"], 2)

        os.remove(victim)
        os.remove(keeper)
        root_abs = os.path.abspath(vault)
        real_stat = os.stat

        def hollow_root_stat(p, *a, **kw):
            st = real_stat(p, *a, **kw)
            if os.path.abspath(p) == root_abs:  # identity changed underneath
                return os.stat_result((st.st_mode, st.st_ino + 5, st.st_dev)
                                      + st[3:])
            return st

        # hollow mount point (identity changed, no files served): nothing is
        # confirmable, nothing is purged, and the fresh identity is NOT
        # adopted (the root served no files to prove it is the real volume)
        with mock.patch.object(os, "stat", side_effect=hollow_root_stat):
            summary = ing.scan_whitelist(reason="hollow-root")
        self.assertEqual(summary["removed"], 0)
        self.assertIn(doc_id_for(victim), store.docs)   # kept: not confirmable
        self.assertIn(doc_id_for(keeper), store.docs)

        # volume restored: the stored identity matches again and the genuine
        # deletions are confirmed as before
        summary = ing.scan_whitelist(reason="restored")
        self.assertGreaterEqual(summary["removed"], 2)
        self.assertNotIn(doc_id_for(keeper), store.docs)
        self.assertNotIn(doc_id_for(victim), store.docs)
        self.assertTrue(os.path.exists(root_abs))       # read-only boundary


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------- hermetic pool failure paths

class _ScriptedConn:
    """Fake pipe connection scripted per test; records every send."""

    def __init__(self, messages=None, send_error=None, recv_error=None,
                 poll_results=None, close_error=None):
        self.sent = []
        self._messages = list(messages or [])
        self.send_error = send_error
        self.recv_error = recv_error
        self.poll_results = list(poll_results or [])
        self.close_error = close_error
        self.closed = False

    def send(self, obj):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(obj)

    def recv(self):
        if self._messages:
            return self._messages.pop(0)
        if self.recv_error is not None:
            raise self.recv_error
        raise EOFError("no more scripted messages")

    def poll(self, timeout=None):
        if self.poll_results:
            return self.poll_results.pop(0)
        return bool(self._messages)

    def close(self):
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class _ScriptedProc:
    def __init__(self, fail_start=False, alive=True):
        self.fail_start = fail_start
        self._alive = alive
        self.terminated = False
        self.killed = False
        self.joins = 0

    def start(self):
        if self.fail_start:
            raise OSError("spawn refused")

    def is_alive(self):
        return self._alive

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def join(self, timeout=None):
        self.joins += 1


class _FakeCtx:
    """Stand-in for multiprocessing.get_context("spawn"): hands out scripted
    pipes/processes so the parent-side state machine is tested in-process."""

    def __init__(self, procs=None):
        self.procs = list(procs or [])
        self.pipes: list[tuple[_ScriptedConn, _ScriptedConn]] = []

    def Pipe(self, duplex=True):
        if self.pipes:
            return self.pipes.pop(0)
        return _ScriptedConn(messages=[(_READY,)]), _ScriptedConn()

    def Process(self, target=None, args=(), name=None, daemon=None):
        if not self.procs:
            raise AssertionError("script ran out of fake processes")
        return self.procs.pop(0)


class TestPoolFailurePaths(TempCase):
    """Parent-side worker-pool state machine, exercised in-process (no spawn)
    with scripted pipes/processes - every failure branch must fail closed."""

    def _pool(self, procs) -> _ExtractWorkerPool:
        pool = _ExtractWorkerPool()
        pool._ctx = _FakeCtx(procs=procs)
        return pool

    def test_start_failure_fails_closed(self):
        pool = self._pool([_ScriptedProc(fail_start=True)])
        with self.assertRaises(ExtractError) as ctx:
            pool.extract("/x", "text", 5)
        self.assertIn("failed to start", str(ctx.exception))

    def test_bootstrap_timeout_kills_worker(self):
        proc = _ScriptedProc()
        pool = self._pool([proc])
        pool._ctx.pipes = [(_ScriptedConn(messages=[], poll_results=[False]),
                            _ScriptedConn())]
        with self.assertRaises(ExtractError) as ctx:
            pool.extract("/x", "text", 5)
        self.assertIn("failed to start", str(ctx.exception))
        self.assertTrue(proc.terminated)  # timed-out bootstrap is killed
        self.assertIsNone(pool._proc)

    def test_bootstrap_wrong_message_kills_worker(self):
        proc = _ScriptedProc()
        pool = self._pool([proc])
        pool._ctx.pipes = [(_ScriptedConn(messages=[("hello",)]),
                            _ScriptedConn())]
        with self.assertRaises(ExtractError) as ctx:
            pool.extract("/x", "text", 5)
        self.assertIn("failed to start", str(ctx.exception))
        self.assertTrue(proc.terminated)

    def test_send_failure_retries_on_fresh_worker(self):
        good_proc = _ScriptedProc()
        pool = self._pool([_ScriptedProc(), good_proc])
        pool._ctx.pipes = [
            (_ScriptedConn(messages=[(_READY,)],
                           send_error=OSError("pipe broke")),
             _ScriptedConn()),
            (_ScriptedConn(messages=[(_READY,), (True, "text ok", "text", {})]),
             _ScriptedConn()),
        ]
        text, kind, _extra = pool.extract("/f.md", "text", 30)
        self.assertEqual((text, kind), ("text ok", "text"))
        self.assertIs(pool._proc, good_proc)

    def test_send_failure_twice_raises_unavailable(self):
        pool = self._pool([_ScriptedProc(), _ScriptedProc()])
        pool._ctx.pipes = [
            (_ScriptedConn(messages=[(_READY,)],
                           send_error=OSError("pipe broke")), _ScriptedConn()),
            (_ScriptedConn(messages=[(_READY,)],
                           send_error=OSError("pipe broke again")),
             _ScriptedConn()),
        ]
        with self.assertRaises(ExtractError) as ctx:
            pool.extract("/f.md", "text", 30)
        self.assertIn("unavailable", str(ctx.exception))

    def test_recv_eof_mid_request_reports_worker_death(self):
        proc = _ScriptedProc()
        pool = self._pool([proc])
        pool._ctx.pipes = [(_ScriptedConn(
            messages=[(_READY,)], recv_error=EOFError("gone"),
            poll_results=[True, True]), _ScriptedConn())]
        with self.assertRaises(ExtractError) as ctx:
            pool.extract("/f.md", "text", 30)
        self.assertIn("terminated unexpectedly", str(ctx.exception))
        self.assertTrue(proc.terminated)

    def test_malformed_ok_payload_is_protocol_error(self):
        proc = _ScriptedProc()
        pool = self._pool([proc])
        pool._ctx.pipes = [(_ScriptedConn(
            messages=[(_READY,), (True,)],
            poll_results=[True, True]), _ScriptedConn())]
        with self.assertRaises(ExtractError) as ctx:
            pool.extract("/f.md", "text", 30)
        self.assertIn("protocol error", str(ctx.exception))
        self.assertTrue(proc.terminated)

    def test_worker_error_keeps_worker_warm(self):
        proc = _ScriptedProc()
        pool = self._pool([proc])
        pool._ctx.pipes = [(_ScriptedConn(
            messages=[(_READY,), (False, "invalid docx"),
                      (True, "second try", "text", {})],
            poll_results=[True, True, True]), _ScriptedConn())]
        with self.assertRaises(ExtractError) as ctx:
            pool.extract("/bad.docx", "docx", 30)
        self.assertIn("invalid docx", str(ctx.exception))
        # a clean error keeps the SAME warm worker alive
        self.assertIs(pool._proc, proc)
        text, _k, _e = pool.extract("/good.md", "text", 30)
        self.assertEqual(text, "second try")

    def test_discard_survives_hostile_proc_and_conn(self):
        class HostileProc(_ScriptedProc):
            def is_alive(self):
                return True

            def terminate(self):
                raise OSError("cannot terminate")

            def join(self, timeout=None):
                raise ValueError("already joined")

        pool = _ExtractWorkerPool()
        pool._proc = HostileProc()
        pool._conn = _ScriptedConn(close_error=OSError("close failed"))
        pool._discard_worker(kill=True)  # must not raise
        self.assertIsNone(pool._proc)
        self.assertIsNone(pool._conn)

    def test_close_is_idempotent(self):
        pool = self._pool([_ScriptedProc()])
        self.assertTrue(pool._start_worker())
        pool.close()
        pool.close()
        self.assertIsNone(pool._proc)


class TestWorkerMainInProcess(TempCase):
    """Drive the real worker loop with a scripted pipe - no spawn, full
    protocol coverage (ready, success, oversize, parse failure, shutdown)."""

    def _run_worker(self, script):
        conn = _ScriptedConn(messages=script)
        _worker_main(conn)
        return conn.sent

    def test_ready_then_success_then_shutdown_on_none(self):
        vault = os.path.join(self.td, "v")
        os.makedirs(vault)
        p = write_text(os.path.join(vault, "a.md"), "worker protocol text")
        sent = self._run_worker([None])
        self.assertEqual(sent, [(_READY,)])
        conn = _ScriptedConn(messages=[(p, "text", 10 * 1024 * 1024), None])
        _worker_main(conn)
        self.assertEqual(conn.sent[0], (_READY,))
        ok, text, kind, extra = conn.sent[1]
        self.assertTrue(ok and kind == "text" and "worker protocol" in text)
        self.assertEqual(extra, {})

    def test_oversize_request_reports_error(self):
        p = write_text(os.path.join(self.td, "big.md"), "x" * 1024)
        conn = _ScriptedConn(messages=[(p, "text", 10), None])
        _worker_main(conn)
        ok, msg = conn.sent[1]
        self.assertFalse(ok)
        self.assertIn("exceeds max_file_mb cap of 10 bytes", msg)

    def test_parse_failure_reports_error_string(self):
        bad = write_text(os.path.join(self.td, "bad.docx"), "not a zip")
        conn = _ScriptedConn(messages=[(bad, "docx", 10 * 1024 * 1024), None])
        _worker_main(conn)
        ok, msg = conn.sent[1]
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_recv_eof_ends_worker_silently(self):
        conn = _ScriptedConn(recv_error=EOFError("parent gone"))
        _worker_main(conn)
        self.assertEqual(conn.sent, [(_READY,)])

    def test_send_failure_after_extract_ends_worker(self):
        p = write_text(os.path.join(self.td, "b.md"), "reply will fail")
        conn = _ScriptedConn(messages=[(p, "text", 1024 * 1024)])
        conn.send_error = OSError("pipe dead")
        _worker_main(conn)  # must return, not raise

    def test_unknown_kind_reports_error(self):
        p = write_text(os.path.join(self.td, "x.bin"), "binary-ish")
        conn = _ScriptedConn(messages=[(p, "unknown", 1024), None])
        _worker_main(conn)
        ok, msg = conn.sent[1]
        self.assertFalse(ok)
        self.assertIn("unsupported file kind", msg)


if __name__ == "__main__":
    unittest.main()
