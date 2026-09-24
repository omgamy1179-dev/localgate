"""Pipeline-level tests: ingest orchestration, OCR engine behaviour with
stubbed engines, the Ollama embedder against a local stub server, CLI paths
and remaining extraction fallbacks. All loopback, all temporary."""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from localgate.embedding import EmbedError, LocalHashEmbedder, OllamaEmbedder, make_embedder
from localgate.extract import (
    ExtractError,
    classify,
    extract,
    extract_chat_json,
    extract_docx,
    extract_pdf,
    read_file_bytes,
)
from localgate.ingest import Ingestor, IngestProgress, confirmed_gone, doc_id_for
from localgate.ocr import OcrEngine, OcrError, OcrUnavailable
from localgate.store import IndexStore
from tests.helpers import make_cfg
from tests.make_samples import make_chat_export, make_docx, make_pdf, make_png, write_text


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name

    def _ingestor(self, whitelist: list[str]) -> tuple[Ingestor, IndexStore]:
        cfg = make_cfg(self.td, paths={"whitelist": whitelist})
        store = IndexStore(cfg["index"]["data_dir"])
        from localgate.embedding import LocalHashEmbedder
        ing = Ingestor(cfg, store, LocalHashEmbedder(64), OcrEngine(mode="off"),
                       IngestProgress(), protected_dirs=[cfg["index"]["data_dir"],
                                                         cfg["logs"]["dir"]])
        return ing, store


# ------------------------------------------------------------------ ingest

class TestIngestPipeline(TempCase):
    def test_full_scan_incremental_and_removal(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        p1 = write_text(os.path.join(vault, "a.md"), "alpha bravo charlie")
        ing, store = self._ingestor([vault])

        s1 = ing.scan_whitelist(reason="t1")
        self.assertEqual(s1["ingested"], 1)
        self.assertEqual(len(store.docs), 1)

        s2 = ing.scan_whitelist(reason="t2")
        self.assertEqual(s2["skipped"], 1)  # unchanged file
        self.assertEqual(len(store.docs), 1)

        write_text(p1, "alpha bravo charlie delta")  # modify
        s3 = ing.scan_whitelist(reason="t3")
        self.assertEqual(s3["ingested"], 1)

        os.remove(p1)
        s4 = ing.scan_whitelist(reason="t4")
        self.assertEqual(s4["removed"], 1)
        self.assertEqual(len(store.docs), 0)

    def test_single_bad_file_does_not_stop_scan(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "bad.docx"), "definitely not a zip")
        write_text(os.path.join(vault, "good.md"), "still here marker text")
        ing, store = self._ingestor([vault])
        summary = ing.scan_whitelist(reason="mixed")
        self.assertEqual(summary["ingested"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertTrue(any("good.md" in d.get("path", "")
                            for d in store.docs.values()))

    def test_kinds_end_to_end(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(os.path.join(vault, "d"))
        make_pdf(os.path.join(vault, "doc.pdf"), ["pdf text here"])
        make_docx(os.path.join(vault, "doc.docx"), ["docx para"])
        make_chat_export(os.path.join(vault, "chat.json"),
                         [{"sender": "a", "text": "hello chat"}])
        write_text(os.path.join(vault, "d", "code.py"), "def f(): return 1")
        ing, store = self._ingestor([vault])
        ing.scan_whitelist(reason="kinds")
        self.assertEqual(store.stats()["docs"], 4)
        self.assertEqual(store.stats()["docs_by_status"].get("ok"), 4)

    def test_image_with_ocr_off_is_recorded_not_fatal(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        make_png(os.path.join(vault, "pic.png"))
        ing, store = self._ingestor([vault])
        ing.scan_whitelist(reason="img")
        self.assertEqual(store.stats()["docs"], 1)
        status = next(iter(store.docs.values()))["status"]
        self.assertEqual(status, "ocr_unavailable")

    def test_retry_failed_docs_and_drops(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        p = write_text(os.path.join(vault, "x.md"), "retry me text")
        ing, store = self._ingestor([vault])
        ing.scan_whitelist(reason="init")
        doc_id = doc_id_for(p)
        store.set_doc_status(doc_id, "parse_error", "forced")
        res = ing.retry_failed_docs()
        self.assertEqual(res["fixed"], 1)
        # confirmed deletion drops the entry
        os.remove(p)
        store.set_doc_status(doc_id, "parse_error")
        res = ing.retry_failed_docs()
        self.assertEqual(res["dropped"], 1)

    def test_confirmed_gone_semantics(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        p = write_text(os.path.join(vault, "x.md"), "t")
        self.assertFalse(confirmed_gone(p))
        os.remove(p)
        self.assertTrue(confirmed_gone(p))  # parent exists -> confirmed
        gone_root = os.path.join(self.td, "no-such-mount", "f.txt")
        self.assertFalse(confirmed_gone(gone_root))  # parent gone -> unknown

    def test_progress_snapshot(self):
        prog = IngestProgress()
        prog.note_error("/x", "boom")
        snap = prog.snapshot()
        self.assertEqual(snap["errors"], 1)
        self.assertTrue(any("boom" in e for e in snap["recent_errors"]))


# ------------------------------------------------------------------ ocr

class TestOcrEngine(TempCase):
    def test_off_mode(self):
        eng = OcrEngine(mode="off")
        self.assertIsNone(eng.available())
        with self.assertRaises(OcrUnavailable):
            eng.recognize("x.png")

    def test_tesseract_preferred_and_language_mapping_used(self):
        eng = OcrEngine(mode="auto", languages=["zh-Hans", "en-US"], timeout_s=5)
        calls = []

        class Proc:
            returncode = 0
            stdout = b"recognized text"
            stderr = b""

        def fake_run(argv, **kw):
            calls.append(argv)
            return Proc()

        with mock.patch.object(eng, "_tesseract_bin", return_value="/usr/bin/tesseract"), \
                mock.patch("localgate.ocr.subprocess.run", side_effect=fake_run):
            text, engine = eng.recognize("/tmp/img.png")
        self.assertEqual(engine, "tesseract")
        self.assertEqual(text, "recognized text")
        self.assertIn("chi_sim+eng", calls[0])

    def test_tesseract_language_load_failure_falls_back(self):
        eng = OcrEngine(mode="auto", languages=["zh-Hans"], timeout_s=5)
        calls = []

        class Proc:
            def __init__(self, rc, err=b""):
                self.returncode = rc
                self.stdout = b"fallback text"
                self.stderr = err

        def fake_run(argv, **kw):
            calls.append(argv)
            if "-l" in argv:
                return Proc(1, b"Failed loading language 'chi_sim'")
            return Proc(0)

        with mock.patch.object(eng, "_tesseract_bin", return_value="/usr/bin/tesseract"), \
                mock.patch("localgate.ocr.subprocess.run", side_effect=fake_run):
            text, engine = eng.recognize("/tmp/img.png")
        self.assertEqual(text, "fallback text")

    def test_tesseract_failure_raises_ocr_error(self):
        eng = OcrEngine(mode="auto", languages=["en-US"], timeout_s=5)

        class Proc:
            returncode = 1
            stdout = b""
            stderr = b"some error detail"

        with mock.patch.object(eng, "_tesseract_bin", return_value="/usr/bin/tesseract"), \
                mock.patch("localgate.ocr.subprocess.run", return_value=Proc()):
            with self.assertRaises(OcrError):
                eng.recognize("/tmp/img.png")

    def test_no_engine_available(self):
        eng = OcrEngine(mode="auto")
        eng._vision_broken = True
        with mock.patch.object(eng, "_tesseract_bin", return_value=None):
            with self.assertRaises(OcrUnavailable):
                eng.recognize("/tmp/img.png")

    def test_timeout_maps_to_ocr_error(self):
        eng = OcrEngine(mode="auto", languages=["en-US"], timeout_s=5)
        with mock.patch.object(eng, "_tesseract_bin", return_value="/usr/bin/tesseract"), \
                mock.patch("localgate.ocr.subprocess.run",
                           side_effect=subprocess.TimeoutExpired(cmd="t", timeout=5)):
            with self.assertRaises(OcrError):
                eng.recognize("/tmp/img.png")


# ------------------------------------------------- ollama embedder (stub)

class _OllamaStub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode())
        if self.path == "/api/embeddings":
            if body.get("prompt") == "explode":
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            vec = [0.1] * 8
            data = json.dumps({"embedding": vec}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class TestOllamaEmbedder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaStub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_embed_and_health(self):
        emb = OllamaEmbedder(self.url, "test-model", timeout_s=5)
        vecs = emb.embed(["hello", "world"])
        self.assertEqual(len(vecs), 2)
        self.assertEqual(len(vecs[0]), 8)
        self.assertEqual(emb.dim, 8)
        ok, _ms, detail = emb.health()
        self.assertTrue(ok, detail)

    def test_embed_error_on_bad_status(self):
        emb = OllamaEmbedder(self.url, "test-model", timeout_s=5)
        with self.assertRaises(EmbedError):
            emb.embed(["explode"])

    def test_embed_error_on_missing_field(self):
        emb = OllamaEmbedder(self.url, "m", timeout_s=5)
        with mock.patch.object(emb, "_post", return_value={"nope": 1}):
            with self.assertRaises(EmbedError):
                emb.embed(["x"])

    def test_make_embedder_selects_backend(self):
        cfg = make_cfg(tempfile.mkdtemp())
        self.assertIsInstance(make_embedder(cfg), LocalHashEmbedder)
        cfg["embedding"]["backend"] = "ollama"
        self.assertIsInstance(make_embedder(cfg), OllamaEmbedder)

    def test_unreachable_health_reports_false(self):
        emb = OllamaEmbedder("http://127.0.0.1:1", "m", timeout_s=2)
        ok, _ms, detail = emb.health()
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)


# ------------------------------------------------------------------ cli

class TestCliPaths(TempCase):
    def test_status_unreachable_gateway(self):
        from localgate.cli import main
        cfg_path = os.path.join(self.td, "c.yaml")
        from tests.helpers import write_config
        cfg = make_cfg(self.td)
        write_config(cfg_path, cfg)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["status", "--config", cfg_path])
        self.assertEqual(rc, 1)
        self.assertIn("not reachable", err.getvalue())

    def test_index_on_real_whitelist(self):
        from localgate.cli import main
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "v.md"), "cli index content marker")
        from tests.helpers import write_config
        cfg = make_cfg(self.td, paths={"whitelist": [vault]})
        cfg_path = os.path.join(self.td, "c.yaml")
        write_config(cfg_path, cfg)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["index", "--config", cfg_path])
        self.assertEqual(rc, 0)
        self.assertIn('"ingested": 1', out.getvalue())

    def test_python_m_localgate_version(self):
        proc = subprocess.run(
            [sys.executable, "-m", "localgate", "--version"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("localgate", proc.stdout)

    def test_main_module_via_runpy(self):
        with mock.patch.object(sys, "argv", ["localgate", "--version"]):
            with self.assertRaises(SystemExit) as cm:
                runpy.run_module("localgate", run_name="__main__")
        self.assertEqual(cm.exception.code, 0)


# --------------------------------------------------- extract fallback paths

class TestExtractFallbacks(TempCase):
    def test_minimal_pdf_extractor_directly(self):
        from localgate.extract import _extract_pdf_minimal
        pdf = make_pdf(os.path.join(self.td, "m.pdf"), ["fallback path text"])
        self.assertIn("fallback path text", _extract_pdf_minimal(pdf))

    def test_pdf_unescape_sequences(self):
        from localgate.extract import _pdf_unescape
        self.assertEqual(_pdf_unescape(rb"(a\nb\\\(x\)y)"), "a\nb\\(x)y")
        self.assertEqual(_pdf_unescape(rb"(A\101)"), "AA")

    def test_minimal_pdf_used_when_pypdf_missing(self):
        pdf = make_pdf(os.path.join(self.td, "f.pdf"), ["no pypdf here text"])
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            text = extract_pdf(pdf)
        self.assertIn("no pypdf here text", text)

    def test_docx_missing_document_xml(self):
        import zipfile
        p = os.path.join(self.td, "wrong.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("other.xml", "<x/>")
        with self.assertRaises(ExtractError):
            extract_docx(p)

    def test_chat_json_malformed(self):
        p = os.path.join(self.td, "bad.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(ExtractError):
            extract_chat_json(p)

    def test_classify_and_unknown_kind(self):
        self.assertEqual(classify("a.PNG"), "image")
        self.assertEqual(classify("a.PDF"), "pdf")
        self.assertEqual(classify("b.docx"), "docx")
        self.assertEqual(classify("c.json"), "chat_json")
        self.assertEqual(classify("d.py"), "code")
        self.assertEqual(classify("e.md"), "text")
        self.assertEqual(classify("f.xyz"), "unknown")
        with self.assertRaises(ExtractError):
            extract("whatever", kind="unknown")

    def test_image_kind_rejected_by_text_extract(self):
        with self.assertRaises(ExtractError):
            extract("img.png", kind="image")

    def test_read_file_bytes_cap(self):
        p = write_text(os.path.join(self.td, "big.txt"), "x" * 10000)
        with self.assertRaises(ExtractError):
            read_file_bytes(p, max_bytes=100)
        self.assertEqual(len(read_file_bytes(p, max_bytes=20000)), 10000)

    def test_decode_bytes_replacement_fallback(self):
        from localgate.extract import decode_bytes
        # invalid utf-8 AND invalid gb18030 -> replacement char, never a raise
        self.assertIn("\ufffd", decode_bytes(b"caf\xe9"))
        self.assertEqual(decode_bytes("中文".encode("gb18030")), "中文")

    def test_chat_export_numeric_and_missing_fields(self):
        from localgate.extract import _msg_field
        self.assertEqual(_msg_field({"ts": 5}, ("ts",)), "5")
        self.assertEqual(_msg_field({}, ("ts",)), "")


if __name__ == "__main__":
    unittest.main()
