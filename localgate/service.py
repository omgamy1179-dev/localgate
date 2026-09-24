"""Service wiring: config -> store -> embedder -> ocr -> ingest -> watcher ->
self-check -> HTTP API. Owns lifecycle and the shared status snapshot."""

from __future__ import annotations

import os
import signal
import threading
import time

from . import __version__
from .config import effective_public_config
from .embedding import make_embedder
from .httpapi import ApiServer
from .ingest import Ingestor, IngestProgress
from .jsonllog import JsonlLogger
from .ocr import OcrEngine
from .search import HybridSearcher
from .selfcheck import SelfCheckDaemon
from .store import IndexStore
from .watcher import FileWatcher


class LocalGateService:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.version = __version__
        self._started_at = time.time()
        self._stop_once = threading.Event()

        os.makedirs(cfg["index"]["data_dir"], exist_ok=True)
        os.makedirs(cfg["logs"]["dir"], exist_ok=True)

        self.service_log = JsonlLogger(cfg["logs"]["dir"], "localgate-service",
                                       cfg["logs"]["max_mb"] * 1024 * 1024,
                                       cfg["logs"]["backups"])
        self.selfcheck_log = JsonlLogger(cfg["logs"]["dir"], "localgate-selfcheck",
                                         cfg["logs"]["max_mb"] * 1024 * 1024,
                                         cfg["logs"]["backups"])

        self.store = IndexStore(cfg["index"]["data_dir"])
        for err in self.store.load_errors:
            self.service_log.write({"timestamp": _now(), "event": "index_load_error",
                                    "error": err})

        self.embedder = make_embedder(cfg)
        self.ocr = OcrEngine(mode=cfg["ocr"]["mode"], languages=cfg["ocr"]["languages"],
                             timeout_s=cfg["ocr"]["timeout_s"],
                             helper_cache_dir=os.path.join(cfg["index"]["data_dir"], "bin"))
        self.progress = IngestProgress()
        self.protected_dirs = [cfg["index"]["data_dir"], cfg["logs"]["dir"]]
        self.ingestor = Ingestor(cfg, self.store, self.embedder, self.ocr,
                                 self.progress, self.protected_dirs)
        self.searcher = HybridSearcher(self.store, self.embedder, cfg)
        self.watcher = FileWatcher(cfg, self.ingestor)
        self.http = ApiServer(self)
        self.selfcheck = SelfCheckDaemon(cfg, self.store, self.ingestor, self.embedder,
                                         self.ocr, self.watcher,
                                         self.http.base_url, self.service_log,
                                         self.selfcheck_log)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        wl = self.cfg["paths"]["whitelist"]
        self.http.start()
        self.service_log.write({
            "timestamp": _now(), "event": "service_start", "version": self.version,
            "port": self.cfg["server"]["port"],
            "whitelist": wl,
            "whitelist_empty": not wl,
            "embedding_backend": self.cfg["embedding"]["backend"],
            "note": "" if wl else "whitelist is empty: nothing will be indexed "
                                  "(add directories to config.yaml paths.whitelist)",
        })
        if not wl:
            print("[localgate] WARNING: whitelist is empty - nothing will be indexed. "
                  "Edit paths.whitelist in config.yaml.", flush=True)
        if self.cfg["watcher"]["enabled"]:
            self.watcher.start()
        if self.cfg["selfcheck"]["enabled"]:
            self.selfcheck.start()
        print(f"[localgate] serving on {self.http.base_url} "
              f"(docs indexed: {len(self.store.docs)})", flush=True)

    def stop(self, join_timeout_s: float = 30.0) -> None:
        if self._stop_once.is_set():
            return
        self._stop_once.set()
        try:
            self.selfcheck.request_stop()
            self.watcher.request_stop()
            # wait for in-flight scans/rounds so the final save below captures
            # every accepted write (the store lock serializes the last writes)
            for worker in (self.watcher, self.selfcheck):
                if worker.ident is not None:  # never started -> nothing to join
                    worker.join(timeout=join_timeout_s)
            self.store.save_all()
            self.http.stop()
        finally:
            self.service_log.write({"timestamp": _now(), "event": "service_stop",
                                    "uptime_s": round(self.uptime(), 1)})

    # ------------------------------------------------------------- rescan

    def request_rescan(self) -> dict:
        """Trigger one whitelist scan and report the REAL dispatch state.

        Returns a dict the client can observe:
        - watcher thread alive: request is picked up on its next tick;
        - watcher disabled/stopped: a one-shot background scan thread runs;
        - a scan already running: nothing new is started.
        Progress/completion is observable via GET /api/status -> ingest.scanning
        and watcher.last_pass."""
        if self.ingestor.progress.scanning:
            return {"rescan_started": False,
                    "reason": "a scan is already running (see GET /api/status)"}
        if self.watcher.is_alive():
            requested = self.watcher.request_rescan()
            return {"rescan_started": bool(requested), "mode": "watcher",
                    "reason": "" if requested else "watcher queue rejected the request"}
        if not self.cfg["paths"]["whitelist"]:
            return {"rescan_started": False, "mode": "oneshot",
                    "reason": "whitelist is empty; nothing to scan"}
        t = threading.Thread(target=self._oneshot_scan, name="localgate-rescan",
                             daemon=True)
        t.start()
        return {"rescan_started": True, "mode": "oneshot", "reason": ""}

    def _oneshot_scan(self) -> None:
        try:
            summary = self.ingestor.scan_whitelist(reason="manual")
            self.watcher.last_pass = summary
        except Exception as e:  # must never take the service down
            self.service_log.write({"timestamp": _now(), "event": "oneshot_scan_error",
                                    "error": f"{type(e).__name__}: {e}"})

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def wait_forever(self) -> None:
        try:
            while not self._stop_once.is_set():
                time.sleep(0.3)
        except KeyboardInterrupt:
            self.stop()

    def uptime(self) -> float:
        return time.time() - self._started_at

    # ------------------------------------------------------------- status

    def public_config(self) -> dict:
        return effective_public_config(self.cfg)

    def status_snapshot(self) -> dict:
        ocr_engine = self.ocr.available()
        return {
            "service": "localgate",
            "version": self.version,
            "uptime_s": round(self.uptime(), 1),
            "config": self.public_config(),
            "index": self.store.stats(),
            "ingest": self.progress.snapshot(),
            "watcher": self.watcher.stats(),
            "selfcheck": self.selfcheck.stats(),
            "ocr": {"mode": self.cfg["ocr"]["mode"], "engine_available": ocr_engine},
            "embedding": {
                "backend": self.cfg["embedding"]["backend"],
                "model": (self.cfg["embedding"]["model"]
                          if self.cfg["embedding"]["backend"] == "ollama" else
                          f"local-hash dim={self.cfg['embedding']['dim']}"),
            },
        }


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
