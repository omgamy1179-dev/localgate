"""Background self-check daemon (core requirement, README section 5).

Each round runs every configured check item, applies only SAFE automatic
optimizations (orphan cleanup, segment merge, single-file reindex), and writes
one structured JSONL record. It never modifies user files, never adds
whitelist entries, and never deletes user data. Consecutive severe errors
trigger a backoff (slower checks) instead of a crash.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

from .httpclient import read_upstream_text, urlopen_noproxy
from .ingest import _now_iso


class SelfCheckDaemon(threading.Thread):
    ITEMS = ("file_sources", "index_integrity", "performance", "embedding_health",
             "services_alive", "resource_thresholds", "optimization")

    def __init__(self, cfg, store, ingestor, embedder, ocr_engine, watcher,
                 http_base: str, service_log, selfcheck_log):
        super().__init__(name="localgate-selfcheck", daemon=True)
        self.cfg = cfg
        self.store = store
        self.ingestor = ingestor
        self.embedder = embedder
        self.ocr = ocr_engine
        self.watcher = watcher
        self.http_base = http_base
        self.service_log = service_log
        self.log = selfcheck_log
        self.stop_event = threading.Event()
        self.round_no = 0
        self.consecutive_errors = 0
        self.current_interval = float(cfg["selfcheck"]["interval_s"])
        self.last_entry: dict | None = None
        self.search_latency_ms: float | None = None

    # ------------------------------------------------------------------ helpers

    def request_stop(self) -> None:
        self.stop_event.set()

    def _pace(self) -> None:
        delay = self.cfg["selfcheck"]["item_delay_ms"] / 1000.0
        if delay > 0:
            self.stop_event.wait(delay)

    def _http_ping(self) -> tuple[bool, int, str]:
        url = self.http_base + "/health"
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, method="GET")
            with urlopen_noproxy(req, timeout=5) as resp:
                body = read_upstream_text(resp, timeout_s=5, max_bytes=65536,
                                          encoding="utf-8", errors="replace")
                ms = int((time.monotonic() - t0) * 1000)
                if resp.status == 200 and '"ok"' in body:
                    return True, ms, "ok"
                return False, ms, f"http {resp.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return False, int((time.monotonic() - t0) * 1000), f"unreachable: {e}"

    def _probe_search_latency(self) -> tuple[float | None, str | None]:
        t0 = time.monotonic()
        try:
            self.ingestor.store.search_fulltext("localgate selfcheck probe", top_n=3)
            ms = (time.monotonic() - t0) * 1000
            return ms, None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------ round

    def run_round(self) -> dict:
        items: list[dict] = []
        errors: list[str] = []
        actions: list[str] = []
        metrics: dict = {}
        warnings = 0
        self.round_no += 1

        # 1. file sources -------------------------------------------------
        try:
            confirmed, unreachable = self.store.stale_docs_missing_files()
            removed = 0
            for doc_id in confirmed:
                if self.store.remove_doc(doc_id):
                    removed += 1
                    actions.append(f"removed stale index entry {doc_id}")
            if confirmed:
                self.store.save_all()
            if unreachable:
                # volume/mount/permission problem: report, never purge
                warnings += 1
            items.append({"item": "file_sources",
                          "status": "warning" if unreachable else "ok",
                          "detail": {"confirmed_missing": len(confirmed),
                                     "removed": removed,
                                     "unreachable_kept": len(unreachable)}})
        except Exception as e:
            errors.append(f"file_sources: {e}")
            items.append({"item": "file_sources", "status": "error",
                          "detail": str(e)[:200]})
        self._pace()

        # 2. index integrity ----------------------------------------------
        try:
            broken = self.store.broken_docs()
            if broken:
                self.ingestor.retry_failed_docs(only_doc_ids=broken)
                for d in broken:
                    actions.append(f"reindexed broken doc {d}")
            load_errors = list(self.store.load_errors)
            items.append({"item": "index_integrity",
                          "status": "warning" if broken else "ok",
                          "detail": {"broken_docs": broken, "load_errors": load_errors[:5],
                                     "reindex_attempted": len(broken)}})
            if broken:
                warnings += 1
        except Exception as e:
            errors.append(f"index_integrity: {e}")
            items.append({"item": "index_integrity", "status": "error",
                          "detail": str(e)[:200]})
        self._pace()

        # 3. performance metrics -------------------------------------------
        try:
            ms, err = self._probe_search_latency()
            if ms is not None:
                self.search_latency_ms = ms
            stats = self.store.stats()
            metrics.update({
                "search_latency_ms": round(ms, 2) if ms is not None else None,
                "index_size_bytes": stats["index_size_bytes"],
                "chunks": stats["chunks"],
                "vectors": stats["vectors"],
                "docs": stats["docs"],
                "ocr_failures": self.ingestor.progress.ocr_failures,
                "watcher_errors": self.watcher.error_count,
            })
            items.append({"item": "performance", "status": "ok",
                          "detail": {"latency_probe_error": err}})
        except Exception as e:
            errors.append(f"performance: {e}")
            items.append({"item": "performance", "status": "error",
                          "detail": str(e)[:200]})
        self._pace()

        # 4. embedding health ------------------------------------------------
        try:
            ok, ms, detail = self.embedder.health()
            metrics["embedding_latency_ms"] = ms
            items.append({"item": "embedding_health",
                          "status": "ok" if ok else "warning", "detail": detail})
            if not ok:
                warnings += 1
        except Exception as e:
            ok = False
            errors.append(f"embedding_health: {e}")
            items.append({"item": "embedding_health", "status": "error",
                          "detail": str(e)[:200]})
        self._pace()

        # 5. services alive ----------------------------------------------------
        try:
            alive, ms, detail = self._http_ping()
            metrics["http_selfping_ms"] = ms
            mcp_note = "stdio server launched on demand by MCP clients; backend reachable" \
                if alive else "backend unreachable"
            items.append({"item": "services_alive",
                          "status": "ok" if alive else "error",
                          "detail": {"http": detail, "mcp": mcp_note}})
            if not alive:
                errors.append("services_alive: http self-ping failed")
        except Exception as e:
            errors.append(f"services_alive: {e}")
            items.append({"item": "services_alive", "status": "error",
                          "detail": str(e)[:200]})
        self._pace()

        # 6. resource thresholds ------------------------------------------------
        try:
            size_mb = self.store.index_size_bytes() / (1024 * 1024)
            warn_mb = self.cfg["selfcheck"]["index_size_warn_mb"]
            over = size_mb > warn_mb
            metrics["index_size_mb"] = round(size_mb, 2)
            metrics["index_size_warn_mb"] = warn_mb
            items.append({"item": "resource_thresholds",
                          "status": "warning" if over else "ok",
                          "detail": {"index_size_mb": round(size_mb, 2),
                                     "policy": "alert only; user data is never auto-deleted"}})
            if over:
                warnings += 1
        except Exception as e:
            errors.append(f"resource_thresholds: {e}")
            items.append({"item": "resource_thresholds", "status": "error",
                          "detail": str(e)[:200]})
        self._pace()

        # 7. safe automatic optimizations -----------------------------------------
        try:
            orphans = self.store.orphan_cleanup()
            if any(orphans.values()):
                actions.append(f"orphan cleanup: {orphans}")
            merged = self.store.merge_small_segments()
            if merged:
                actions.append(f"merged {merged} fragmented vector segment(s)")
            retry = self.ingestor.retry_failed_docs()
            if retry["retried"]:
                actions.append(f"retried failed docs: {retry}")
            items.append({"item": "optimization", "status": "ok",
                          "detail": {"orphans": orphans, "segments_merged": merged,
                                     "failed_doc_retry": retry}})
            if any(orphans.values()) or merged:
                self.store.save_all()
        except Exception as e:
            errors.append(f"optimization: {e}")
            items.append({"item": "optimization", "status": "error",
                          "detail": str(e)[:200]})

        # aggregate ----------------------------------------------------------------
        item_statuses = [i["status"] for i in items]
        if "error" in item_statuses:
            status = "error"
        elif warnings > 0 or "warning" in item_statuses:
            status = "warning"
        else:
            status = "ok"

        if status == "error":
            self.consecutive_errors += 1
        else:
            self.consecutive_errors = 0
        suggestions = ""
        threshold = self.cfg["selfcheck"]["consecutive_error_threshold"]
        if self.consecutive_errors >= threshold:
            base = self.cfg["selfcheck"]["interval_s"]
            backoff = min(base * (2 ** min(self.consecutive_errors, 10)),
                          self.cfg["selfcheck"]["backoff_max_s"])
            self.current_interval = float(backoff)
            suggestions = (f"{self.consecutive_errors} consecutive error rounds; "
                           f"check interval backed off to {backoff}s")
        else:
            self.current_interval = float(self.cfg["selfcheck"]["interval_s"])
            if status == "warning":
                bad = [i["item"] for i in items if i["status"] != "ok"]
                suggestions = f"attention needed: {', '.join(bad)}"

        entry = {
            "timestamp": _now_iso(),
            "check_round": self.round_no,
            "status": status,
            "check_items": items,
            "metrics": metrics,
            "optimization_actions": actions,
            "errors": errors,
            "suggestions": suggestions,
        }
        self.log.write(entry)
        self.last_entry = entry
        return entry

    # ------------------------------------------------------------------ thread

    def run(self) -> None:
        # low-CPU cooperative loop: paced sleeps between items and between rounds
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                entry = self.run_round()
                self.service_log.write({"timestamp": _now_iso(), "event": "selfcheck_round",
                                        "round": entry["check_round"],
                                        "status": entry["status"]})
            except Exception as e:  # never crash the service from the loop
                self.service_log.write({"timestamp": _now_iso(),
                                        "event": "selfcheck_crashed",
                                        "error": f"{type(e).__name__}: {e}",
                                        "traceback": traceback_fmt(e)})
            elapsed = time.monotonic() - started
            wait = max(0.2, self.current_interval - elapsed)
            self.stop_event.wait(wait)

    def stats(self) -> dict:
        return {
            "enabled": bool(self.cfg["selfcheck"]["enabled"]),
            "rounds": self.round_no,
            "interval_configured_s": self.cfg["selfcheck"]["interval_s"],
            "current_interval_s": self.current_interval,
            "consecutive_errors": self.consecutive_errors,
            "last_status": (self.last_entry or {}).get("status"),
            "last_round_at": (self.last_entry or {}).get("timestamp"),
            "alive": self.is_alive(),
        }


def traceback_fmt(e: Exception) -> str:
    import traceback
    return "".join(traceback.format_exception(type(e), e, e.__traceback__))[-800:]
