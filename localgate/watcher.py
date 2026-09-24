"""Incremental file watcher: polling-based, whitelist-scoped, incremental.

The watcher never touches user files; it only reads directory listings and
file stats, then asks the ingestor to index new/changed files and to drop
index entries for files that disappeared.
"""

from __future__ import annotations

import threading


class FileWatcher(threading.Thread):
    def __init__(self, cfg, ingestor, poll_interval_s: float | None = None):
        super().__init__(name="localgate-watcher", daemon=True)
        self.cfg = cfg
        self.ingestor = ingestor
        self.interval = max(1.0, float(poll_interval_s or cfg["watcher"]["interval_s"]))
        self.stop_event = threading.Event()
        self._wakeup = threading.Event()  # interrupts the inter-pass sleep
        self.error_count = 0
        self.last_error: str | None = None
        self.last_pass: dict | None = None
        self._rescan_requested = threading.Event()

    def request_rescan(self) -> bool:
        if self.ingestor.progress.scanning:
            return False
        self._rescan_requested.set()
        self._wakeup.set()  # don't wait out the full sleep interval
        return True

    def request_stop(self) -> None:
        self.stop_event.set()
        self._wakeup.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            triggered = self._rescan_requested.is_set()
            self._rescan_requested.clear()
            try:
                summary = self.ingestor.scan_whitelist(reason="manual" if triggered
                                                       else "watch")
                self.last_pass = summary
            except Exception as e:  # watcher must never die
                self.error_count += 1
                self.last_error = f"{type(e).__name__}: {e}"
            self._wakeup.wait(self.interval)
            self._wakeup.clear()

    def stats(self) -> dict:
        return {
            "enabled": bool(self.cfg["watcher"]["enabled"]),
            "interval_s": self.interval,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_pass": self.last_pass,
            "alive": self.is_alive(),
        }
