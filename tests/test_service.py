"""Service lifecycle tests: rescan truthfulness with watcher on/off, clean
stop persistence, scan-in-progress guard."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

from tests.helpers import build_service, make_cfg
from tests.make_samples import write_text


def wait_until(fn, timeout: float = 15.0, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if fn():
                return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(interval)
    raise AssertionError(f"condition not met in {timeout}s: {last}")


class RescanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name
        self.vault = os.path.join(self.td, "vault")
        os.makedirs(self.vault)
        write_text(os.path.join(self.vault, "note.md"),
                   "# note\n\nrescan lifecycle marker content.")

    def test_rescan_with_watcher_alive(self):
        svc = build_service(self.td, paths={"whitelist": [self.vault]},
                            watcher={"enabled": True, "interval_s": 3600})
        svc.http.start()
        self.addCleanup(svc.stop)
        svc.watcher.start()
        wait_until(lambda: not svc.ingestor.progress.scanning)  # first pass done
        wait_until(lambda: len(svc.store.docs) == 1)
        state = svc.request_rescan()
        self.assertTrue(state["rescan_started"])
        self.assertEqual(state["mode"], "watcher")
        wait_until(lambda: svc.watcher.last_pass and
                   svc.watcher.last_pass.get("reason") == "manual" and
                   not svc.ingestor.progress.scanning)

    def test_rescan_without_watcher_oneshot_scan(self):
        """watcher disabled: rescan must REALLY scan, not fake success."""
        svc = build_service(self.td, paths={"whitelist": [self.vault]},
                            watcher={"enabled": False})
        svc.http.start()
        self.addCleanup(svc.stop)
        self.assertFalse(svc.watcher.is_alive())
        state = svc.request_rescan()
        self.assertTrue(state["rescan_started"], state)
        self.assertEqual(state["mode"], "oneshot")
        wait_until(lambda: len(svc.store.docs) == 1, timeout=30)
        self.assertEqual(svc.watcher.last_pass.get("ingested"), 1)

    def test_rescan_empty_whitelist_reports_refusal(self):
        svc = build_service(self.td, paths={"whitelist": []},
                            watcher={"enabled": False})
        svc.http.start()
        self.addCleanup(svc.stop)
        state = svc.request_rescan()
        self.assertFalse(state["rescan_started"])
        self.assertIn("whitelist is empty", state["reason"])

    def test_rescan_during_scan_reports_busy(self):
        svc = build_service(self.td, paths={"whitelist": [self.vault]},
                            watcher={"enabled": True, "interval_s": 3600})
        svc.http.start()
        self.addCleanup(svc.stop)
        svc.watcher.start()
        # hold the scanning flag as if a scan were running
        with svc.ingestor.progress.lock:
            svc.ingestor.progress.scanning = True
            state = svc.request_rescan()
            svc.ingestor.progress.scanning = False
        self.assertFalse(state["rescan_started"])
        self.assertIn("already running", state["reason"])


class StopLifecycleCase(unittest.TestCase):
    def test_clean_stop_persists_last_ingest(self):
        with tempfile.TemporaryDirectory() as td:
            vault = os.path.join(td, "vault")
            os.makedirs(vault)
            write_text(os.path.join(vault, "note.md"),
                       "# note\n\nrescan lifecycle marker content.")
            svc = build_service(td, paths={"whitelist": [vault]},
                                watcher={"enabled": True, "interval_s": 3600})
            svc.http.start()
            svc.watcher.start()
            wait_until(lambda: len(svc.store.docs) == 1)
            # ingest one more file and stop immediately - the final save must
            # capture it even though the watcher never ticked again
            write_text(os.path.join(vault, "late.md"),
                       "late arrival before clean shutdown marker.")
            svc.ingestor.ingest_file(os.path.join(vault, "late.md"))
            svc.stop()
            self.assertEqual(len(svc.store.docs), 2)
            # restart on the same data dir: both docs survive
            svc2 = build_service(td, paths={"whitelist": [vault]},
                                 watcher={"enabled": True, "interval_s": 3600})
            self.assertEqual(len(svc2.store.docs), 2)
            svc2.stop()

    def test_stop_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            svc = build_service(td)
            svc.stop()
            svc.stop()  # second call must be a no-op, not an error

    def test_unreachable_whitelist_root_never_clears_index(self):
        """A scan with NO accessible whitelist root must leave the index alone."""
        with tempfile.TemporaryDirectory() as td:
            vault = os.path.join(td, "vault")
            os.makedirs(vault)
            write_text(os.path.join(vault, "keep.md"), "precious content marker.")
            svc = build_service(td, paths={"whitelist": [vault]},
                                watcher={"enabled": False})
            svc.ingestor.scan_whitelist(reason="initial")
            self.assertEqual(len(svc.store.docs), 1)
            # the whitelist root disappears (unmounted volume / renamed dir)
            svc.cfg["paths"]["whitelist"] = [os.path.join(vault, "now-gone")]
            summary = svc.ingestor.scan_whitelist(reason="unmount")
            self.assertIn("left unchanged", summary.get("skipped", ""))
            self.assertEqual(len(svc.store.docs), 1,
                            "index was mass-cleared by an unavailable root")
            # parent dir missing => 'unreachable', kept; parent present =>
            # confirmed deletion, removed
            os.remove(os.path.join(vault, "keep.md"))
            svc.cfg["paths"]["whitelist"] = [vault]
            summary = svc.ingestor.scan_whitelist(reason="confirmed")
            self.assertEqual(summary.get("removed"), 1)
            self.assertEqual(len(svc.store.docs), 0)
            svc.stop()

    def test_watcher_disabled_service_flags_it(self):
        with tempfile.TemporaryDirectory() as td:
            svc = build_service(td, watcher={"enabled": False})
            svc.http.start()
            svc.stop()
            self.assertFalse(svc.watcher.stats()["alive"])
            self.assertFalse(svc.watcher.stats()["enabled"])


if __name__ == "__main__":
    unittest.main()


class SignalShutdownCase(unittest.TestCase):
    """A real service process must shut down cleanly on SIGINT/SIGTERM and
    persist everything ingested so far (POSIX only; Windows kills differ)."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name

    def _write_service_cfg(self, td: str, vault: str) -> tuple[str, int]:
        from tests.helpers import free_port, write_config
        port = free_port()
        cfg = make_cfg(td, port=port,
                       paths={"whitelist": [vault]},
                       watcher={"enabled": True, "interval_s": 2},
                       selfcheck={"enabled": False})
        cfg["index"]["data_dir"] = os.path.join(td, "data")
        cfg["logs"]["dir"] = os.path.join(td, "logs")
        cfg_path = os.path.join(td, "config.yaml")
        write_config(cfg_path, cfg)
        return cfg_path, port

    @unittest.skipIf(os.name == "nt", "POSIX signal semantics")
    def test_sigint_graceful_stop_persists_state(self):
        import signal as signal_mod
        import subprocess as subprocess_mod
        import urllib.request

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "sig.md"), "signal shutdown content marker")
        cfg_path, port = self._write_service_cfg(self.td, vault)
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        proc = subprocess_mod.Popen(
            [sys.executable, "-m", "localgate.cli", "serve", "--config", cfg_path],
            cwd=root, env=env, stdout=subprocess_mod.DEVNULL, stderr=subprocess_mod.DEVNULL)
        try:
            base = f"http://127.0.0.1:{port}"

            def healthy():
                try:
                    with urllib.request.build_opener(
                            urllib.request.ProxyHandler({})).open(
                                base + "/health", timeout=2) as resp:
                        return resp.status == 200
                except OSError:
                    return False

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not healthy():
                time.sleep(0.2)
            self.assertTrue(healthy(), "service did not come up")

            def indexed():
                try:
                    with urllib.request.build_opener(
                            urllib.request.ProxyHandler({})).open(
                                base + "/api/status", timeout=2) as resp:
                        return json.loads(resp.read())["index"]["docs"] == 1
                except (OSError, ValueError):
                    return False

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not indexed():
                time.sleep(0.2)
            self.assertTrue(indexed(), "vault doc not indexed")

            proc.send_signal(signal_mod.SIGINT)
            self.assertEqual(proc.wait(timeout=20), 0, "unclean exit on SIGINT")

            # everything ingested before the signal survives a restart
            svc2_resumed = build_service(self.td,
                                         paths={"whitelist": [vault]},
                                         watcher={"enabled": False})
            self.assertEqual(len(svc2_resumed.store.docs), 1)
            svc2_resumed.stop()
        finally:
            if proc.poll() is None:
                proc.kill()
