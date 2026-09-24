"""MCP stdio protocol tests: in-process protocol conformance plus an official
MCP Python SDK client round-trip (skipped when the `mcp` package is absent)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest

from localgate.mcp import LATEST_PROTOCOL_VERSION, PROTOCOL_VERSIONS, McpStdioServer

ROOT = __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__)))


class FakeApiClient:
    """Stands in for the HTTP gateway."""

    def __init__(self, reachable: bool = True):
        self.reachable = reachable
        self.base_url = "http://127.0.0.1:8770"

    def search(self, params):
        if not self.reachable:
            raise __import__("urllib.error", fromlist=["URLError"]).URLError("conn refused")
        return 200, {"query": params.get("query"), "results": [
            {"score": 0.9, "path": "/vault/a.md", "snippet": "alpha beta"}],
            "degraded": False}

    def status(self):
        if not self.reachable:
            raise __import__("urllib.error", fromlist=["URLError"]).URLError("conn refused")
        return 200, {"index": {"docs": 1, "chunks": 2, "vectors": 2,
                               "index_size_bytes": 10},
                     "selfcheck": {"rounds": 1, "last_status": "ok"},
                     "config": {"paths": {"whitelist": ["/vault"]}}}

    def document(self, doc_id):
        if not self.reachable:
            raise __import__("urllib.error", fromlist=["URLError"]).URLError("conn refused")
        return 200, {"doc": {"path": "/vault/a.md", "kind": "text",
                             "status": "ok"},
                     "chunks": [{"ordinal": 0, "text": "alpha"}]}


def make_server() -> McpStdioServer:
    return McpStdioServer(FakeApiClient())


class ProtocolNegotiationCase(unittest.TestCase):
    def test_supported_versions_echoed(self):
        s = make_server()
        for v in PROTOCOL_VERSIONS:
            resp = s._handle_call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": v}})
            self.assertEqual(resp["result"]["protocolVersion"], v, v)

    def test_unknown_version_gets_latest(self):
        s = make_server()
        resp = s._handle_call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(resp["result"]["protocolVersion"], LATEST_PROTOCOL_VERSION)

    def test_missing_and_bad_params(self):
        s = make_server()
        resp = s._handle_call({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(resp["result"]["protocolVersion"], LATEST_PROTOCOL_VERSION)
        resp = s._handle_call({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                               "params": ["not", "an", "object"]})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_server_info(self):
        resp = make_server()._handle_call(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "localgate")
        self.assertIn("version", resp["result"]["serverInfo"])
        self.assertIn("tools", resp["result"]["capabilities"])


class ToolCallCase(unittest.TestCase):
    def test_tools_listed(self):
        resp = make_server()._handle_call(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {"localgate_search", "localgate_status",
                                 "localgate_get_document"})

    def test_search_ok(self):
        resp = make_server()._handle_call(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "localgate_search",
                        "arguments": {"query": "alpha"}}})
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("alpha", resp["result"]["content"][0]["text"])

    def test_search_bad_arguments_is_tool_error(self):
        s = make_server()
        for args in ({}, {"query": ""}, {"query": "   "}):
            resp = s._handle_call(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "localgate_search", "arguments": args}})
            self.assertTrue(resp["result"]["isError"], args)

    def test_non_object_arguments_is_invalid_params(self):
        resp = make_server()._handle_call(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "localgate_search", "arguments": ["nope"]}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_missing_name_is_invalid_params(self):
        resp = make_server()._handle_call(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_tool_is_tool_error(self):
        resp = make_server()._handle_call(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "no_such_tool", "arguments": {}}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("unknown tool", resp["result"]["content"][0]["text"])

    def test_status_and_document(self):
        s = make_server()
        resp = s._handle_call({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "localgate_status"}})
        self.assertIn("LocalGate index", resp["result"]["content"][0]["text"])
        resp = s._handle_call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "localgate_get_document",
                                          "arguments": {"doc_id": "abc"}}})
        self.assertIn("Document abc", resp["result"]["content"][0]["text"])

    def test_offline_gateway_is_tool_error_not_crash(self):
        s = McpStdioServer(FakeApiClient(reachable=False))
        resp = s._handle_call({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "localgate_search",
                                          "arguments": {"query": "x"}}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("not reachable", resp["result"]["content"][0]["text"])

    def test_notifications_and_unknown(self):
        s = make_server()
        self.assertIsNone(s._handle_call(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(s._handle_call(
            {"jsonrpc": "2.0", "method": "notifications/cancelled",
             "params": {"requestId": 1}}))
        resp = s._handle_call({"jsonrpc": "2.0", "id": 9, "method": "no/such"})
        self.assertEqual(resp["error"]["code"], -32601)
        resp = s._handle_call("not an object")
        self.assertEqual(resp["error"]["code"], -32600)


class StdioLoopCase(unittest.TestCase):
    def _run(self, lines: list[str]) -> list[dict]:
        s = make_server()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s.serve_forever(lines)
        return [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]

    def test_batch_requests(self):
        batch = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ])
        resps = self._run([batch])
        self.assertEqual([r["id"] for r in resps], [1, 2])

    def test_parse_error(self):
        resps = self._run(["this is not json"])
        self.assertEqual(resps[0]["error"]["code"], -32700)

    def test_mixed_valid_invalid_batch(self):
        batch = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "localgate_search", "arguments": {"query": "q"}}},
        ])
        resps = self._run([batch])
        self.assertEqual(len(resps), 2)
        self.assertNotIn("error", resps[0])


class RunServerGuardCase(unittest.TestCase):
    def test_non_loopback_api_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            from localgate.mcp import run_mcp_server
            rc = run_mcp_server(api_base="http://cloud.example.com:8770")
        self.assertEqual(rc, 2)
        self.assertIn("non-loopback", err.getvalue())


class OfficialSdkClientCase(unittest.TestCase):
    """Round-trip with the official MCP Python SDK client over stdio."""

    def setUp(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("official `mcp` python sdk not installed")
        # hold a listener so no other test can grab this port between runs:
        # the tool call then fails deterministically (no valid HTTP response)
        # which is exactly the offline behavior under test
        import socket
        self._holder = socket.socket()
        self._holder.bind(("127.0.0.1", 0))
        self._holder.listen(1)
        self.addCleanup(self._holder.close)
        self.dead_port = self._holder.getsockname()[1]

    def test_initialize_list_tools_against_real_subprocess(self):
        import asyncio

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def _drive():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "localgate.cli", "mcp",
                      "--api", f"http://127.0.0.1:{self.dead_port}"],
                cwd=ROOT,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    self.assertEqual(init.server_info.name, "localgate")
                    self.assertIn(str(init.protocol_version), PROTOCOL_VERSIONS)
                    tools = await session.list_tools()
                    self.assertEqual({t.name for t in tools.tools},
                                     {"localgate_search", "localgate_status",
                                      "localgate_get_document"})
                    # gateway is down on that port: a call must yield a clean
                    # tool error rather than a protocol failure
                    res = await session.call_tool("localgate_search",
                                                  {"query": "anything"})
                    self.assertTrue(res.is_error)

        asyncio.run(asyncio.wait_for(_drive(), timeout=60))


if __name__ == "__main__":
    unittest.main()
