"""End-to-end scenario tests: simulate every user scenario against a REAL
running LocalGate service (and a real stdio MCP server session).

Run: python3 tests/run_scenarios.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import make_samples  # noqa: E402

PASS: list[str] = []
FAILED: list[str] = []


def scenario(name):
    def deco(fn):
        fn._scenario_name = name
        return fn
    return deco


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_json(method: str, url: str, body: dict | None = None,
              timeout: float = 15) -> tuple[int, dict | str]:
    # loopback traffic must bypass any proxy env vars
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(payload)
            except ValueError:
                return resp.status, payload
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(payload)
        except ValueError:
            return e.code, payload


def wait_until(fn, timeout: float, interval: float = 0.25, desc: str = "") -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            if fn():
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {desc or fn}: {last_err}")


class Service:
    def __init__(self, name: str, workdir: str, vault: str, port: int, *,
                 whitelist: list[str] | None = None, blacklist: list[str] | None = None,
                 embedding: dict | None = None, watcher: dict | None = None,
                 selfcheck: dict | None = None, extra: dict | None = None,
                 read_timeout_s: int = 5):
        self.name = name
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.workdir = workdir
        self.cfg_path = os.path.join(workdir, f"config-{name}.yaml")
        self.log_path = os.path.join(workdir, f"service-{name}.out")
        self.proc: subprocess.Popen | None = None
        whitelist = vault if whitelist is None else whitelist
        cfg = {
            "server": {"host": "127.0.0.1", "port": port,
                       "read_timeout_s": read_timeout_s},
            "index": {"data_dir": os.path.join(workdir, f"data-{name}")},
            "paths": {"whitelist": whitelist, "blacklist": blacklist or []},
            "watcher": watcher if watcher is not None else {"enabled": True, "interval_s": 2},
            "selfcheck": selfcheck if selfcheck is not None else {
                "enabled": True, "interval_s": 3, "item_delay_ms": 5},
            "logs": {"dir": os.path.join(workdir, f"logs-{name}")},
        }
        if embedding:
            cfg["embedding"] = embedding
        if extra:
            cfg.update(extra)
        import yaml
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

    def start(self, timeout: float = 30) -> None:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "main.py"), "serve",
             "--config", self.cfg_path],
            stdout=open(self.log_path, "ab"), stderr=subprocess.STDOUT, env=env,
            cwd=ROOT)
        try:
            wait_until(lambda: http_json("GET", self.base + "/health")[0] == 200,
                       timeout, desc=f"{self.name} /health")
        except AssertionError:
            try:
                with open(self.log_path, encoding="utf-8", errors="replace") as f:
                    print(f"    [{self.name} service log]\n{f.read()[-2000:]}")
            except OSError:
                pass
            raise

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def cleanup(self) -> None:
        self.stop()
        for sub in (f"data-{self.name}", f"logs-{self.name}"):
            shutil.rmtree(os.path.join(self.workdir, sub), ignore_errors=True)


def tree_hashes(root: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


def search(base: str, query: str, **kw) -> dict:
    status, body = http_json("POST", base + "/api/search", {"query": query, **kw})
    check(status == 200, f"search http {status}: {body}")
    return body


# ============================================================ scenarios

@scenario("S1 初始索引：白名单全量入库、黑名单与未知类型排除")
def s1(ctx):
    base = ctx.svc.base
    wait_until(lambda: ctx.status()["index"]["docs"] == 8, 120,
               desc="initial index of 8 docs (vision helper compile included)")
    st = ctx.status()
    kinds = st["index"]["docs_by_kind"]
    for kind in ("text", "code", "pdf", "docx", "chat_json", "image"):
        check(kinds.get(kind, 0) >= 1, f"kind {kind} missing, got {kinds}")
    # blacklisted diary never indexed
    res = search(base, "private diary secret")
    check(all("secret-diary" not in (r["path"] or "") for r in res["results"]),
          "blacklisted file appeared in results")
    # unsupported extension never indexed
    res = search(base, "not a known format")
    check(all("blob.xyz" not in (r["path"] or "") for r in res["results"]),
          "unsupported ext indexed")
    check(st["index"]["docs"] == 8, f"expected exactly 8 docs, got {st['index']['docs']}")


@scenario("S2 混合检索：英文/中文/代码/聊天记录命中正确来源")
def s2(ctx):
    base = ctx.svc.base
    res = search(base, "Phoenix reliability OKR")
    check(res["results"], "no results for Phoenix query")
    tops = " ".join(r["path"] or "" for r in res["results"][:3])
    check("project-phoenix" in tops or "meeting-notes" in tops or "report.docx" in tops,
          f"unexpected top hits: {tops}")
    res = search(base, "深度学习 神经网络")
    check(res["results"], "no results for Chinese query")
    check("读书笔记" in (res["results"][0]["path"] or ""),
          f"Chinese top hit wrong: {res['results'][0]['path']}")
    res = search(base, "process_training_data dataset")
    check(any("pipeline.py" in (r["path"] or "") for r in res["results"]),
          "code search failed")
    res = search(base, "reviewed the Phoenix pull request")
    check(any("team-chat.json" in (r["path"] or "") for r in res["results"][:3]),
          "chat export search failed")
    check(res["results"][0]["snippet"], "snippet missing")


@scenario("S3 API 校验与健壮性：400/404/非法输入/HTML面板")
def s3(ctx):
    base = ctx.svc.base
    status, body = http_json("POST", base + "/api/search", {"query": ""})
    check(status == 400, f"empty query should 400, got {status}")
    status, body = http_json("POST", base + "/api/search", {})
    check(status == 400, f"missing query should 400, got {status}")
    status, _ = http_json("POST", base + "/api/search", {"query": "x", "top_k": -1})
    check(status == 400, "negative top_k should 400")
    status, body = http_json("POST", base + "/api/search", {"query": "x", "top_k": "many"})
    check(status == 400, "string top_k should 400")
    status, body = http_json("POST", base + "/api/search", {"query": "x", "vector_weight": 5})
    check(status == 400, "out-of-range weight should 400")
    # raw malformed json body (through the same no-proxy opener)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + "/api/search", data=b"{not json",
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(req, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    check(status == 400, f"malformed JSON should 400, got {status}")
    # stalled request body: server must cut the connection off instead of
    # pinning a handler thread forever (read_timeout_s=5 in test services)
    host, port = base.split("//")[1].split(":")
    stall = socket.create_connection((host, int(port)), timeout=20)
    stall.sendall(b"POST /api/search HTTP/1.1\r\nHost: x\r\n"
                  b"Content-Type: application/json\r\nContent-Length: 500\r\n\r\n")
    t0 = time.time()
    closed = False
    try:
        while True:
            chunk = stall.recv(4096)
            if not chunk:
                closed = True
                break
    except TimeoutError:
        pass
    elapsed = time.time() - t0
    stall.close()
    check(closed, "server never closed a stalled-body connection")
    check(elapsed < 12, f"stalled body cut off after {elapsed:.1f}s (expected ~5s)")
    status, _ = http_json("GET", base + "/health")
    check(status == 200, "gateway unhealthy after stalled connection")
    status, _ = http_json("GET", base + "/api/nope")
    check(status == 404, "unknown endpoint should 404")
    status, body = http_json("GET", base + "/api/document/deadbeef")
    check(status == 404, "unknown doc should 404")
    status, st = http_json("GET", base + "/api/status")
    check(status == 200 and st["service"] == "localgate" and "index" in st, "status shape")
    status, body = http_json("GET", base + "/")
    check(status == 200 and "<html" in body.lower() and "localgate" in body.lower(),
          "HTML status panel missing")
    status, entries = http_json("GET", base + "/api/logs/selfcheck")
    check(status == 200 and "entries" in entries, "selfcheck log endpoint")


@scenario("S4 增量监听：新增/修改/删除文件自动同步进索引")
def s4(ctx):
    base = ctx.svc.base
    vault = ctx.vault
    new_file = os.path.join(vault, "notes", "fresh-note.md")
    try:
        # add
        make_samples.write_text(new_file, "# Fresh note\n\nUnique quantum telemetry beacon "
                                           "arrived from the observatory today.")
        wait_until(lambda: any("fresh-note" in (r["path"] or "")
                               for r in search(base, "quantum telemetry beacon")["results"]),
                   30, desc="new file indexed")
        # modify
        make_samples.write_text(new_file, "# Fresh note rewritten\n\nZebra unicorns parade "
                                          "through the crystal canyon every morning.")
        wait_until(lambda: any("zebra unicorns" in r["snippet"].lower()
                               for r in search(base, "zebra unicorns parade")["results"]),
                   30, desc="modified file reindexed")
        res = search(base, "quantum telemetry beacon")
        check(all("quantum" not in r["snippet"].lower() for r in res["results"]),
              f"stale content still served after modify: {res['results'][:2]}")
    finally:
        # delete
        if os.path.exists(new_file):
            os.remove(new_file)
    deadline = time.time() + 30
    last_diag = None
    while time.time() < deadline:
        st = ctx.status()
        res = search(base, "zebra unicorns parade")
        last_diag = (st["index"]["docs"],
                     [(r["path"], r["snippet"][:60], r["score"],
                       r["fulltext_score"], r["vector_score"]) for r in res["results"]],
                     st["watcher"]["last_pass"], st["watcher"]["error_count"],
                     st["watcher"]["alive"])
        stale = [r for r in res["results"]
                 if r["path"] and "fresh-note" in r["path"]]
        if not stale:
            break
        time.sleep(1)
    else:
        raise AssertionError(f"deleted file still served 30s: {last_diag}")


@scenario("S5 自检循环：JSONL 结构完整、七项检测齐全、动作记录")
def s5(ctx):
    def rounds():
        _status, body = http_json("GET", ctx.svc.base + "/api/logs/selfcheck?lines=10")
        return body["entries"]
    wait_until(lambda: len(rounds()) >= 2, 30, desc="two self-check rounds")
    required = {"timestamp", "check_round", "status", "check_items", "metrics",
                "optimization_actions", "errors", "suggestions"}
    for e in rounds()[:2]:
        missing = required - set(e.keys())
        check(not missing, f"log entry missing fields: {missing}")
        check(len(e["check_items"]) == 7,
              f"expected 7 check items, got {len(e['check_items'])}")
        names = [i["item"] for i in e["check_items"]]
        check(names == ["file_sources", "index_integrity", "performance",
                        "embedding_health", "services_alive", "resource_thresholds",
                        "optimization"], f"item order/names wrong: {names}")
        check(e["status"] in ("ok", "warning", "error"), "bad status value")
        check("index_size_bytes" in e["metrics"], "metrics missing index size")
    check(all(e["status"] == "ok" for e in rounds()[:2]),
          f"expected healthy rounds, got {[e['status'] for e in rounds()[:2]]}")


@scenario("S6 索引损坏自修复：重启后自检定位并单文件重建")
def s6(ctx):
    svc = ctx.svc
    # stop, corrupt, restart
    svc.stop()
    docs_path = os.path.join(svc.workdir, f"data-{svc.name}", "index", "docs.jsonl")
    with open(docs_path, encoding="utf-8") as f:
        lines = f.readlines()
    check(len(lines) >= 8, f"docs.jsonl too small: {len(lines)}")
    # corrupt one doc record (broken JSON) and one vector
    lines[2] = '{"doc_id": "oops", broken\n'
    with open(docs_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    vec_dir = os.path.join(svc.workdir, f"data-{svc.name}", "index", "vectors")
    seg = sorted(os.listdir(vec_dir))[0]
    seg_path = os.path.join(vec_dir, seg)
    with open(seg_path, encoding="utf-8") as f:
        seg_lines = f.readlines()
    seg_lines[0] = '{"chunk_id": "bad", "vector": [not a number\n'
    with open(seg_path, "w", encoding="utf-8") as f:
        f.writelines(seg_lines)
    svc.start()
    # self-check should repair: docs reload with load_errors, broken docs reindexed
    def repaired():
        st = ctx.status()
        return (st["index"]["docs"] == 8 and st["selfcheck"]["rounds"] >= 1
                and st["index"]["docs_by_status"].get("ok", 0) >= 7)
    wait_until(repaired, 40, desc="self-check repaired corrupted index")
    _status, entries = http_json("GET", svc.base + "/api/logs/selfcheck?lines=5")
    flat = json.dumps(entries, ensure_ascii=False)
    check("load_errors" in flat, "integrity check did not surface load errors")
    res = search(svc.base, "quantum")  # arbitrary; ensure search still works
    check(isinstance(res.get("results"), list), "search broken after repair")


@scenario("S7 空白名单安全：不索引任何内容并明确告警")
def s7(ctx):
    svc = ctx.svc_empty
    st = ctx.status(svc)
    check(st["index"]["docs"] == 0, f"empty whitelist indexed {st['index']['docs']} docs")
    svclog = os.path.join(svc.workdir, f"service-{svc.name}.out")
    with open(svclog, encoding="utf-8") as f:
        out = f.read()
    check("WARNING" in out and "whitelist is empty" in out,
          "empty-whitelist warning not printed")


@scenario("S8 MCP stdio 协议：握手/工具列表/调用/错误处理")
def s8(ctx):
    env = dict(os.environ)
    env["LOCALGATE_API"] = ctx.svc.base
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py"), "mcp",
         "--config", ctx.svc.cfg_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, cwd=ROOT)

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        check(line.strip(), "mcp server closed stdout unexpectedly; stderr="
              + proc.stderr.read() if not line.strip() else "empty response")
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "test", "version": "0"}}})
    resp = recv()
    check(resp["id"] == 1 and resp["result"]["protocolVersion"] == "2024-11-05"
          and resp["result"]["serverInfo"]["name"] == "localgate", f"initialize: {resp}")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    resp = recv()
    tools = {t["name"] for t in resp["result"]["tools"]}
    check({"localgate_search", "localgate_status", "localgate_get_document"} <= tools,
          f"tools missing: {tools}")
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "localgate_search",
                     "arguments": {"query": "深度学习", "top_k": 3}}})
    resp = recv()
    text = resp["result"]["content"][0]["text"]
    check("result(s)" in text and not resp["result"].get("isError", False),
          f"mcp search bad: {text[:200]}")
    check("读书笔记" in text, f"mcp search missed Chinese note: {text[:200]}")
    send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
          "params": {"name": "localgate_status", "arguments": {}}})
    resp = recv()
    check("Self-check" in resp["result"]["content"][0]["text"], "mcp status bad")
    send({"jsonrpc": "2.0", "id": 5, "method": "ping"})
    resp = recv()
    check(resp["id"] == 5 and resp["result"] == {}, "ping bad")
    send({"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
    resp = recv()
    check(resp["result"] == {"resources": []}, "resources/list bad")
    send({"jsonrpc": "2.0", "id": 7, "method": "bogus/method"})
    resp = recv()
    check(resp["error"]["code"] == -32601, "unknown method should be -32601")
    proc.stdin.write("this is not json\n")
    proc.stdin.flush()
    resp = recv()
    check(resp["error"]["code"] == -32700, "parse error should be -32700")
    # get_document round trip
    res = search(ctx.svc.base, "Phoenix reliability")
    doc_id = res["results"][0]["doc_id"]
    send({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
          "params": {"name": "localgate_get_document", "arguments": {"doc_id": doc_id}}})
    resp = recv()
    check("Document" in resp["result"]["content"][0]["text"], "get_document bad")
    proc.stdin.close()
    proc.wait(timeout=10)
    check(proc.returncode == 0, f"mcp exit code {proc.returncode}")


@scenario("S9 MCP 网关离线：工具调用返回可读错误而非崩溃")
def s9(ctx):
    env = dict(os.environ)
    env["LOCALGATE_API"] = "http://127.0.0.1:59999"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py"), "mcp", "--api", env["LOCALGATE_API"]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, cwd=ROOT)

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    recv()
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "localgate_search", "arguments": {"query": "anything"}}})
    resp = recv()
    check(resp["result"].get("isError") is True, "offline call should be isError")
    check("not reachable" in resp["result"]["content"][0]["text"],
          "offline message not helpful")
    proc.stdin.close()
    proc.wait(timeout=10)


@scenario("S10 Embedding 后端宕机：FT降级可用、自检报警、服务不倒")
def s10(ctx):
    svc = ctx.svc_degraded
    base = svc.base
    wait_until(lambda: ctx.status(svc)["index"]["docs"] == 8, 150,
               desc="degraded instance indexes docs (embed fails, FT kept)")
    res = search(base, "深度学习")
    check(res["results"], "FT degraded search returned nothing")
    check(res["degraded"] is True, "search should be flagged degraded")
    def warned():
        _s, body = http_json("GET", base + "/api/logs/selfcheck?lines=3")
        return body["entries"] and body["entries"][0]["status"] in ("warning", "error")
    wait_until(warned, 30, desc="selfcheck flags embedding outage")
    _s, body = http_json("GET", base + "/api/logs/selfcheck?lines=3")
    entry = body["entries"][0]
    emb = [i for i in entry["check_items"] if i["item"] == "embedding_health"][0]
    check(emb["status"] in ("warning", "error"), f"embedding health not flagged: {emb}")
    st = ctx.status(svc)
    check(st["selfcheck"]["last_status"] in ("warning", "error"), "overall status")
    check(st["index"]["docs"] == 8, "service died from embedding outage?")


@scenario("S11 持久化与重启：索引免重建、检索即用")
def s11(ctx):
    svc = ctx.svc
    before = ctx.status()["index"]
    svc.stop()
    svc.start()
    st = ctx.status()
    check(st["index"]["docs"] == before["docs"], "docs lost on restart")
    wait_until(lambda: search(svc.base, "深度学习 神经网络")["results"], 20,
               desc="search after restart")
    check("读书笔记" in search(svc.base, "深度学习")["results"][0]["path"],
          "relevance broken after restart")


@scenario("S12 白名单变更：移出白名单的目录被清理出索引")
def s12(ctx):
    vault = ctx.vault
    old = ctx.svc
    old.stop()
    svc2 = Service("reduced", ctx.workdir, vault, ctx.port2,
                   blacklist=[os.path.join(vault, "src"),
                              os.path.join(vault, "notes", "private")])
    svc2.start()
    ctx.svc2 = svc2
    try:
        wait_until(lambda: ctx.status(svc2)["index"]["docs"] == 7, 120,
                   desc="blacklisted-on-restart dir dropped")
        res = search(svc2.base, "process_training_data dataset")
        check(all("src" not in (r["path"] or "") for r in res["results"]),
              f"code still searchable after leaving whitelist view: "
              f"{[r['path'] for r in res['results']]}")
    finally:
        svc2.stop()
        old.start()  # restore the main service for later scenarios


@scenario("S13 并发：扫描进行中检索依然可用")
def s13(ctx):
    base = ctx.svc.base
    ctx.rescan()
    # immediate searches must succeed during scan
    for q in ("深度学习", "phoenix", "process_training_data"):
        res = search(base, q)
        check(isinstance(res["results"], list), f"concurrent search failed: {q}")


@scenario("S14 OCR：本地引擎产出图片索引记录（不可用时优雅降级）")
def s14(ctx):
    st = ctx.status()
    check(st["ocr"]["mode"] == "auto", "ocr mode")
    check(st["index"]["docs_by_kind"].get("image") == 1,
          f"expected 1 image doc, got {st['index']['docs_by_kind']}")
    # engine recorded (vision on this Mac, tesseract elsewhere, or graceful None)
    check(st["ocr"]["engine_available"] in ("vision", "tesseract", None),
          f"unexpected engine: {st['ocr']['engine_available']}")


@scenario("S17 符号链接策略：指向白名单外的链接一律 fail-closed")
def s17(ctx):
    if sys.platform == "win32":
        print("    SKIP (symlink privileges vary on Windows)")
        return
    import make_samples as ms
    vault = os.path.join(ctx.workdir, "vault-sym")
    os.makedirs(vault, exist_ok=True)
    ms.write_text(os.path.join(vault, "real.md"),
                  "A real visible note about symphony orchestras.")
    outside = os.path.join(ctx.workdir, "outside-sym")
    os.makedirs(outside, exist_ok=True)
    secret_path = os.path.join(outside, "secret.md")
    ms.write_text(secret_path, "TOP SECRET content that must never be indexed.")
    # dir symlink pointing outside the whitelist
    os.symlink(outside, os.path.join(vault, "link_dir"))
    # file symlink pointing outside the whitelist
    os.symlink(secret_path, os.path.join(vault, "leak.md"))
    # whitelist root reached THROUGH a parent symlink
    link_root = os.path.join(ctx.workdir, "vault-sym-link")
    if os.path.islink(link_root):
        os.remove(link_root)
    os.symlink(vault, link_root)
    svc = Service("sym", ctx.workdir, vault, free_port(), whitelist=[link_root])
    svc.start()
    try:
        wait_until(lambda: ctx.status(svc)["index"]["docs"] == 1, 60,
                   desc="only the real file indexed (both symlinks skipped)")
        res = search(svc.base, "TOP SECRET must never be indexed")
        check(all("secret" not in (r["path"] or "") and "leak" not in (r["path"] or "")
                  for r in res["results"]),
              f"outside content leaked through symlink: {res['results']}")
        res = search(svc.base, "symphony orchestras")
        check(any("real.md" in (r["path"] or "") for r in res["results"]),
              "real file behind symlinked whitelist root not indexed")
    finally:
        svc.stop()


@scenario("S18 调试面板：文件名中的 HTML 一律转义（防注入）")
def s18(ctx):
    if sys.platform == "win32":
        print("    SKIP ('<'/'>' are illegal in Windows filenames)")
        return
    base = ctx.svc.base
    evil = os.path.join(ctx.vault, "notes", "<script>alert(1).md")
    try:
        make_samples.write_text(evil, "panel injection marker uniquewords here.")
        wait_until(lambda: any("alert(1)" in (r["path"] or "")
                               for r in search(base, "panel injection marker")["results"]),
                   30, desc="evil-named file indexed")
        _status, html = http_json("GET", base + "/")
        check("<script" not in html,
              "raw <script> from filename reached the panel HTML unescaped")
        check("&lt;script&gt;" in html, "expected escaped filename in panel")
    finally:
        if os.path.exists(evil):
            os.remove(evil)
    wait_until(lambda: all("alert(1)" not in (r["path"] or "")
                           for r in search(base, "panel injection marker")["results"]),
               30, desc="evil-named file dropped from index")


@scenario("S19 并发压力：监听变更与检索同时进行状态不撕裂")
def s19(ctx):
    import threading
    base = ctx.svc.base
    errors: list[str] = []
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                status, body = http_json("POST", base + "/api/search",
                                         {"query": "深度学习"})
                if status != 200:
                    errors.append(f"search http {status}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
            time.sleep(0.05)

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    churn = os.path.join(ctx.vault, "notes", "churn-note.md")
    try:
        for i in range(8):
            make_samples.write_text(
                churn, f"# churn round {i}\n\nchurnmarker{i} transient content.")
            time.sleep(0.4)
    finally:
        if os.path.exists(churn):
            os.remove(churn)
    time.sleep(2.5)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    check(not errors, f"concurrent search errors: {errors[:3]}")
    wait_until(lambda: ctx.status()["index"]["docs"] == 8, 30,
               desc="doc count settled after churn")
    st = ctx.status()
    statuses = st["index"]["docs_by_status"]
    check(all(s in ("ok", "ocr_empty") for s in statuses) and
          sum(statuses.values()) == 8,
          f"docs not settled/healthy after churn: {statuses}")
    check(st["index"]["load_errors"] == 0, "index load errors after churn")


@scenario("S15 只读红线：整个测试期间用户文件逐一未被改动、无新增文件")
def s15(ctx):
    before = ctx.vault_hashes
    after = tree_hashes(ctx.vault)
    check(set(before) == set(after),
          f"file set changed: +{set(after) - set(before)} -{set(before) - set(after)}")
    for path, h in before.items():
        check(after.get(path) == h, f"user file modified: {path}")


@scenario("S16 隐私边界：源码无外部网络端点（全 loopback/本地）")
def s16(ctx):
    url_re = re.compile(r"""["'](https?://[^"'\s]+)["']""")
    allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "localgate")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            with open(p, encoding="utf-8") as f:
                src = f.read()
            for m in url_re.finditer(src):
                url = m.group(1)
                if "{" in url:
                    continue  # f-string: host comes from config at runtime
                host = url.split("://", 1)[1].split("/")[0].split(":")[0]
                check(host in allowed_hosts or host.startswith("LOCALGATE"),
                      f"external endpoint hardcoded in {fn}: {url}")


# ============================================================ runner

class Ctx:
    def __init__(self, workdir, vault):
        self.workdir = workdir
        self.vault = vault
        self.vault_hashes = tree_hashes(vault)
        self.svc = None

    def status(self, svc=None) -> dict:
        svc = svc or self.svc
        _s, body = http_json("GET", svc.base + "/api/status")
        return body

    def rescan(self):
        http_json("POST", self.svc.base + "/api/rescan")


def run():
    keep = os.environ.get("LG_SCENARIO_KEEP") == "1"
    workdir = tempfile.mkdtemp(prefix="localgate-scenario-")
    print(f"[scenario workdir] {workdir}")
    try:
        return _run_in(workdir)
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def _run_in(workdir: str) -> int:
    vault = os.path.join(workdir, "vault")
    make_samples.build_sample_vault(vault)
    ctx = Ctx(workdir, vault)
    port = free_port()
    ctx.port2 = free_port()
    ctx.svc = Service("main", workdir, vault, port,
                      blacklist=[os.path.join(vault, "notes", "private")])
    svc_degraded_port = free_port()
    ctx.svc_degraded = Service(
        "degraded", workdir, vault, svc_degraded_port,
        blacklist=[os.path.join(vault, "notes", "private")],
        embedding={"backend": "ollama", "ollama_url": "http://127.0.0.1:59998",
                   "model": "nope", "timeout_s": 3})
    ctx.svc_empty = Service("empty", workdir, vault, free_port(), whitelist=[])
    try:
        ctx.svc.start()
        ctx.svc_degraded.start()
        ctx.svc_empty.start()
        scenarios = [s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13, s14,
                     s17, s18, s19, s15, s16]
        for fn in scenarios:
            name = fn._scenario_name
            print(f"\n=== {name}", flush=True)
            t0 = time.time()
            try:
                fn(ctx)
                PASS.append(name)
                print(f"    PASS ({time.time() - t0:.1f}s)")
            except AssertionError as e:
                FAILED.append((name, str(e)[:400]))
                print(f"    FAIL: {str(e)[:400]}")
            except Exception as e:  # noqa: BLE001
                import traceback
                FAILED.append((name, f"{type(e).__name__}: {e}"))
                print(f"    ERROR: {type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        for s in ("svc", "svc_degraded", "svc_empty", "svc2"):
            s_obj = ctx.__dict__.get(s)
            if s_obj:
                s_obj.stop()

    print("\n" + "=" * 60)
    print(f"scenarios: {len(PASS)} passed, {len(FAILED)} failed")
    for name, err in FAILED:
        print(f"  FAIL {name}: {err[:200]}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(run())
