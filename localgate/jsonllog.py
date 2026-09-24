"""JSON Lines structured logging with size/date rotation.

Log files never contain raw user file content - only paths, sizes, counts,
statuses and error messages.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime


class JsonlLogger:
    """Append-only JSONL logger. Rotates by date in the filename and by size."""

    def __init__(self, log_dir: str, base_name: str, max_bytes: int = 10 * 1024 * 1024,
                 backups: int = 5):
        self.log_dir = os.path.abspath(log_dir)
        self.base_name = base_name
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.backups = max(1, int(backups))
        self._lock = threading.Lock()
        self._path: str | None = None
        self._day: str | None = None
        os.makedirs(self.log_dir, exist_ok=True)

    def _file_for_today(self) -> str:
        day = datetime.now().strftime("%Y%m%d")
        if day != self._day or self._path is None:
            self._day = day
            self._path = os.path.join(self.log_dir, f"{self.base_name}-{day}.log")
        return self._path

    def _rotate_if_needed(self, path: str) -> None:
        try:
            if os.path.getsize(path) < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.backups - 1, 0, -1):
            src = f"{path}.{i}"
            dst = f"{path}.{i + 1}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        try:
            os.replace(path, path + ".1")
        except OSError:
            pass

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            path = self._file_for_today()
            self._rotate_if_needed(path)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                # Logging must never take the service down.
                pass

    def read_recent(self, lines: int = 50) -> list[dict]:
        """Return up to `lines` newest records across today's and previous logs."""
        out: list[dict] = []
        with self._lock:
            try:
                names = sorted(
                    (n for n in os.listdir(self.log_dir)
                     if n.startswith(self.base_name + "-") and n.endswith(".log")),
                    reverse=True,
                )
            except OSError:
                return out
            for name in names:
                path = os.path.join(self.log_dir, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        file_lines = f.readlines()
                except OSError:
                    continue
                for ln in reversed(file_lines):
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
                    if len(out) >= lines:
                        break
                if len(out) >= lines:
                    break
        out.reverse()
        return out
