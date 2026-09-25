"""Standard unittest suite for LocalGate core modules.

Run:  python -m pytest tests/test_units.py -q
      python -m unittest discover -s tests
All tests use temporary directories and free loopback ports.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

from localgate.chunker import chunk_text
from localgate.config import ConfigError, _mini_yaml_parse, load_config
from localgate.embedding import LocalHashEmbedder
from localgate.extract import (
    MAX_DOCX_ENTRIES,
    ExtractError,
    extract_docx,
    extract_pdf,
    format_chat_export,
    looks_like_chat_export,
)
from localgate.fsutil import is_under, iter_files, matches_exclude
from localgate.httpclient import validate_loopback_http_url
from localgate.jsonllog import JsonlLogger
from localgate.ocr import tesseract_langs
from localgate.store import IndexStore, tokenize
from tests.helpers import free_port, make_cfg, write_config
from tests.make_samples import make_chat_export, make_docx, make_pdf, write_text


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name


# ------------------------------------------------------------------ config

class TestConfig(TempCase):
    def test_defaults_are_privacy_safe(self):
        cfg = load_config(os.path.join(self.td, "missing.yaml"))
        self.assertEqual(cfg["paths"]["whitelist"], [])
        self.assertEqual(cfg["paths"]["blacklist"], [])
        self.assertEqual(cfg["server"]["host"], "127.0.0.1")
        self.assertEqual(cfg["embedding"]["ollama_url"], "http://127.0.0.1:11434")

    def test_overrides_and_expansion(self):
        p = os.path.join(self.td, "config.yaml")
        write_config(p, {
            "server": {"port": 9999},
            "paths": {"whitelist": ["~/Notes", "/tmp/Vault"],
                      "blacklist": ["/tmp/Vault/private"]},
            "embedding": {"backend": "ollama"},
        })
        cfg = load_config(p)
        self.assertEqual(cfg["server"]["port"], 9999)
        self.assertTrue(cfg["paths"]["whitelist"][0].startswith(os.path.expanduser("~")))
        self.assertEqual(cfg["watcher"]["interval_s"], 10)  # default kept
        self.assertEqual(cfg["embedding"]["backend"], "ollama")

    def test_non_loopback_host_rejected(self):
        p = os.path.join(self.td, "bad.yaml")
        write_config(p, {"server": {"host": "0.0.0.0"}})
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_remote_ollama_url_rejected(self):
        p = os.path.join(self.td, "remote.yaml")
        write_config(p, {"embedding": {"ollama_url": "http://192.168.1.5:11434"}})
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_https_and_malformed_ollama_url_rejected(self):
        for url in ("https://127.0.0.1:11434", "http://user@127.0.0.1",
                    "http://127.0.0.1/evil", "nonsense"):
            p = os.path.join(self.td, "bad-url.yaml")
            write_config(p, {"embedding": {"ollama_url": url}})
            with self.assertRaises(ConfigError, msg=url):
                load_config(p)

    def test_localhost_ollama_url_normalized_to_loopback_ip(self):
        p = os.path.join(self.td, "localhost.yaml")
        write_config(p, {"embedding": {"ollama_url": "http://localhost:11434"}})
        cfg = load_config(p)
        self.assertEqual(cfg["embedding"]["ollama_url"], "http://127.0.0.1:11434")

    def test_non_int_port_rejected(self):
        p = os.path.join(self.td, "bad2.yaml")
        write_config(p, {"server": {"port": "notanumber"}})
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_relative_paths_resolve_against_config_dir_even_from_other_cwd(self):
        cfg_dir = tempfile.mkdtemp(prefix="lgcfg-")
        self.addCleanup(lambda: __import__("shutil").rmtree(cfg_dir, True))
        p = os.path.join(cfg_dir, "config.yaml")
        write_config(p, {"index": {"data_dir": "./data"}, "logs": {"dir": "./logs"},
                         "paths": {"whitelist": ["./vault", "~/Notes"]}})
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.mkdtemp(prefix="lgcwd-"))  # an unrelated CWD
            cfg = load_config(p)
        finally:
            os.chdir(cwd)
        expected_root = os.path.normpath(cfg_dir)  # no symlink resolution by design
        self.assertEqual(cfg["index"]["data_dir"], os.path.join(expected_root, "data"))
        self.assertEqual(cfg["logs"]["dir"], os.path.join(expected_root, "logs"))
        self.assertEqual(cfg["paths"]["whitelist"][0],
                         os.path.join(expected_root, "vault"))
        self.assertTrue(cfg["paths"]["whitelist"][1].startswith(os.path.expanduser("~")))

    def test_mini_yaml_fallback(self):
        parsed = _mini_yaml_parse("""
# comment
server:
  port: 1234
  host: "127.0.0.1"
paths:
  whitelist:
    - /a/b
    - /c/d
  flag: true
  ratio: 0.5
""")
        self.assertEqual(parsed["server"]["port"], 1234)
        self.assertEqual(parsed["server"]["host"], "127.0.0.1")
        self.assertEqual(parsed["paths"]["whitelist"], ["/a/b", "/c/d"])
        self.assertIs(parsed["paths"]["flag"], True)
        self.assertEqual(parsed["paths"]["ratio"], 0.5)

    def test_public_config_strips_internals(self):
        p = os.path.join(self.td, "c.yaml")
        write_config(p, {})
        from localgate.config import effective_public_config
        pub = effective_public_config(load_config(p))
        self.assertNotIn("_config_source", pub)
        self.assertNotIn("_base_dir", pub["paths"])


# ------------------------------------------------------------------ net guard

class TestLoopbackUrlGuard(unittest.TestCase):
    def test_loopback_forms_accepted_and_normalized(self):
        self.assertEqual(validate_loopback_http_url("http://127.0.0.1:11434"),
                         "http://127.0.0.1:11434")
        self.assertEqual(validate_loopback_http_url("http://localhost:11434/"),
                         "http://127.0.0.1:11434")
        self.assertEqual(validate_loopback_http_url("http://[::1]:8770"),
                         "http://[::1]:8770")

    def test_remote_ipv4_ipv6_and_dns_rejected(self):
        for url in ("http://8.8.8.8:11434", "http://192.168.1.5",
                    "http://[2001:db8::1]:11434", "http://0.0.0.0",
                    "http://example.com"):
            with self.assertRaises(ValueError, msg=url):
                validate_loopback_http_url(url)

    def test_malformed_and_mixed_urls_rejected(self):
        for url in ("https://127.0.0.1", "ftp://127.0.0.1", "http://user:pass@127.0.0.1",
                    "http://127.0.0.1/#f", "http://127.0.0.1/?q=1",
                    "http://127.0.0.1/path", "http://127.0.0.1:99999",
                    "http://127.0.0.1:0", "http://127.0.0.1\\@evil.com",
                    "http://evil.com\\@127.0.0.1", "not a url", "", "   ",
                    "http://127.0.0.1 .evil.com", "http://metasploit.local"):
            with self.assertRaises(ValueError, msg=url):
                validate_loopback_http_url(url)

    def test_non_string_rejected(self):
        with self.assertRaises(ValueError):
            validate_loopback_http_url(None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            validate_loopback_http_url(1234)  # type: ignore[arg-type]

    def test_dns_fail_closed(self):
        with mock.patch("localgate.httpclient.socket.getaddrinfo",
                        side_effect=socket.gaierror(8, "nodename nor servname")):
            with self.assertRaises(ValueError):
                validate_loopback_http_url("http://somehost:11434")


# ------------------------------------------------------------------ fsutil

class TestFsutil(TempCase):
    def test_is_under(self):
        a = os.path.join(self.td, "a")
        b = os.path.join(a, "b")
        os.makedirs(b)
        self.assertTrue(is_under(os.path.join(b, "f.txt"), a))
        self.assertTrue(is_under(a, a))
        self.assertFalse(is_under(os.path.join(self.td, "x", "f"), a))
        self.assertFalse(is_under(a + "2", a))  # prefix trap

    def test_excludes(self):
        self.assertTrue(matches_exclude(".DS_Store", [".DS_Store", "*.tmp"]))
        self.assertTrue(matches_exclude("foo.tmp", ["*.tmp"]))

    def test_policy_walk(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(os.path.join(vault, "secret"))
        os.makedirs(os.path.join(vault, "sub"))
        for rel in ("keep.md", "sub/keep2.txt", "secret/hidden.md", "skip.xyz"):
            write_text(os.path.join(vault, rel), "x")
        got = sorted(iter_files([vault], [os.path.join(vault, "secret")], [],
                                {".md", ".txt"}, 10, protected_dirs=[]))
        names = [os.path.relpath(g, vault) for g in got]
        self.assertEqual(names, [os.path.normpath("keep.md"),
                                 os.path.normpath("sub/keep2.txt")])

    def test_protected_dirs_never_indexed(self):
        vault = os.path.join(self.td, "vault")
        inner_data = os.path.join(vault, "service-data")
        os.makedirs(inner_data)
        write_text(os.path.join(inner_data, "docs.jsonl"), "{}")
        write_text(os.path.join(vault, "note.md"), "x")
        got = list(iter_files([vault], [], [], {".md", ".jsonl"}, 10,
                              protected_dirs=[inner_data]))
        self.assertEqual([os.path.basename(g) for g in got], ["note.md"])

    def test_whitelist_root_missing_reports_error(self):
        errors: list[str] = []
        got = list(iter_files([os.path.join(self.td, "nope")], [], [],
                              {".md"}, 10, protected_dirs=[],
                              on_error=lambda p, m: errors.append(m)))
        self.assertEqual(got, [])
        self.assertTrue(any("does not exist" in m for m in errors))

    def test_file_over_max_file_mb_skipped(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        big = os.path.join(vault, "big.md")
        with open(big, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024 + 1))
        errors: list[str] = []
        got = list(iter_files([vault], [], [], {".md"}, 2, protected_dirs=[],
                              on_error=lambda p, m: errors.append(m)))
        self.assertEqual(got, [])
        self.assertTrue(any("larger than max_file_mb" in m for m in errors))


# ------------------------------------------------------------------ chunker

class TestChunker(unittest.TestCase):
    def test_short_and_empty(self):
        self.assertEqual(chunk_text("hello world", 500, 80), ["hello world"])
        self.assertEqual(chunk_text("", 500, 80), [])
        self.assertEqual(chunk_text("   ", 500, 80), [])

    def test_multi_chunk_with_overlap(self):
        long = ("段落一。" * 50) + "\n\n" + ("Paragraph two words here. " * 50)
        chunks = chunk_text(long, 200, 30)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 400 for c in chunks))
        joined = "".join(chunks)
        self.assertGreaterEqual(len(joined), len(long) * 0.85)

    def test_cjk(self):
        zh = "深度学习是机器学习的一个分支。" * 40
        zc = chunk_text(zh, 100, 20)
        self.assertGreaterEqual(len(zc), 10)
        self.assertTrue(all(c.strip() for c in zc))

    def test_no_progress_path_terminates(self):
        chunks = chunk_text("a" * 1000, 100, 200)  # overlap >= size would be invalid config
        self.assertTrue(all(isinstance(c, str) for c in chunks))


# ------------------------------------------------------------------ embedding

class TestEmbedding(unittest.TestCase):
    def test_deterministic_normalized_discriminative(self):
        e = LocalHashEmbedder(dim=256)
        v1 = e.embed_one("机器学习 深度学习")
        v2 = e.embed_one("机器学习 深度学习")
        v3 = e.embed_one("cooking recipe pasta")
        self.assertEqual(v1, v2)
        self.assertEqual(len(v1), 256)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in v1)), 1.0, places=6)
        cos = sum(a * b for a, b in zip(v1, v3, strict=False))
        self.assertLess(cos, 0.2)

    def test_health(self):
        ok, ms, detail = LocalHashEmbedder(64).health()
        self.assertTrue(ok)
        self.assertGreaterEqual(ms, 0)
        self.assertEqual(detail, "ok")

    def test_unicode_and_very_long_text(self):
        e = LocalHashEmbedder(dim=128)
        v = e.embed_one("🚀 emoji 混合 text " * 5000)
        self.assertEqual(len(v), 128)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0, places=6)


# ------------------------------------------------------------------ store

class TestStore(TempCase):
    def _populate(self, st: IndexStore):
        st.upsert_doc("d1", {"path": os.path.join(self.td, "a.md"), "kind": "text",
                             "ext": ".md", "status": "ok"},
                      ["machine learning platform Phoenix", "nightly dataset runs"],
                      [[1.0, 0.0], [0.0, 1.0]])
        st.upsert_doc("d2", {"path": os.path.join(self.td, "b.md"), "kind": "text",
                             "ext": ".md", "status": "ok"},
                      ["cooking pasta tonight"], [[0.7, 0.7]])

    def test_bm25_vector_and_persistence(self):
        data_dir = os.path.join(self.td, "data")
        st = IndexStore(data_dir)
        self._populate(st)
        st.save_all()

        self.assertTrue(st.search_fulltext("machine learning")[0][0].startswith("d1"))
        self.assertTrue(st.search_fulltext("pasta")[0][0].startswith("d2"))
        self.assertEqual(st.search_vector([0.9, 0.5])[0][0], "d2#c0")

        st2 = IndexStore(data_dir)
        self.assertEqual(len(st2.docs), 2)
        self.assertEqual(len(st2.chunks), 3)
        self.assertEqual(len(st2.vectors), 3)
        self.assertTrue(st2.search_fulltext("phoenix")[0][0].startswith("d1"))

        st2.remove_doc("d1")
        self.assertEqual((len(st2.docs), len(st2.chunks), len(st2.vectors)), (1, 1, 1))
        self.assertFalse(st2.search_fulltext("phoenix"))
        st2.save_all()
        self.assertEqual(len(IndexStore(data_dir).docs), 1)

    def test_empty_and_malformed_queries(self):
        st = IndexStore(os.path.join(self.td, "data"))
        self._populate(st)
        self.assertEqual(st.search_fulltext(""), [])
        self.assertEqual(st.search_fulltext("。。。"), [])
        self.assertEqual(st.search_vector([1.0]), [])  # dim mismatch -> skipped
        long_q = "词" * 50000
        self.assertIsInstance(st.search_fulltext(long_q), list)

    def test_corrupt_line_tolerated_and_reported(self):
        st = IndexStore(os.path.join(self.td, "data"))
        self._populate(st)
        st.save_all()
        with open(st.docs_path, "a", encoding="utf-8") as f:
            f.write('{"doc_id": "corrupt", broken json\n')
        st4 = IndexStore(os.path.join(self.td, "data"))
        self.assertEqual(len(st4.docs), 2)
        self.assertEqual(len(st4.load_errors), 1)

    def test_truncated_last_line_tolerated(self):
        st = IndexStore(os.path.join(self.td, "data"))
        self._populate(st)
        st.save_all()
        with open(st.docs_path, encoding="utf-8") as f:
            content = f.read()
        # simulate a crash mid-write: cut the last line in half
        lines = content.splitlines(keepends=True)
        truncated = "".join(lines[:-1]) + lines[-1][:len(lines[-1]) // 2]
        with open(st.docs_path, "w", encoding="utf-8") as f:
            f.write(truncated)
        st2 = IndexStore(os.path.join(self.td, "data"))
        self.assertLessEqual(len(st2.docs), 2)
        self.assertTrue(st2.load_errors)

    def test_partial_write_recovery_prunes_stale_postings(self):
        """bm25 persisted but chunks.jsonl write lost (crash between saves)."""
        data_dir = os.path.join(self.td, "data")
        st = IndexStore(data_dir)
        self._populate(st)
        st.save_all()
        with open(st.chunks_path, "w", encoding="utf-8") as f:
            f.write('{"chunk_id": "d2#c0", "doc_id": "d2", "ordinal": 0, '
                    '"text": "cooking pasta tonight"}\n')
        st2 = IndexStore(data_dir)
        self.assertEqual(len(st2.docs), 2)
        self.assertEqual(len(st2.chunks), 1)
        # postings referencing the pruned chunks must be gone, with a note
        self.assertFalse(st2.search_fulltext("phoenix"))
        self.assertTrue(st2.search_fulltext("pasta"))
        self.assertTrue(any("recovered partial write" in e for e in st2.load_errors))
        self.assertEqual(st2.total_len,
                         sum(st2.chunk_len.values()))
        self.assertEqual(set(st2.vectors), {"d2#c0"})

    def test_stale_confirmed_vs_unreachable(self):
        st = IndexStore(os.path.join(self.td, "data"))
        gone_parent = os.path.join(self.td, "unmounted", "vol")
        write_text(os.path.join(self.td, "gone.md"), "x")
        st.upsert_doc("gone", {"path": os.path.join(self.td, "gone.md"),
                               "status": "ok"}, ["t"], None)
        os.remove(os.path.join(self.td, "gone.md"))
        st.upsert_doc("unreach", {"path": os.path.join(gone_parent, "x.md"),
                                  "status": "ok"}, ["t"], None)
        confirmed, unreachable = st.stale_docs_missing_files()
        self.assertIn("gone", confirmed)
        self.assertIn("unreach", unreachable)

    def test_broken_doc_detection_and_merge(self):
        st = IndexStore(os.path.join(self.td, "data"))
        self._populate(st)
        write_text(os.path.join(self.td, "a.md"), "x")
        write_text(os.path.join(self.td, "b.md"), "x")
        confirmed, _ = st.stale_docs_missing_files()
        self.assertEqual(confirmed, [])
        st.vectors.pop("d2#c0")
        self.assertEqual(st.broken_docs(), ["d2"])
        st.vectors["d2#c0"] = [0.0, 1.0]
        merged = st.merge_small_segments(min_entries=9999)
        self.assertGreaterEqual(merged, 0)
        self.assertEqual(len(st.vectors), 3)

    def test_tokenizer(self):
        self.assertEqual(tokenize("Hello World_2"), ["hello", "world_2"])
        self.assertEqual(tokenize("机器学习"), ["机器", "器学", "学习"])
        self.assertEqual(tokenize("好"), ["好"])

    @unittest.skipIf(os.name == "nt",
                     "POSIX dir permissions are not enforced on Windows")
    def test_read_only_dir_save_fails_without_corrupting_memory(self):
        st = IndexStore(os.path.join(self.td, "data"))
        self._populate(st)
        st.save_all()
        for d in (st.docs_dir, st.vec_dir):
            os.chmod(d, 0o555)
        try:
            st.upsert_doc("d3", {"path": "x", "status": "ok"}, ["t"], None)
            with self.assertRaises(OSError):
                st.save_all()
        finally:
            for d in (st.docs_dir, st.vec_dir):
                os.chmod(d, 0o755)
        # in-memory state still searchable
        self.assertTrue(st.search_fulltext("pasta"))

    def test_get_document_shape(self):
        st = IndexStore(os.path.join(self.td, "data"))
        self._populate(st)
        doc = st.get_document("d1")
        self.assertEqual(doc["doc"]["doc_id"], "d1")
        self.assertEqual([c["ordinal"] for c in doc["chunks"]], [0, 1])
        self.assertIsNone(st.get_document("nope"))


# ------------------------------------------------------------------ extract

class TestExtract(TempCase):
    def test_pdf(self):
        pdf = make_pdf(os.path.join(self.td, "t.pdf"),
                       ["Hello PDF world.", "Second page text."])
        text = extract_pdf(pdf)
        self.assertIn("Hello PDF world.", text)
        self.assertIn("Second page text.", text)

    def test_docx(self):
        docx = make_docx(os.path.join(self.td, "t.docx"),
                         ["First para", "Second & <para>"])
        self.assertEqual(extract_docx(docx), "First para\nSecond & <para>")

    def test_chat_export(self):
        chat = make_chat_export(os.path.join(self.td, "c.json"),
                                [{"sender": "alice", "time": "t1", "text": "hi there"},
                                 {"sender": "bob", "time": "t2", "text": "hello"}])
        with open(chat, encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(looks_like_chat_export(data))
        self.assertIn("[t1] alice: hi there", format_chat_export(data))
        with open(os.path.join(self.td, "n.json"), "w", encoding="utf-8") as f:
            json.dump({"key": "value"}, f)
        with open(os.path.join(self.td, "n.json"), encoding="utf-8") as f:
            self.assertFalse(looks_like_chat_export(json.load(f)))

    def test_corrupt_docx_raises(self):
        bad = os.path.join(self.td, "bad.docx")
        with open(bad, "wb") as f:
            f.write(b"this is not a zip file at all")
        with self.assertRaises(ExtractError):
            extract_docx(bad)

    def test_docx_zip_bomb_guards(self):
        import zipfile
        # (1) entry-count guard
        many = os.path.join(self.td, "many.docx")
        with zipfile.ZipFile(many, "w", zipfile.ZIP_DEFLATED) as z:
            for i in range(MAX_DOCX_ENTRIES + 10):
                z.writestr(f"part{i}", "x")
        with self.assertRaises(ExtractError):
            extract_docx(many)
        # (2) decompressed-size guard: 70MB of nulls compresses to ~70KB
        bomb = os.path.join(self.td, "bomb.docx")
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", "\x00" * (70 * 1024 * 1024))
        with self.assertRaises(ExtractError):
            extract_docx(bomb)

    def test_text_extraction_truncation_recorded(self):
        from localgate import extract as em
        p = write_text(os.path.join(self.td, "big.md"), "a" * 5000)
        with mock.patch.object(em, "MAX_EXTRACTED_CHARS", 1000):
            text, kind, extra = em.extract(p)
        self.assertEqual(len(text), 1000)
        self.assertTrue(extra.get("truncated"))
        self.assertEqual(kind, "text")

    def test_gbk_text_decoded(self):
        p = os.path.join(self.td, "gbk.md")
        with open(p, "wb") as f:
            f.write("中文内容测试".encode("gb18030"))
        from localgate.extract import extract_text_like
        self.assertIn("中文内容测试", extract_text_like(p))

    def test_empty_text_file(self):
        from localgate.extract import extract
        p = write_text(os.path.join(self.td, "empty.md"), "")
        text, kind, extra = extract(p)
        self.assertEqual(text, "")
        self.assertEqual(kind, "text")
        self.assertNotIn("truncated", extra)


# ------------------------------------------------------------------ ocr

class TestOcrLanguageMapping(unittest.TestCase):
    def test_common_mappings(self):
        self.assertEqual(tesseract_langs(["zh-Hans", "en-US"]), "chi_sim+eng")
        self.assertEqual(tesseract_langs(["zh-TW"]), "chi_tra")
        self.assertEqual(tesseract_langs(["ja", "ko"]), "jpn+kor")
        self.assertEqual(tesseract_langs(["en-GB"]), "eng")
        self.assertEqual(tesseract_langs(["pt-BR"]), "por")

    def test_unknown_falls_back_to_eng(self):
        self.assertEqual(tesseract_langs(["xx-Unknown"]), "eng")
        self.assertEqual(tesseract_langs([]), "eng")
        self.assertEqual(tesseract_langs(None), "eng")
        self.assertEqual(tesseract_langs(["", "  "]), "eng")

    def test_mixed_known_unknown(self):
        self.assertEqual(tesseract_langs(["zh-Hans", "xx-Yy", "fr-FR"]), "chi_sim+fra")


# ------------------------------------------------------------------ logger

class TestJsonlLogger(TempCase):
    def test_write_read_rotate(self):
        lg = JsonlLogger(self.td, "test-log", max_bytes=64 * 1024, backups=3)
        for i in range(1000):
            lg.write({"i": i, "payload": "x" * 200})
        entries = lg.read_recent(5)
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[-1]["i"], 999)
        rotated = [n for n in os.listdir(self.td) if n.endswith(".log.1")]
        self.assertGreaterEqual(len(rotated), 1)
        with open(os.path.join(self.td, rotated[0]), encoding="utf-8") as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        self.assertTrue(lines)

    def test_read_recent_empty_dir(self):
        lg = JsonlLogger(self.td, "none", max_bytes=64 * 1024)
        self.assertEqual(lg.read_recent(10), [])


# ------------------------------------------------------------------ search

class TestSearchDegradation(TempCase):
    def test_embedder_failure_degrades_to_fulltext(self):
        from localgate.search import HybridSearcher

        class Boom:
            def embed(self, _):
                raise RuntimeError("down")

        st = IndexStore(os.path.join(self.td, "data"))
        st.upsert_doc("d1", {"path": "p", "kind": "text", "ext": ".md",
                             "status": "ok"}, ["alpha beta gamma"], [[1.0, 0.0]])
        cfg = make_cfg(self.td)
        res = HybridSearcher(st, Boom(), cfg).search("alpha")
        self.assertTrue(res["degraded"])
        self.assertTrue(any("vector" in n for n in res["notes"]))
        self.assertTrue(res["results"])

    def test_top_k_clamped(self):
        from localgate.search import HybridSearcher
        st = IndexStore(os.path.join(self.td, "data"))
        st.upsert_doc("d1", {"path": "p", "kind": "text", "ext": ".md",
                             "status": "ok"}, ["alpha beta"], [[1.0, 0.0]])
        cfg = make_cfg(self.td)
        res = HybridSearcher(st, LocalHashEmbedder(2), cfg).search("alpha", top_k=10 ** 9)
        self.assertEqual(res["top_k"], 100)


# ------------------------------------------------------------------ selfcheck

class TestSelfcheckGuardrails(TempCase):
    def test_backoff_and_recovery(self):
        from localgate.ingest import IngestProgress
        from localgate.ocr import OcrEngine
        from localgate.selfcheck import SelfCheckDaemon

        st = IndexStore(os.path.join(self.td, "data"))
        prog = IngestProgress()

        class _FakeWatcher:
            error_count = 0

        class _FakeIngestor:
            progress = prog
            store = st

            def retry_failed_docs(self, only_doc_ids=None):
                return {"retried": 0, "fixed": 0, "still_broken": 0, "dropped": 0}

        cfg = make_cfg(self.td)
        cfg["selfcheck"].update({"interval_s": 10, "consecutive_error_threshold": 2,
                                 "backoff_max_s": 40, "item_delay_ms": 0})
        d = SelfCheckDaemon(cfg, st, _FakeIngestor(), LocalHashEmbedder(64),
                            OcrEngine(mode="off"), _FakeWatcher(),
                            f"http://127.0.0.1:{free_port()}",
                            JsonlLogger(self.td, "svc"), JsonlLogger(self.td, "sc"))
        d._http_ping = lambda: (False, 1, "unreachable: forced")
        e1 = d.run_round()
        self.assertEqual(e1["status"], "error")
        self.assertEqual(d.current_interval, 10)
        e2 = d.run_round()
        self.assertEqual(e2["status"], "error")
        self.assertGreater(d.current_interval, 10)
        self.assertIn("backed off", e2["suggestions"])

        d._http_ping = lambda: (True, 1, "ok")
        e3 = d.run_round()
        self.assertEqual(e3["status"], "ok")
        self.assertEqual(d.current_interval, 10)

        entries = d.log.read_recent(10)
        self.assertEqual(len(entries), 3)
        required = {"timestamp", "check_round", "status", "check_items", "metrics",
                    "optimization_actions", "errors", "suggestions"}
        self.assertTrue(all(required <= set(e) for e in entries))
        self.assertEqual(len(e3["check_items"]), 7)

    def test_round_covers_repair_warning_and_error_paths(self):
        from localgate.ingest import IngestProgress
        from localgate.ocr import OcrEngine
        from localgate.selfcheck import SelfCheckDaemon

        st = IndexStore(os.path.join(self.td, "data"))
        prog = IngestProgress()

        class _FakeWatcher:
            error_count = 2

        class _FakeIngestor:
            progress = prog
            store = st

            def retry_failed_docs(self, only_doc_ids=None):
                return {"retried": 1, "fixed": 1, "still_broken": 0, "dropped": 0}

        cfg = make_cfg(self.td)
        cfg["selfcheck"].update({"interval_s": 5, "item_delay_ms": 0,
                                 "consecutive_error_threshold": 50,
                                 "index_size_warn_mb": 1})
        d = SelfCheckDaemon(cfg, st, _FakeIngestor(), LocalHashEmbedder(64),
                            OcrEngine(mode="off"), _FakeWatcher(),
                            f"http://127.0.0.1:{free_port()}",
                            JsonlLogger(self.td, "svc2"), JsonlLogger(self.td, "sc2"))

        # broken doc -> repair path; oversized index -> warning
        write_text(os.path.join(self.td, "a.md"), "x")
        st.upsert_doc("d1", {"path": os.path.join(self.td, "a.md"),
                             "status": "ok", "chunk_count": 1}, ["t"], [[0.0]])
        st.vectors.pop("d1#c0")
        # a doc whose file is confirmed deleted (parent dir exists) so that
        # file_sources will attempt a removal
        gone = os.path.join(self.td, "gone.md")
        write_text(gone, "x")
        st.upsert_doc("dg", {"path": gone, "status": "ok", "chunk_count": 1},
                      ["t"], None)
        os.remove(gone)

        # file_sources error branch: make confirmed-stale removal fail
        d._http_ping = lambda: (False, 1, "unreachable: forced")
        with mock.patch.object(st, "remove_doc", side_effect=OSError("disk gone")):
            e = d.run_round()
        statuses = {i["item"]: i["status"] for i in e["check_items"]}
        self.assertEqual(statuses["file_sources"], "error")
        self.assertEqual(e["status"], "error")
        self.assertTrue(any("file_sources" in err for err in e["errors"]))

        # healthy pass with repair + size warning
        d._http_ping = lambda: (True, 1, "ok")
        with mock.patch.object(st, "index_size_bytes",
                               return_value=5 * 1024 * 1024):  # > warn_mb of 1
            e2 = d.run_round()
        statuses2 = {i["item"]: i["status"] for i in e2["check_items"]}
        self.assertEqual(statuses2["index_integrity"], "warning")
        self.assertEqual(statuses2["resource_thresholds"], "warning")
        self.assertTrue(any("reindexed broken doc" in a for a in e2["optimization_actions"]))
        self.assertEqual(e2["status"], "warning")
        self.assertIn("attention needed", e2["suggestions"])
        self.assertIn("embedding_latency_ms", e2["metrics"])
        self.assertIn("http_selfping_ms", e2["metrics"])

        # optimization error branch
        with mock.patch.object(st, "orphan_cleanup", side_effect=RuntimeError("no")):
            e3 = d.run_round()
        statuses3 = {i["item"]: i["status"] for i in e3["check_items"]}
        self.assertEqual(statuses3["optimization"], "error")
        self.assertEqual(e3["status"], "error")


# ------------------------------------------------------------------ upstream

class TestUpstreamReadGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/json":
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://example.invalid/x")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/drip":
                    self.send_response(200)
                    self.send_header("Content-Length", "20")
                    self.end_headers()
                    try:
                        for _ in range(20):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.3)
                    except OSError:
                        pass
                elif self.path == "/big":
                    self.send_response(200)
                    self.send_header("Content-Length", str(1024 * 1024))
                    self.end_headers()
                    self.wfile.write(b"x" * (1024 * 1024))

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.srv = srv
        cls.base = f"http://127.0.0.1:{srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_json_and_caps(self):
        import urllib.request

        from localgate.httpclient import read_upstream_json, read_upstream_text, urlopen_noproxy
        req = urllib.request.Request(self.base + "/json", method="GET")
        with urlopen_noproxy(req, timeout=5) as resp:
            self.assertEqual(read_upstream_json(resp, timeout_s=5, max_bytes=1024),
                             {"ok": True})

        with urlopen_noproxy(urllib.request.Request(self.base + "/drip"),
                             timeout=10) as resp:
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                read_upstream_text(resp, timeout_s=1, max_bytes=1024)
            self.assertLess(time.monotonic() - t0, 3.0)

        with urlopen_noproxy(urllib.request.Request(self.base + "/big"),
                             timeout=10) as resp:
            with self.assertRaises(ValueError):
                read_upstream_text(resp, timeout_s=10, max_bytes=4096)

    def test_redirects_blocked(self):
        """A 3xx from a local service must not be followed (exfil guard)."""
        import urllib.error
        import urllib.request

        from localgate.httpclient import urlopen_noproxy
        with self.assertRaises(urllib.error.HTTPError):
            urlopen_noproxy(urllib.request.Request(self.base + "/redirect"),
                            timeout=5)


# ------------------------------------------------------------------ cli

class TestCli(unittest.TestCase):
    def test_version(self):
        from localgate.cli import main
        with self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_mcp_rejects_remote_gateway(self):
        from localgate.cli import main
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["mcp", "--api", "http://10.1.2.3:8770"])
        self.assertEqual(rc, 2)
        self.assertIn("non-loopback", err.getvalue())

    def test_index_empty_whitelist_fails(self):
        from localgate.cli import main
        with tempfile.TemporaryDirectory() as td:
            cfg_path = write_config(os.path.join(td, "c.yaml"), make_cfg(td))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main(["index", "--config", cfg_path])
            self.assertEqual(rc, 1)
            self.assertIn("whitelist is empty", err.getvalue())


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------- coverage round-out

class TestFsutilBranches(TempCase):
    @unittest.skipIf(os.name == "nt", "symlink privileges vary on Windows")
    def test_hidden_dirs_and_symlink_escape_skipped(self):
        vault = os.path.join(self.td, "vault")
        outside = os.path.join(self.td, "outside")
        os.makedirs(os.path.join(vault, ".hidden"))
        os.makedirs(os.path.join(vault, "node_modules"))
        os.makedirs(outside)
        write_text(os.path.join(vault, ".hidden", "h.md"), "hidden")
        write_text(os.path.join(vault, "node_modules", "n.md"), "dep")
        write_text(os.path.join(vault, "visible.md"), "visible")
        os.symlink(outside, os.path.join(vault, "linkdir"))
        write_text(os.path.join(outside, "leak.md"), "outside leak")
        got = list(iter_files([vault], [], ["node_modules"], {".md"}, 10,
                              protected_dirs=[]))
        names = [os.path.basename(g) for g in got]
        self.assertEqual(names, ["visible.md"], names)

    @unittest.skipIf(os.name == "nt", "symlink privileges vary on Windows")
    def test_symlink_within_whitelist_followed(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "real.md"), "real file")
        os.symlink(os.path.join(vault, "real.md"), os.path.join(vault, "alias.md"))
        got = list(iter_files([vault], [], [], {".md"}, 10, protected_dirs=[]))
        # symlinked files are followed to their real path inside the whitelist
        self.assertEqual({os.path.basename(g) for g in got}, {"real.md"})

    def test_duplicate_whitelist_roots_walked_once(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "one.md"), "x")
        got = list(iter_files([vault, vault], [], [], {".md"}, 10,
                              protected_dirs=[]))
        self.assertEqual(len(got), 1)

    def test_blacklist_file_exact(self):
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "a.md"), "x")
        write_text(os.path.join(vault, "b.md"), "y")
        got = list(iter_files([vault], [os.path.join(vault, "a.md")], [],
                              {".md"}, 10, protected_dirs=[]))
        self.assertEqual([os.path.basename(g) for g in got], ["b.md"])


class TestConfigBranches(TempCase):
    def test_json_config_supported(self):
        p = os.path.join(self.td, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"server": {"port": 9001}}, f)
        self.assertEqual(load_config(p)["server"]["port"], 9001)

    def test_invalid_json_rejected(self):
        p = os.path.join(self.td, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken")
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_invalid_yaml_rejected(self):
        p = os.path.join(self.td, "config.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("a: [unclosed")
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_scalar_type_errors(self):
        cases = (
            ({"embedding": {"backend": "bogus"}}, "backend"),
            ({"embedding": {"dim": "x"}}, "dim"),
            ({"embedding": {"timeout_s": -1}}, "timeout"),
            ({"ocr": {"mode": "maybe"}}, "ocr mode"),
            ({"index": {"chunk_overlap": 600}}, "overlap"),
            ({"server": {"port": 70000}}, "port range"),
            ({"search": {"top_k": 0}}, "top_k"),
            ({"search": {"vector_weight": 2}}, "weight"),
            ({"watcher": {"interval_s": -3}}, "interval"),
            ({"selfcheck": {"backoff_max_s": 0}}, "backoff"),
            ({"logs": {"max_mb": 0}}, "max_mb"),
        )
        for override, why in cases:
            p = os.path.join(self.td, f"c{why}.yaml")
            write_config(p, override)
            with self.assertRaises(ConfigError, msg=why):
                load_config(p)

    def test_whitelist_single_string_accepted(self):
        p = os.path.join(self.td, "c.yaml")
        write_config(p, {"paths": {"whitelist": "~/Solo"}})
        cfg = load_config(p)
        self.assertEqual(cfg["paths"]["whitelist"],
                         [os.path.normpath(os.path.expanduser("~/Solo"))])


class TestMcpHelpers(unittest.TestCase):
    def test_format_search_variants(self):
        from localgate.mcp import _format_search
        self.assertIn("No local results", _format_search({"query": "q",
                                                          "results": []}))
        text = _format_search({"query": "q", "degraded": True, "results": [
            {"score": 0.5, "path": "/p", "snippet": "multi\nline"}]})
        self.assertIn("degraded", text)
        self.assertIn("[1]", text)
        self.assertNotIn("\nline", text.split("[1]")[1].split("\n    ")[1])

    def test_run_mcp_uses_config_port(self):
        # a config with a custom loopback port is honored; empty stdin -> rc 0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cfg_path = write_config(os.path.join(td, "c.yaml"),
                                    make_cfg(td, port=8770))
            from localgate.mcp import run_mcp_server
            rc = run_mcp_server(config_path=cfg_path, lines=[])
        self.assertEqual(rc, 0)

    def test_api_client_json_bodies(self):
        import http.server
        import threading

        from localgate.mcp import ApiClient

        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _respond(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                seen["body"] = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"])))
                self._respond({"ok": True})

            def do_GET(self):
                seen["path"] = self.path
                self._respond({"ok": True})

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        client = ApiClient(f"http://127.0.0.1:{srv.server_address[1]}")
        status, body = client.search({"query": "q", "top_k": 3})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(seen["body"], {"query": "q", "top_k": 3})
        client.status()
        self.assertEqual(seen["path"], "/api/status")
        client.document("abc")
        self.assertEqual(seen["path"], "/api/document/abc")


class TestServiceSnapshot(TempCase):
    def test_start_stop_full_lifecycle(self):
        from localgate.service import LocalGateService
        vault = os.path.join(self.td, "vault")
        os.makedirs(vault)
        write_text(os.path.join(vault, "s.md"), "lifecycle content")
        cfg = make_cfg(self.td, paths={"whitelist": [vault]},
                       watcher={"enabled": True, "interval_s": 1},
                       selfcheck={"enabled": True, "interval_s": 1,
                                  "item_delay_ms": 0})
        svc = LocalGateService(cfg)
        svc.install_signal_handlers()
        svc.start()
        self.addCleanup(svc.stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not svc.store.docs:
            time.sleep(0.2)
        self.assertTrue(svc.store.docs)
        snap = svc.status_snapshot()
        for key in ("service", "version", "uptime_s", "config", "index",
                    "ingest", "watcher", "selfcheck", "ocr", "embedding"):
            self.assertIn(key, snap)
        self.assertEqual(snap["service"], "localgate")

    def test_wait_forever_returns_after_stop(self):
        import threading as th

        from localgate.service import LocalGateService
        svc = LocalGateService(make_cfg(self.td))
        svc.http.start()
        self.addCleanup(svc.stop)
        th.Timer(0.5, svc.stop).start()
        svc.wait_forever()  # must return, not hang


class TestSelfcheckErrorBranches(TempCase):
    def test_all_items_failing_still_writes_log_and_backs_off(self):
        import threading as th

        from localgate.ingest import IngestProgress
        from localgate.ocr import OcrEngine
        from localgate.selfcheck import SelfCheckDaemon

        st = IndexStore(os.path.join(self.td, "data"))
        prog = IngestProgress()

        class _FakeWatcher:
            error_count = 0

        class _FakeIngestor:
            progress = prog
            store = st

            def retry_failed_docs(self, only_doc_ids=None):
                raise RuntimeError("repair exploded")

        class _BoomEmbedder:
            def health(self):
                raise RuntimeError("embedder exploded")

        # a broken doc (chunk_count set, no vector) forces the integrity item
        # to call retry_failed_docs, which explodes -> error branch. Its file
        # must exist so the file_sources item does not drop it first.
        write_text(os.path.join(self.td, "b.md"), "x")
        st.upsert_doc("broken", {"path": os.path.join(self.td, "b.md"),
                                 "status": "ok", "chunk_count": 1}, ["t"], None)
        st.docs["broken"]["chunk_count"] = 2  # count mismatch -> broken_docs

        cfg = make_cfg(self.td)
        cfg["selfcheck"].update({"interval_s": 2, "item_delay_ms": 1,
                                 "consecutive_error_threshold": 1,
                                 "backoff_max_s": 8})
        d = SelfCheckDaemon(cfg, st, _FakeIngestor(), _BoomEmbedder(),
                            OcrEngine(mode="off"), _FakeWatcher(),
                            f"http://127.0.0.1:{free_port()}",
                            JsonlLogger(self.td, "svc3"), JsonlLogger(self.td, "sc3"))
        d._http_ping = lambda: (_ for _ in ()).throw(RuntimeError("ping exploded"))
        e = d.run_round()
        self.assertEqual(e["status"], "error")
        self.assertEqual(len(e["check_items"]), 7)
        self.assertGreater(d.current_interval, 2)  # backoff after threshold
        items = {i["item"]: i["status"] for i in e["check_items"]}
        self.assertEqual(items["index_integrity"], "error")
        self.assertEqual(items["embedding_health"], "error")
        self.assertEqual(items["services_alive"], "error")
        self.assertEqual(items["optimization"], "error")

        # run loop crash guard: a raising round must not kill the thread
        started = th.Event()

        def exploding_round():
            started.set()
            raise RuntimeError("round exploded")

        d.run_round = exploding_round  # type: ignore[method-assign]
        t = th.Thread(target=d.run, daemon=True)
        t.start()
        started.wait(5)
        time.sleep(0.3)
        d.request_stop()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        entries = JsonlLogger(self.td, "svc3").read_recent(10)
        self.assertTrue(any(e.get("event") == "selfcheck_crashed" for e in entries),
                        entries)

    def test_stats_shape(self):
        from localgate.ingest import IngestProgress
        from localgate.ocr import OcrEngine
        from localgate.selfcheck import SelfCheckDaemon

        class _W:
            error_count = 0

        class _I:
            progress = IngestProgress()
            store = IndexStore(os.path.join(self.td, "data"))

            def retry_failed_docs(self, only_doc_ids=None):
                return {"retried": 0, "fixed": 0, "still_broken": 0, "dropped": 0}

        cfg = make_cfg(self.td)
        d = SelfCheckDaemon(cfg, _I.store, _I(), LocalHashEmbedder(32),
                            OcrEngine(mode="off"), _W(),
                            f"http://127.0.0.1:{free_port()}",
                            JsonlLogger(self.td, "s"), JsonlLogger(self.td, "c"))
        stats = d.stats()
        self.assertFalse(stats["alive"])
        self.assertEqual(stats["rounds"], 0)
        self.assertIsNone(stats["last_status"])


class TestJsonlLogEdges(TempCase):
    def test_read_recent_tolerates_corrupt_lines_and_missing_dir(self):
        lg = JsonlLogger(self.td, "edge", max_bytes=64 * 1024)
        lg.write({"ok": 1})
        with open(lg._file_for_today(), "a", encoding="utf-8") as f:
            f.write("this is not json\n")
        entries = lg.read_recent(10)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ok"], 1)
        missing = JsonlLogger(os.path.join(self.td, "nope"), "x")
        self.assertEqual(missing.read_recent(5), [])

    def test_oversized_rotation_removes_old_backups(self):
        lg = JsonlLogger(self.td, "rot", max_bytes=64 * 1024, backups=2)
        for i in range(1500):
            lg.write({"i": i, "payload": "y" * 300})
        names = os.listdir(self.td)
        backups = [n for n in names if ".log." in n]
        self.assertTrue(backups)
        self.assertLessEqual(len(backups), lg.backups)


class TestMiniYamlEdges(TempCase):
    def test_scalar_coercions(self):
        parsed = _mini_yaml_parse('''
a: "quoted # not comment"
b: 'single'
c:
d: ~
e: null
f: [1, 2, "three, four"]
g: ""
''')
        self.assertEqual(parsed["a"], "quoted # not comment")
        self.assertEqual(parsed["b"], "single")
        self.assertIsNone(parsed["c"])
        self.assertIsNone(parsed["d"])
        self.assertIsNone(parsed["e"])
        self.assertEqual(parsed["f"], [1, 2, "three, four"])
        self.assertEqual(parsed["g"], "")

    def test_inline_list_of_scalars(self):
        parsed = _mini_yaml_parse("list: [ true, no, 3.5 ]")
        self.assertEqual(parsed["list"], [True, False, 3.5])

    def test_empty_document_yields_empty_mapping(self):
        self.assertEqual(_mini_yaml_parse(""), {})
        self.assertEqual(_mini_yaml_parse("# only a comment\n"), {})

    def test_bad_line_without_colon(self):
        with self.assertRaises(ConfigError):
            _mini_yaml_parse("just some words")

    def test_unexpected_indent(self):
        with self.assertRaises(ConfigError):
            _mini_yaml_parse("a:\n    b: 1\n      c: 2")

    def test_root_not_mapping(self):
        with self.assertRaises(ConfigError):
            _mini_yaml_parse("- just\n- a list")

    def test_none_data_via_pyyaml(self):
        import types
        p = os.path.join(self.td, "empty.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("")  # parses to None under yaml.safe_load
        fake_yaml = types.SimpleNamespace(safe_load=lambda _t: None)
        import localgate.config as cfg_mod
        with mock.patch.dict(sys.modules, {"yaml": fake_yaml}):
            data, _src = cfg_mod._load_raw(p)
        self.assertEqual(data, {})

    def test_pyyaml_non_mapping_root(self):
        import types
        p = os.path.join(self.td, "list.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("- a\n- b\n")
        fake_yaml = types.SimpleNamespace(safe_load=lambda _t: ["a", "b"])
        import localgate.config as cfg_mod
        with mock.patch.dict(sys.modules, {"yaml": fake_yaml}):
            with self.assertRaises(ConfigError):
                cfg_mod._load_raw(p)

    def test_mini_yaml_fallback_when_pyyaml_missing(self):
        p = os.path.join(self.td, "mini.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("server:\n  port: 8123\n")
        import localgate.config as cfg_mod
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") \
            else __builtins__["__import__"]

        def no_yaml(name, *a, **kw):
            if name == "yaml":
                raise ImportError("no yaml")
            return real_import(name, *a, **kw)

        with mock.patch("builtins.__import__", side_effect=no_yaml):
            data, _src = cfg_mod._load_raw(p)
        self.assertEqual(data["server"]["port"], 8123)

    def test_config_root_scalar_values_type_errors(self):
        p = os.path.join(self.td, "t.yaml")
        write_config(p, {"server": {"read_timeout_s": "fast"}})
        with self.assertRaises(ConfigError):
            load_config(p)
        p2 = os.path.join(self.td, "t2.yaml")
        write_config(p2, {"search": {"fulltext_weight": "heavy"}})
        with self.assertRaises(ConfigError):
            load_config(p2)
        p3 = os.path.join(self.td, "t3.yaml")
        write_config(p3, {"paths": {"exclude_names": [1, 2]}})
        with self.assertRaises(ConfigError):
            load_config(p3)
