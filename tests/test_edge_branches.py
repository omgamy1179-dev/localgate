"""Branch-edge tests for modules that platform coverage runs left slightly
under the combined-coverage gate: CLI config errors, watcher resilience,
JSONL log rotation/reading edges, loopback URL validation errors and OCR
engine probe branches. All loopback/local, no external dependencies."""

from __future__ import annotations

import contextlib
import io
import os
import socket
import tempfile
import time
import unittest
from unittest import mock

from localgate.fsutil import is_under
from tests.helpers import make_cfg


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name


# ------------------------------------------------------------------ cli

class TestCliConfigError(TempCase):
    def test_bad_config_exits_2_with_message(self):
        from localgate.cli import _cfg
        bad = os.path.join(self.td, "bad.yaml")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("index:\n  max_file_mb: banana\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                _cfg(bad)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("config error", err.getvalue())


# ------------------------------------------------------------------ watcher

class TestWatcherResilience(TempCase):
    def _watcher(self, scan_impl):
        from localgate.ingest import IngestProgress
        from localgate.watcher import FileWatcher

        class FakeIngestor:
            progress = IngestProgress()
            scan_whitelist = staticmethod(scan_impl)

        cfg = make_cfg(self.td)
        cfg["watcher"]["interval_s"] = 1
        watcher = FileWatcher(cfg, FakeIngestor(), poll_interval_s=1)
        return watcher

    def test_request_rescan_rejected_while_scanning(self):
        watcher = self._watcher(lambda reason="manual": {})
        watcher.ingestor.progress.scanning = True
        self.assertFalse(watcher.request_rescan())
        self.assertFalse(watcher._rescan_requested.is_set())

    def test_run_survives_scan_exception(self):
        def boom(reason="manual"):
            raise RuntimeError("scan exploded")

        watcher = self._watcher(boom)
        watcher.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and watcher.error_count == 0:
            time.sleep(0.05)
        watcher.request_stop()
        watcher.join(timeout=10)
        self.assertFalse(watcher.is_alive())
        self.assertGreaterEqual(watcher.error_count, 1)
        self.assertIn("RuntimeError", watcher.last_error or "")
        self.assertTrue(watcher.stats()["alive"] is False)


# ------------------------------------------------------------------ jsonllog

class TestJsonlLogEdges(TempCase):
    def _logger(self):
        from localgate.jsonllog import JsonlLogger
        return JsonlLogger(self.td, "edge", max_bytes=1, backups=3)

    def test_rotation_survives_replace_failures(self):
        log = self._logger()
        log.write({"event": "before-rotation"})
        with mock.patch.object(os, "replace", side_effect=OSError("locked")):
            log.write({"event": "during-rotation"})  # must not raise
        self.assertTrue(any(n.endswith(".log") for n in os.listdir(self.td)))

    def test_read_recent_survives_listdir_failure(self):
        log = self._logger()
        log.write({"event": "one"})
        with mock.patch.object(os, "listdir", side_effect=OSError("gone")):
            self.assertEqual(log.read_recent(5), [])

    def test_read_recent_skips_unreadable_and_blank_lines(self):
        log = self._logger()
        log.write({"event": "real"})
        # a rotated name that is a directory: open() raises IsADirectoryError
        os.makedirs(os.path.join(self.td, "edge.log.1"), exist_ok=True)
        out = log.read_recent(10)
        self.assertTrue(any(e.get("event") == "real" for e in out))


# ------------------------------------------------------------------ httpclient

class TestLoopbackUrlValidation(TempCase):
    def test_rejects_non_empty_path(self):
        from localgate.httpclient import validate_loopback_http_url
        with self.assertRaises(ValueError) as ctx:
            validate_loopback_http_url("http://127.0.0.1:1/api", name="ollama")
        self.assertIn("path must be empty", str(ctx.exception))

    def test_rejects_missing_host(self):
        from localgate.httpclient import validate_loopback_http_url
        with self.assertRaises(ValueError) as ctx:
            validate_loopback_http_url("http://", name="ollama")
        self.assertIn("no host", str(ctx.exception))

    def test_unresolvable_host_reports_resolution_failure(self):
        from localgate.httpclient import validate_loopback_http_url
        with mock.patch.object(socket, "getaddrinfo",
                               side_effect=socket.gaierror(8, "nx")):
            with self.assertRaises(ValueError) as ctx:
                validate_loopback_http_url("http://somehost/", name="ollama")
        self.assertIn("could not be resolved", str(ctx.exception))

    def test_empty_resolution_reports_no_addresses(self):
        from localgate.httpclient import validate_loopback_http_url
        with mock.patch.object(socket, "getaddrinfo", return_value=[]):
            with self.assertRaises(ValueError) as ctx:
                validate_loopback_http_url("http://somehost/", name="ollama")
        self.assertIn("resolved to no addresses", str(ctx.exception))

    def test_non_ip_resolution_is_rejected(self):
        from localgate.httpclient import validate_loopback_http_url
        fake = [(2, 1, 6, "", ("definitely-not-an-ip", 80))]
        with mock.patch.object(socket, "getaddrinfo", return_value=fake):
            with self.assertRaises(ValueError) as ctx:
                validate_loopback_http_url("http://somehost/", name="ollama")
        self.assertIn("non-IP address", str(ctx.exception))

    def test_find_socket_tolerates_unusual_response_objects(self):
        import types

        from localgate.httpclient import _find_socket
        self.assertIsNone(_find_socket(types.SimpleNamespace()))
        self.assertIsNone(_find_socket(None))


# ------------------------------------------------------------------ ocr

class TestOcrProbeBranches(TempCase):
    def _engine(self, **kw):
        from localgate.ocr import OcrEngine
        return OcrEngine(mode=kw.pop("mode", "auto"),
                         helper_cache_dir=os.path.join(self.td, "bin"), **kw)

    def test_vision_helper_marks_broken_without_swiftc(self):
        eng = self._engine()
        with mock.patch("localgate.ocr.shutil.which", return_value=None):
            self.assertIsNone(eng._vision_helper())
        self.assertTrue(eng._vision_broken)

    def test_vision_helper_marks_broken_on_compile_failure(self):
        eng = self._engine()
        proc = mock.Mock(returncode=1)
        with mock.patch("localgate.ocr.shutil.which", return_value="/usr/bin/swiftc"), \
                mock.patch("localgate.ocr.subprocess.run", return_value=proc):
            self.assertIsNone(eng._vision_helper())
        self.assertTrue(eng._vision_broken)

    def test_vision_helper_marks_broken_on_compile_timeout(self):
        import subprocess

        eng = self._engine()
        with mock.patch("localgate.ocr.shutil.which", return_value="/usr/bin/swiftc"), \
                mock.patch("localgate.ocr.subprocess.run",
                           side_effect=subprocess.TimeoutExpired(cmd="swiftc",
                                                                 timeout=1)):
            self.assertIsNone(eng._vision_helper())
        self.assertTrue(eng._vision_broken)

    def test_available_uses_cached_helper_without_probe(self):
        eng = self._engine()
        eng._helper_path = os.path.join(self.td, "helper")
        with mock.patch.object(eng, "_start_background_probe") as probe:
            self.assertEqual(eng.available(), "vision")
            probe.assert_not_called()

    def test_available_short_circuits_on_broken_vision(self):
        eng = self._engine()
        eng._vision_broken = True
        with mock.patch.object(eng, "_tesseract_bin", return_value=None), \
                mock.patch.object(eng, "_start_background_probe") as probe:
            self.assertIsNone(eng.available())
            probe.assert_not_called()

    def test_available_blocking_mode_off_is_none(self):
        eng = self._engine(mode="off")
        self.assertIsNone(eng.available_blocking())

    def test_available_blocking_prefers_tesseract(self):
        eng = self._engine()
        with mock.patch.object(eng, "_tesseract_bin", return_value="/usr/bin/tesseract"):
            self.assertEqual(eng.available_blocking(), "tesseract")

    def test_background_probe_never_raises(self):
        eng = self._engine()
        with mock.patch.object(eng, "available_blocking",
                               side_effect=RuntimeError("boom")):
            eng._background_probe()  # must swallow

    def test_tesseract_spawn_failure_maps_to_ocr_error(self):
        from localgate.ocr import OcrError
        eng = self._engine(timeout_s=5)
        with mock.patch.object(eng, "_tesseract_bin",
                               return_value="/usr/bin/tesseract"), \
                mock.patch("localgate.ocr.subprocess.run",
                           side_effect=OSError("no exec")):
            with self.assertRaises(OcrError) as ctx:
                eng.recognize("/tmp/x.png")
        self.assertIn("spawn failed", str(ctx.exception))


# ------------------------------------------------------------------ fsutil

class TestIsUnderEdges(unittest.TestCase):
    def test_relative_escape_is_not_under(self):
        self.assertFalse(is_under("../outside/x", "/base"))
        self.assertTrue(is_under("/base", "/base"))
        self.assertTrue(is_under("/base/sub/f", "/base"))
