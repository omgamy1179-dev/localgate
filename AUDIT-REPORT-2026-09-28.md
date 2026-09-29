# LocalGate 全盘完成度与 GitHub 发布就绪审计

审计日期：2026-09-28  
本地审计提交：`d8d6afcce8fb90aadd94f2a06289ddc66200c9b1`  
公开仓库：<https://github.com/omgamy1179-dev/localgate>  
结论：**综合发布就绪度 87/100，未达到 90+ 停止线，也未达到 100 分目标。**

## 1. 执行摘要

LocalGate 的核心功能、隐私边界、跨平台 CI、测试覆盖率、Python 打包和基础社区文件已经形成完整体系。当前代码不是“未完成项目”，而是一个已发布 `v1.0.0`、本地质量门禁整体健康，但仍有若干发布工程和极端输入安全边界需要收口的项目。

本轮验证结果：

- 单元/集成测试：`185 passed`；
- 端到端场景：`19 passed, 0 failed`；
- 综合语句/分支覆盖率：`90.4%`，通过现有 `85%` 闸门；
- Ruff：通过；
- Mypy：19 个源文件通过；
- `compileall`：通过；
- `pip check`：无依赖破损；
- sdist/wheel 构建：通过；
- `twine check`：两个制品均通过；
- 干净虚拟环境 wheel 安装、`localgate --version`、`--help`：通过；
- 发布版默认白名单为空：已验证；
- 公开 GitHub `v1.0.0` Release workflow：成功；
- 公开 GitHub 最新已合并依赖更新的 CI 与 CodeQL：成功；
- 未发现被 Git 跟踪的运行数据、日志、缓存或构建制品；未扫描到实际凭据。

因此，当前主要风险集中于少数可以明确修复和验收的问题，不需要重写架构。

## 2. 评分

| 维度 | 得分 | 满分 | 判断 |
| --- | ---: | ---: | --- |
| 功能完整性与正确性 | 18 | 20 | MCP、HTTP、索引、OCR 降级、自检、持久化和只读边界均有测试；删除确认仍有边界缺口 |
| 隐私、安全与资源边界 | 21 | 25 | 回环限制、Host/Origin、URL 解析、请求上限良好；提取超时和重试文件上限未形成硬隔离 |
| 测试与质量门禁 | 19 | 20 | 185+19 全通过、综合覆盖率 90.4%；新增缺陷尚无回归测试 |
| 打包与发布工程 | 12 | 15 | build、twine、clean install 均通过；标签发布未先执行完整质量门禁，许可证元数据已弃用 |
| 供应链与 GitHub 自动化 | 8 | 10 | Actions 固定 SHA、CodeQL/Dependabot/依赖审查齐全；SBOM 不完整且序列号不合规 |
| 文档与社区治理 | 9 | 10 | README、SECURITY、CONTRIBUTING、SUPPORT、模板齐全；行为准则仍有占位符 |
| **总计** | **87** | **100** | **不允许停止；完成下述阻断项并复审** |

评分规则：未能读取的管理员级 GitHub 设置不按“已开启”计分；仅存在文件或 workflow 不等于远端规则已经启用。

## 3. 已完成且达到发布级标准的部分

### 3.1 核心产品与隐私边界

- 默认配置零扫描，白名单为空；所有用户文件操作保持只读。
- HTTP 仅允许回环绑定，并对 `Host`、`Origin`、JSON Content-Type、请求体、查询长度、日志行数实施限制。
- Ollama URL 只允许回环解析，禁止重定向并防止 DNS rebinding。
- 符号链接越界采取 fail-closed 策略。
- MCP stdio 握手、工具发现、调用及离线错误均有端到端覆盖。
- 文件解析、嵌入、OCR、自检和监听的单文件失败不会直接击穿服务。

### 3.2 测试和 CI

- Linux/macOS/Windows 与 Python 3.10/3.13 矩阵已配置。
- CI 包含 Ruff、Mypy、pip-audit、单元测试、端到端场景、覆盖率、构建、Twine 和 wheel 冒烟安装。
- 综合覆盖率实测 `90.4%`，19 个生产模块全部进入覆盖统计。
- Actions 使用完整 commit SHA 固定第三方 action；默认权限为只读，并对发布写权限单独收敛。
- CodeQL、Dependency Review、Dependabot 已配置。

### 3.3 打包、文档和仓库卫生

- wheel 仅包含包代码、元数据和许可证；sdist 内容合理。
- README、LICENSE、SECURITY、CONTRIBUTING、CODE_OF_CONDUCT、CHANGELOG、SUPPORT、CODEOWNERS、Issue/PR 模板均存在。
- `.gitignore` 能覆盖构建产物、缓存、日志、运行数据和编辑器文件。
- `v1.0.0` 标签指向 `ccc6d19`，公开 Release workflow 运行成功：<https://github.com/omgamy1179-dev/localgate/actions/runs/36182061394>。
- 公开可见的远端 CI 与 CodeQL 最近成功记录包括：
  - CI：<https://github.com/omgamy1179-dev/localgate/actions/runs/36188089941>
  - CodeQL：<https://github.com/omgamy1179-dev/localgate/actions/runs/36188089950>

## 4. 阻止 90+ 的问题

### P0-1：解析“硬超时”实际上无法终止解析任务

位置：`localgate/ingest.py:22-51`

当前实现在线程上 `join(deadline)`，超时后只让调用方返回错误；后台 daemon 线程仍继续运行。注释称“至多一个线程残留”，但扫描会继续处理下一个文件，因此多个恶意或病态文件可以持续积累解析线程、CPU 和内存占用。

验收要求：

- 超时必须能终止或隔离实际解析工作；
- 连续多个超时文件不得线性增加存活 worker；
- 超时后下一个正常文件仍能成功索引；
- Linux/macOS/Windows 都通过测试；
- 不得破坏只读边界。

### P0-2：单文件重试路径可绕过 `max_file_mb`

位置：`localgate/fsutil.py:47-61`、`localgate/ingest.py:118-125`、`localgate/ingest.py:272-304`

`fingerprint(path, max_mb)` 接收上限参数却完全未使用，并会读取整个文件。全量扫描先在 `iter_files()` 中检查大小，但 `retry_failed_docs()` 直接调用 `ingest_file()`。若失败文件随后增长为超大文件，自检重试会完整哈希并继续解析，形成资源消耗绕过。

验收要求：

- 大小限制在 `ingest_file()`/`fingerprint()` 的最终入口强制执行；
- 避免检查与读取之间的 TOCTOU 导致无限读取；
- 新增“失败文件变大后重试”的回归测试；
- 超限文件得到可诊断状态，不导致索引服务崩溃。

### P0-3：SBOM 声明与实际内容不一致，序列号不合规

位置：`tools/generate_sbom.py:2-7`、`tools/generate_sbom.py:62-80`

脚本说明声称包含 wheel 声明的可选依赖，但代码遇到 `extra ==` 就跳过。当前 wheel 有 12 条 `Requires-Dist`，生成的 SBOM 却只有 `localgate` 一个 component。`serialNumber` 固定为 `urn:uuid:localgate-release-sbom`，既不是有效 UUID URN，也会在每次发布重复。

验收要求：

- SBOM 至少准确表示发行包及 `yaml`、`pdf` 可选依赖；开发依赖是否纳入必须在文档和实现中一致；
- 每次生成合法、唯一或可验证确定性的 RFC 4122 UUID URN；
- 生成结果通过 CycloneDX 1.5 schema 验证；
- 对组件、依赖关系、序列号和 checksum 文件新增自动测试；
- Release workflow 在上传前执行 SBOM 验证。

### P0-4：标签推送会在未执行完整质量门禁时直接创建 GitHub Release

位置：`.github/workflows/release.yml:9-59`

任何 `v*` tag 都会构建并上传 Release；该 workflow 只做 build 和 Twine 检查，不运行 Ruff、Mypy、测试、覆盖率、安全审计或 SBOM schema 验证，也不校验 tag 与包版本一致。错误标签或从未通过 CI 的提交仍可能产生公开制品。

验收要求：

- 只接受严格 SemVer 标签，并校验 tag 与 `localgate.__version__` 一致；
- 创建 Release 前必须有完整 verify job 成功；
- SBOM 和 checksum 验证通过后才上传；
- 发布失败时不能留下看似成功的公开 Release；
- PyPI 继续保持手工 dispatch + protected environment + OIDC 双重门禁。

## 5. 达到 100 分前应完成的问题

### P1-1：删除确认的实现与文档承诺不一致

位置：`localgate/ingest.py:94-103`

文档字符串声称父目录“存在且可列出”，实现只调用 `isdir()` 与 `exists()`，没有实际列目录。挂载点、权限变化和根目录文件的边界需要更保守处理。

验收要求：父目录不可列出、白名单根临时不可达、挂载设备变化时保留索引；仅在可证实删除时移除。新增多根目录、空挂载点、权限错误和根目录直接文件测试。

### P1-2：Python 许可证元数据已进入弃用期

位置：`pyproject.toml:11,29`

构建成功但 setuptools 警告 `project.license` 表格形式和许可证 classifier 已弃用，并提示将在 2027-02-18 后不再支持。

验收要求：迁移到当前 PEP 639 写法，例如 SPDX `license = "MIT"` 和适当的 `license-files`；去除弃用 classifier；构建输出不再出现该警告。

### P1-3：行为准则仍含公开占位符

位置：`CODE_OF_CONDUCT.md:40`

`[INSERT CONTACT METHOD]` 会使社区举报通道不可执行。

验收要求：填入维护者实际管理且愿意公开的举报渠道，并与 SECURITY 中的安全漏洞渠道区分。

### P1-4：本地与远端分支历史需安全同步

本地 `origin/main` 跟踪引用仍停在 `ccc6d19`，但公开远端 `main` 已是 `42c13a7`；本地 HEAD 是 `d8d6afc`。`git status` 显示的 `ahead 2` 基于过期引用，不能用来判断真实分支关系。

验收要求：先 fetch，再明确比较双方提交；采用 rebase、merge 或新分支 PR 安全整合，禁止 force-push 覆盖远端。最终以 GitHub 上 PR/`main` 的成功 checks 为准。

## 6. 需要仓库所有者在 GitHub UI 验证的项目

当前机器的 GitHub CLI 凭据不可用，公开 API又触发匿名速率限制，以下管理员级设置无法由本轮审计证明。它们必须提供设置截图或可访问的规则链接后才能计入满分：

- `main` 是否禁止直接推送并要求 PR；
- 是否要求 CI、CodeQL、Dependency Review 等 checks 成功；
- 是否要求分支保持最新、至少一名审核者、解决全部会话；
- 是否启用 signed commits 或其他签名策略；
- Private vulnerability reporting 是否真正开启；
- Secret scanning 与 push protection 是否开启；
- Dependabot security updates 是否开启；
- `pypi` environment 是否有审批保护，Trusted Publisher 是否配置；
- GitHub Discussions 是否开启（SUPPORT 已链接）；
- 标签保护或 release 规则是否阻止非授权发布。

## 7. 复现证据

本轮实际执行的关键命令：

```bash
python -m pytest tests -p no:cacheprovider
python tests/run_scenarios.py
python -m ruff check localgate tests main.py
python -m mypy
python -m compileall -q localgate main.py
python -m pip check
python -m build
python -m twine check dist/*
python tools/generate_sbom.py dist
```

覆盖率使用与 CI 相同的组合流程：

```bash
export COVERAGE_PROCESS_START="$PWD/tools/ci-coveragerc.ini"
python -m coverage erase
python -m coverage run --parallel-mode -m pytest tests -p no:cacheprovider
python tests/run_scenarios.py
python -m coverage combine
python -m coverage report --fail-under=85
```

结果：`2606` statements、`866` branches，综合覆盖率 `90.4%`。

审计期间首次在受限沙箱内运行网络测试时出现回环 socket 权限失败；在允许本机回环 socket 的正常执行环境复跑后为 `185 passed`。这属于审计沙箱限制，不计作项目缺陷。

## 8. 最终判定

- **可以继续开发和内测：是。**
- **核心功能完成：基本完成。**
- **现有 v1.0.0 已有可见成功发布记录：是。**
- **当前 HEAD 可直接再发布同版本：否。** 未打标签的提交不能复用 `1.0.0` 制品版本。
- **达到 GitHub 综合发布就绪 90+：否，当前 87。**
- **达到 100：否。**

下一步必须按《GLM-NEXT-STEPS-2026-09-28.md》执行并保留验收证据；只有复评分数超过 90，GLM 才可停止，目标仍是 100。
