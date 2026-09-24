"""HTTP local attack surface tests: Host/Origin/Content-Type gates, input
caps, generic error responses, stalled bodies and IPv6 binding.

The service runs IN-PROCESS on free loopback ports with empty whitelist and
disabled background threads."""

from __future__ import annotations

import json
import socket
import time
import unittest

from tests.helpers import build_service, free_port_v6, json_request, make_cfg, raw_http


class HttpSecurityCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.svc = build_service(self._td.name)
        self.svc.http.start()
        self.addCleanup(self.svc.stop)
        self.port = self.svc.http.port

    # ------------------------------------------------------------- host gate

    def test_valid_loopback_hosts(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}",
                     f"[::1]:{self.port}"):
            code, body = json_request(self.port, "GET", "/health",
                                      headers={"Host": host})
            self.assertEqual(code, 200, host)
            self.assertEqual(body["status"], "ok")

    def test_missing_host_rejected(self):
        code, data = raw_http(self.port,
                              b"GET /health HTTP/1.1\r\nConnection: close\r\n\r\n")
        self.assertEqual(code, 400)

    def test_dns_rebinding_host_rejected(self):
        """attacker page resolving a public name to 127.0.0.1 sends its own
        Host header - it must not be served."""
        for host in ("evil.example.com", "127.0.0.1.evil.example.com",
                     "0177.0.0.1.evil.example.com"):
            code, body = json_request(self.port, "GET", "/api/status",
                                      headers={"Host": host})
            self.assertEqual(code, 403, host)

    # ----------------------------------------------------------- origin gate

    def test_foreign_origin_rejected(self):
        code, _ = json_request(self.port, "POST", "/api/search",
                               body={"query": "x"},
                               headers={"Origin": "http://evil.example.com"})
        self.assertEqual(code, 403)
        code, _ = json_request(self.port, "GET", "/api/status",
                               headers={"Origin": "https://localhost:8443"})
        self.assertEqual(code, 403)
        code, _ = json_request(self.port, "GET", "/api/status",
                               headers={"Origin": "null"})
        self.assertEqual(code, 403)

    def test_loopback_origin_accepted(self):
        code, _ = json_request(self.port, "GET", "/api/status",
                               headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(code, 200)

    # -------------------------------------------------------- content-type

    def test_post_requires_json_content_type(self):
        port = self.port
        req = (f"POST /api/search HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: text/plain\r\nContent-Length: 13\r\n"
               f"Connection: close\r\n\r\n").encode() + b'{"query":"x"}'
        code, _ = raw_http(port, req)
        self.assertEqual(code, 415)

    def test_post_with_charset_ok(self):
        code, body = json_request(
            self.port, "POST", "/api/search", body={"query": "x"},
            headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(code, 200)

    # --------------------------------------------------------------- caps

    def test_query_length_cap(self):
        code, body = json_request(self.port, "POST", "/api/search",
                                  body={"query": "a" * 9000})
        self.assertEqual(code, 400)
        self.assertIn("8192", body["error"])

    def test_body_size_cap(self):
        port = self.port
        payload = json.dumps({"query": "a" * (300 * 1024)}).encode()
        req = (f"POST /api/search HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
               f"Connection: close\r\n\r\n").encode() + payload
        code, _ = raw_http(port, req)
        self.assertEqual(code, 400)

    def test_param_validation(self):
        for body, why in (({"query": ""}, "empty"),
                          ({}, "missing"),
                          ({"query": "x", "top_k": 0}, "top_k=0"),
                          ({"query": "x", "top_k": True}, "top_k bool"),
                          ({"query": "x", "top_k": "many"}, "top_k str"),
                          ({"query": "x", "vector_weight": 5}, "weight>1"),
                          ({"query": "x", "fulltext_weight": True}, "weight bool")):
            code, _ = json_request(self.port, "POST", "/api/search", body=body)
            self.assertEqual(code, 400, f"{why}: {body}")

    def test_log_lines_bounds(self):
        for lines in ("0", "-5", "1001", "abc", "999999999999999999999"):
            code, _ = json_request(self.port, "GET",
                                   f"/api/logs/selfcheck?lines={lines}")
            self.assertEqual(code, 400, lines)
        code, _ = json_request(self.port, "GET", "/api/logs/selfcheck?lines=10")
        self.assertEqual(code, 200)

    def test_document_id_shape(self):
        for doc in ("../../etc/passwd", "ZZZZ", "a" * 100):
            code, _ = json_request(self.port, "GET", f"/api/document/{doc}")
            self.assertEqual(code, 404, doc)

    # ------------------------------------------------- internal error leak

    def test_internal_error_is_generic_and_logged_locally(self):
        def boom(*a, **kw):
            raise RuntimeError("SECRET-PATH /home/me/private leaked in traceback")

        orig = self.svc.searcher.search
        self.svc.searcher.search = boom
        try:
            code, body = json_request(self.port, "POST", "/api/search",
                                      body={"query": "x"})
        finally:
            self.svc.searcher.search = orig
        self.assertEqual(code, 500)
        self.assertEqual(body["error"], "internal server error")
        self.assertNotIn("SECRET-PATH", json.dumps(body))
        # the detail went to the local structured log only
        entries = self.svc.service_log.read_recent(5)
        self.assertTrue(any("SECRET-PATH" in json.dumps(e) for e in entries))
        self.assertTrue(any("traceback" in e for e in entries))

    # ------------------------------------------------------- stalled body

    def test_stalled_body_connection_cut(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(s.close)
        s.sendall(b"POST /api/search HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  b"Content-Type: application/json\r\nContent-Length: 500\r\n\r\n"
                  b"brief start")
        s.settimeout(10)
        t0 = time.monotonic()
        closed = False
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    closed = True
                    break
        except TimeoutError:
            pass
        elapsed = time.monotonic() - t0
        self.assertTrue(closed, "server never closed a stalled-body connection")
        self.assertLess(elapsed, 8.0)  # read_timeout_s=3 + slack

    # ---------------------------------------------------------- misc

    def test_rescan_endpoint_reports_real_state(self):
        code, body = json_request(self.port, "POST", "/api/rescan", body={})
        self.assertEqual(code, 200)
        self.assertIn("rescan_started", body)

    def test_unknown_endpoint_and_panel(self):
        code, _ = json_request(self.port, "GET", "/api/nope")
        self.assertEqual(code, 404)
        code, html = json_request(self.port, "GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("LocalGate", html)
        code, data = raw_http(
            self.port,
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Connection: close\r\n\r\n".encode())
        self.assertIn(b"X-Content-Type-Options: nosniff", data)


class Ipv6BindingCase(unittest.TestCase):
    def test_binds_ipv6_loopback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cfg = make_cfg(td, port=free_port_v6())
            cfg["server"]["host"] = "::1"
            from localgate.service import LocalGateService
            svc = LocalGateService(cfg)
            try:
                svc.http.start()
                code, body = json_request(svc.http.port, "GET", "/health",
                                          headers={"Host": "[::1]"}, host="::1")
                self.assertEqual(code, 200)
                self.assertEqual(body["status"], "ok")
            finally:
                svc.stop()


if __name__ == "__main__":
    unittest.main()


class EndpointBranchCase(unittest.TestCase):
    """Rounds out endpoint branch coverage."""

    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.svc = build_service(self._td.name)
        self.svc.http.start()
        self.addCleanup(self.svc.stop)
        self.port = self.svc.http.port

    def test_config_endpoint(self):
        code, body = json_request(self.port, "GET", "/api/config")
        self.assertEqual(code, 200)
        self.assertIn("paths", body)

    def test_service_log_endpoint_default_lines(self):
        code, body = json_request(self.port, "GET", "/api/logs/service")
        self.assertEqual(code, 200)
        self.assertIn("entries", body)

    def test_empty_json_body_is_missing_query(self):
        code, body = json_request(self.port, "POST", "/api/search", body=None,
                                  headers={"Content-Type": "application/json",
                                           "Content-Length": "0"})
        self.assertEqual(code, 400)

    def test_json_array_body_rejected(self):
        port = self.port
        payload = b"[1,2,3]"
        req = (f"POST /api/search HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
               f"Connection: close\r\n\r\n").encode() + payload
        code, _ = raw_http(port, req)
        self.assertEqual(code, 400)

    def test_rescan_rejects_bad_body_and_wrong_type(self):
        code, _ = json_request(self.port, "POST", "/api/rescan", body={"x": 1})
        self.assertEqual(code, 200)
        port = self.port
        payload = b"{broken"
        req = (f"POST /api/rescan HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
               f"Connection: close\r\n\r\n").encode() + payload
        code, _ = raw_http(port, req)
        self.assertEqual(code, 400)

    def test_post_unknown_endpoint_404(self):
        code, _ = json_request(self.port, "POST", "/api/nope", body={})
        self.assertEqual(code, 404)

    def test_document_empty_id_404(self):
        code, _ = json_request(self.port, "GET", "/api/document/")
        self.assertEqual(code, 404)


class DocAndBodyEdgeCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.svc = build_service(self._td.name)
        self.svc.http.start()
        self.addCleanup(self.svc.stop)
        self.port = self.svc.http.port

    def test_document_found_roundtrip(self):
        self.svc.store.upsert_doc(
            "abc123", {"path": "/tmp/x.md", "kind": "text", "ext": ".md",
                       "status": "ok"},
            ["hello world"], None)
        code, body = json_request(self.port, "GET", "/api/document/abc123")
        self.assertEqual(code, 200)
        self.assertEqual(body["doc"]["doc_id"], "abc123")
        self.assertEqual(body["chunks"][0]["text"], "hello world")

    def test_non_utf8_body_400(self):
        port = self.port
        payload = b"\xff\xfe\xfa"
        req = (f"POST /api/search HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
               f"Connection: close\r\n\r\n").encode() + payload
        code, _ = raw_http(port, req)
        self.assertEqual(code, 400)

    def test_double_start_is_noop(self):
        server = self.svc.http._server
        self.svc.http.start()
        # same server object still serving, no second bind attempted
        self.assertIs(self.svc.http._server, server)
        code, _ = json_request(self.port, "GET", "/health")
        self.assertEqual(code, 200)
