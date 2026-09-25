# LocalGate

[![CI](https://github.com/omgamy1179-dev/localgate/actions/workflows/ci.yml/badge.svg)](https://github.com/omgamy1179-dev/localgate/actions/workflows/ci.yml)
[![CodeQL](https://github.com/omgamy1179-dev/localgate/actions/workflows/codeql.yml/badge.svg)](https://github.com/omgamy1179-dev/localgate/actions/workflows/codeql.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Release](https://img.shields.io/github/v/release/omgamy1179-dev/localgate)](https://github.com/omgamy1179-dev/localgate/releases)
[![Security policy](https://img.shields.io/badge/security-policy-purple)](SECURITY.md)

**LocalGate** 是一个隐私优先的**本地检索网关**:把你自己电脑上分散的笔记、PDF、
DOCX、源码、聊天记录导出和图片(本地 OCR)统一建立**本机索引**,通过
**MCP Server(stdio)+ HTTP API(仅回环)**供本地 AI Agent 调用。

- **数据不出本机**:解析、分块、向量嵌入、检索全部本地执行;索引只写入你
  配置的本地目录。
- **只读红线**:对你的文件只读,绝不编辑/创建/删除/重命名白名单内的文件。
- **默认零扫描**:发行版配置的白名单为空——不添加白名单,一个文件都不会被索引。
- **零强制依赖**:核心只用 Python 标准库,离线可用。

**不适合的场景**:需要云端同步/多人协作的检索;让数据出本机的 RAG 服务;
作为笔记编辑器或聊天界面(它刻意不做这些)。

**平台**:Linux / macOS / Windows,Python **3.10+**。

---

## 1. 安装

从源码安装(推荐放入虚拟环境):

```bash
python -m pip install .            # 核心零第三方依赖
python -m pip install ".[yaml,pdf]"  # 可选:更完整的 YAML 解析与 PDF 抽取
```

安装后提供 `localgate` 命令;也可以不经安装直接运行 `python main.py ...`。

## 2. 最小配置

当前目录没有 `config.yaml` 时,程序使用安全的内置默认值(空白名单、
仅回环、索引写入 `./data`)。要真正开始检索,创建一份配置:

```bash
cp config.yaml myconfig.yaml
```

编辑 `myconfig.yaml`,把你要检索的目录加入白名单(其他默认值已经安全):

```yaml
paths:
  whitelist:
    - /home/me/Notes        # 换成你自己的目录;支持 ~/ 展开
    - ~/Documents/vault
```

说明:

- 相对路径(如 `data_dir: ./data`)一律**相对于配置文件所在目录**解析,与
  启动时的工作目录无关;
- `blacklist` 优先级高于 `whitelist`;
- `embedding.backend` 默认 `local`(内置哈希嵌入,离线、确定性);
  若改为 `ollama`,**只允许本机回环地址**(如 `http://127.0.0.1:11434`)。
  配置加载时会做严格校验:远程 IP、非回环 DNS 解析、https、URL 用户信息、
  非常规路径等一律拒绝(fail closed),并把地址重写为解析后的字面 IP,
  防止 DNS rebinding。

## 3. 运行

```bash
localgate serve  -c myconfig.yaml   # 启动网关:HTTP API + 文件监听 + 自检守护
localgate index  -c myconfig.yaml   # 一次性扫描白名单建索引后退出
localgate status -c myconfig.yaml   # 打印网关状态 JSON
localgate mcp    -c myconfig.yaml   # 运行 stdio MCP Server(供 Agent 拉起)
```

不加 `-c` 时读取 `./config.yaml`;仍找不到则使用内置默认值(空白名单,
只读服务,索引文档数为 0 并打印告警)。

## 4. 接入 MCP 客户端

MCP Server 通过 stdio 与客户端通信,把工具调用代理到本机网关(默认
`http://127.0.0.1:8770`)。客户端配置示例(`.mcp.json` /
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "localgate": {
      "command": "localgate",
      "args": ["mcp"]
    }
  }
}
```

从源码运行可用:`"command": "python3", "args": ["/path/to/localgate/main.py", "mcp"]`。

**协议支持矩阵**:MCP 协议版本 `2025-06-18`、`2025-03-26`、`2024-11-05`
(初始化时与客户端协商,按官方规范回退到服务端最新版本)。已通过 MCP
官方 Python SDK 的客户端完成端到端握手测试;未对全部第三方客户端逐一
验证,遇到问题请附带客户端名称与版本提 issue。

工具:

| 工具 | 说明 |
| --- | --- |
| `localgate_search` | 混合检索(BM25 + 向量融合),返回片段与路径 |
| `localgate_status` | 索引健康:文档/分块数、自检状态、白名单 |
| `localgate_get_document` | 按 doc_id 取回文档元数据与分块 |

## 5. HTTP API(默认仅绑定 `127.0.0.1:8770`)

服务端校验 `Host` 必须是回环地址、`Origin` 若存在必须是回环来源
(防 DNS rebinding 与跨站请求),JSON POST 接口要求
`Content-Type: application/json`。

| 端点 | 说明 |
| --- | --- |
| `GET /health` | 存活检查(自检循环用它自 ping) |
| `GET /` | 最小只读状态面板(索引/进度/错误,HTML 已转义) |
| `GET /api/status` | 索引统计、监听与自检状态、生效配置 |
| `POST /api/search` | `{"query": "...", "top_k": 8}`;query ≤ 8192 字符,top_k ≥ 1(服务端上限 100) |
| `GET /api/document/{id}` | 文档元数据与分块;`id` 为 16 位十六进制 doc_id |
| `GET /api/logs/selfcheck?lines=50` | 最近自检 JSONL 记录(1 ≤ lines ≤ 1000) |
| `GET /api/logs/service?lines=50` | 最近服务 JSONL 记录(同上) |
| `POST /api/rescan` | 触发一次白名单扫描;返回真实调度状态 |

`POST /api/rescan` 的响应是可观察的执行状态,例如:

```json
{"rescan_started": true, "mode": "watcher", "reason": ""}
```

- watcher 线程存活:`mode: "watcher"`,请求进入其下一轮;
- watcher 未启用/已停止:直接启动一次性后台扫描(`mode: "oneshot"`);
- 已有扫描在跑:`rescan_started: false`,原因写明。

进度与完成情况通过 `GET /api/status` 的 `ingest.scanning` 与
`watcher.last_pass` 观察;空白名单时 rescan 明确拒绝(`reason:
"whitelist is empty; nothing to scan"`),绝不假成功。

错误约定:参数/校验错误返回 4xx 与简短消息;内部错误统一返回
`{"error": "internal server error"}`,详细堆栈只写入本地结构化日志。

## 6. OCR 与嵌入

- **OCR**:`ocr.mode: auto` 时按 tesseract → macOS Vision(首次用系统
  swiftc 在本地编译助手,缓存于索引目录 `bin/`)→ 跳过并计数的顺序降级,
  绝不联网。配置里的语言标签是 BCP-47(如 `zh-Hans`),Vision 直接使用;
  传给 tesseract 前会映射为 `chi_sim+eng` 风格的语言码。
- **嵌入**:`local` 后端为纯本地哈希嵌入(离线、确定性、无模型下载);
  `ollama` 后端调用本机 Ollama(地址强制回环,禁用跟随重定向)。

## 7. 自检循环

服务启动后,自检守护按配置间隔(默认 300 秒)执行 7 项检查:文件源、
索引完整性、性能指标、嵌入健康、服务存活、资源阈值、安全自动优化
(孤儿清理/段合并/单文件重试)。每轮写一条 JSONL 结构化日志;连续出错
按指数退避降频;白名单根目录暂时不可访问(如外置卷未挂载)时**保留索
引不清空**,只有可确认的文件删除才会移除对应索引条目。

## 8. 故障排查

| 现象 | 处理 |
| --- | --- |
| 启动即告警 "whitelist is empty" | 正常。把你的目录加入 `paths.whitelist` 才会索引 |
| `config error: ... ollama_url ...` | 嵌入地址必须能解析到本机回环;远程地址一律拒绝 |
| 文件改了但检索结果没更新 | watcher 轮询有间隔;或 `POST /api/rescan` 立即扫描,`GET /api/status` 观察 `ingest.scanning` |
| OCR 全部 `ocr_unavailable` | 未安装 tesseract 且(非 macOS 或无 swiftc);属预期降级,图片仍会建立索引记录 |
| 索引疑似损坏 | 重启服务即可:加载器容忍坏行并记录,自检会定位并单文件重建 |
| 端口被占用 | 换 `server.port`;服务只能绑定回环地址,非回环会在启动时被拒绝 |

## 9. 开发与测试

```bash
python -m pip install -e .[dev]
python -m pytest tests --cov=localgate      # 单元 + 安全边界测试(覆盖率报告)
python tests/run_scenarios.py               # 端到端场景套件(真实子进程服务)
python -m ruff check localgate tests main.py
python -m mypy
python -m build && python -m twine check dist/*
```

测试全部使用临时目录与动态端口,不依赖开发者机器上的路径或既有索引。

## 10. 卸载

```bash
python -m pip uninstall localgate
# 索引与日志是本地数据,不会被包管理器删除;确认无用后手动删除:
#   rm -rf ./data ./logs
```

## 11. 文档与社区

- 许可证:[LICENSE](LICENSE)(MIT)
- 安全策略与漏洞报告:[SECURITY.md](SECURITY.md)
- 贡献指南:[CONTRIBUTING.md](CONTRIBUTING.md)
- 行为准则:[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- 变更记录:[CHANGELOG.md](CHANGELOG.md)
- 支持渠道与边界:[SUPPORT.md](SUPPORT.md)

GitHub 标签:`local-rag` `mcp-server` `ollama` `privacy-first` `vector-search` `agent-tool` `local-index`
