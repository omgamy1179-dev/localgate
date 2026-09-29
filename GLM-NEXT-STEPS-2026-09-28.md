# 给 GLM-5.3 Flash 的 LocalGate 下一步工作规划

## 任务目标

把 LocalGate 从当前审计的 **87/100** 提升到 GitHub 综合发布就绪度 **100/100**。你只有在所有硬门禁通过、没有未关闭的 P0、复评分数 **严格大于 90** 且证据已记录时才可以停止。不要用“代码已写”“本地大概通过”代替验收。

依据文件：

- `AUDIT-REPORT-2026-09-28.md`
- 当前本地 HEAD：`d8d6afcce8fb90aadd94f2a06289ddc66200c9b1`
- 远端 `main` 审计时为：`42c13a73bc7912855331853d7f19d7cc98ab8766`

## 总约束

1. 先读取整个项目、`AGENTS.md`（如存在）、审计报告及 Git 状态，再修改。
2. 不重写已有架构，不引入与问题无关的框架或服务；优先最小、可测试、跨平台的修复。
3. 保持核心运行时零强制第三方依赖、默认白名单为空、仅回环、用户文件只读。
4. 不删除或覆盖用户现有修改，不使用 `git reset --hard`，不 force-push。
5. 每个缺陷先补失败测试，再实现修复；测试必须覆盖 Linux、macOS 和 Windows 可行路径。
6. 不降低测试、覆盖率、类型检查或安全门禁来换取绿色。
7. 每完成一阶段就运行该阶段的定向测试；最终必须运行完整门禁。
8. 所有公开声明必须与实现一致。做不到的声明要修实现或收窄措辞，不能保留误导性承诺。

## 阶段 0：同步基线，避免覆盖远端

目标：得到一个包含本地有效改动和远端最新 `main` 的可审查分支。

步骤：

1. 保存并检查工作区现有改动；审计文件属于预期新增文件。
2. `git fetch origin main --tags`。
3. 比较 `origin/main...HEAD`、两个方向的提交和文件差异。
4. 从最新远端 `main` 创建 `codex/release-readiness` 或同等功能分支，将本地尚未进入远端的有效改动安全整合进去。
5. 解决冲突时保留远端已合并 Dependabot 更新和本地端口碰撞重试修复。
6. 运行一轮现有完整测试，确认同步没有引入回归。

验收：历史清楚，无强推需求；`git diff` 只包含有意改动；基线测试为 185 单测/集成和 19 场景通过或更多。

## 阶段 1：关闭 P0 安全与资源边界

### 1A. 让解析超时真正可终止

问题：`localgate/ingest.py::_extract_with_deadline` 的 daemon 线程超时后仍运行。

要求：

- 采用可被终止的隔离边界；优先考虑标准库、跨平台的独立进程方案，而不是继续叠加线程。
- worker 必须是模块级可序列化入口，兼容 Windows `spawn`。
- 主进程只接收有边界的文本、kind 和 metadata；异常转换为既有 `ExtractError` 语义。
- 超时时 terminate，并在合理时间后 kill/清理；不得遗留 zombie、pipe、queue 或临时文件。
- 正常小文件性能不能出现不可接受退化。如果只对风险解析器启用进程隔离，要在代码和文档中说明边界，并测试所有解析类型。

必须新增测试：正常完成、异常传播、单次超时、连续多次超时不累积 worker、超时后继续索引正常文件、服务退出不挂起。

### 1B. 在最终入口强制文件大小上限

问题：`fingerprint()` 忽略 `max_mb`，`retry_failed_docs()` 可绕过 `iter_files()` 上限。

要求：

- `ingest_file()` 与实际读文件的函数必须独立强制上限。
- 读取时有明确字节预算，防止 stat 后文件继续增长导致无界读取。
- 为超限建立可诊断、可重试但不会反复重读全文件的状态。
- 全量扫描、watcher、手工单文件和自检重试使用同一限制。

必须新增测试：边界值、超 1 字节、stat 后增长、失败文件增长后重试、超限文件不进入 extractor/embedder。

### 1C. 修复删除确认语义

问题：`confirmed_gone()` 没有真正证明父目录可列出，挂载/权限边界不完整。

要求：

- 实际验证父目录可列出，并用目录条目确认目标缺失；任何权限/I/O/挂载不确定性均保留索引。
- 白名单根直接包含的文件也不能因临时卸载后的空挂载目录被误删。
- 如需记录根目录设备/卷身份，仅增加完成该安全目标所需的最小持久化数据，并兼容旧索引。

必须新增测试：父目录不可列出、根不存在、空挂载点模拟、多白名单其中一个失联、根目录直接文件、真实删除。

阶段 1 验收：新增测试全部通过；完整 185+19 基线不回退；不再存在线程泄漏、超大文件绕过和不确定删除。

## 阶段 2：修复供应链和 Release 门禁

### 2A. 生成真实且可验证的 CycloneDX SBOM

要求：

- 修复 `tools/generate_sbom.py`，使输出内容与脚本说明一致。
- 至少包含 root component、`yaml` 和 `pdf` 可选运行依赖及正确依赖关系；开发依赖若不进入发布 SBOM，应明确排除并说明原因。
- 不把版本约束伪装成组件版本或无效 purl。
- `serialNumber` 使用合法 UUID URN，且不同构建不会固定复用同一非法值。
- 增加 CycloneDX 1.5 schema 验证。若引入只用于构建/开发的验证工具，把它放入 dev/release 工具链，不得变成核心运行依赖。
- checksum 文件只覆盖预期发布附件，并加入自校验。

必须新增测试：解析 wheel `Requires-Dist`、extras 去重、合法 serial、schema 通过、checksum 精确匹配且篡改失败。

### 2B. 给标签发布加完整前置验证

要求：

- 标签仅允许 `vMAJOR.MINOR.PATCH`，并校验其版本等于 `localgate.__version__`。
- Release 创建前必须通过 lint、mypy、依赖审计、测试、覆盖率、build、Twine、clean-install、MCP handshake、SBOM schema 和 checksum 验证。
- 尽量复用 CI 命令或可调用 workflow，避免维护两套漂移逻辑。
- 任何验证失败都不得创建或更新 GitHub Release。
- PyPI 保持仅 `workflow_dispatch` + `publish_pypi=true` + protected `pypi` environment + OIDC。
- 将 tag commit SHA、制品哈希和 SBOM 作为最终证据。

必须新增验证：错误 tag、tag/version 不一致、SBOM 无效时 workflow 失败；正确候选流程在本地可复现。

阶段 2 验收：新 SBOM schema 通过且包含预期 extras；Release 工作流只有在全门禁通过后才写入公开 Release。

## 阶段 3：清理发布元数据和社区治理

### 3A. 迁移许可证元数据

- 按当前 PEP 639/setuptools 规范使用 SPDX `MIT` 和适当的 `license-files`。
- 删除弃用的许可证 classifier。
- `python -m build` 不再输出许可证弃用警告。
- wheel/sdist 仍包含 LICENSE，Twine 检查继续通过。

### 3B. 补齐行为准则联系渠道

- 将 `CODE_OF_CONDUCT.md` 中 `[INSERT CONTACT METHOD]` 替换为维护者真实管理的渠道。
- 如果无法自行确定真实地址，不得编造；把这一项作为唯一需要仓库所有者输入的阻塞项明确提出。
- 安全漏洞继续走 private vulnerability reporting，社区行为投诉与安全漏洞渠道应区分。

### 3C. 版本与变更记录

- 不得从修复后的非标签 HEAD 重新发布 `1.0.0`。
- 根据兼容性选择 `1.0.1` 或后续版本，更新 `localgate.__version__` 与 CHANGELOG。
- 只在候选提交全部 checks 通过、GitHub 设置确认后创建标签。

## 阶段 4：由仓库所有者确认 GitHub 设置

如果你能使用已授权 GitHub CLI/API，读取并记录实际设置；否则生成一份最短的人工检查清单，请仓库所有者逐项确认，不得把未知写成已通过。

必须确认：

- `main` 需要 PR，禁止直接推送和 force push；
- CI、CodeQL、Dependency Review 是 required checks；
- 分支保持最新、所有讨论已解决、至少一名审核者；
- Private vulnerability reporting；
- Secret scanning 和 push protection；
- Dependabot alerts/security updates；
- `pypi` environment 审批和 PyPI Trusted Publisher；
- GitHub Discussions；
- 标签/release 保护与维护者权限；
- 建议启用签名提交或至少对 release tag 做签名与验证。

把确认结果写入最终证据文件；无法确认的项仍是未完成项。

## 最终全量验收

在干净工作区和受支持的正常网络/回环环境执行：

```bash
python -m ruff check localgate tests main.py tools
python -m mypy
python -m pytest tests -p no:cacheprovider
python tests/run_scenarios.py
export COVERAGE_PROCESS_START="$PWD/tools/ci-coveragerc.ini"
python -m coverage erase
python -m coverage run --parallel-mode -m pytest tests -p no:cacheprovider
python tests/run_scenarios.py
python -m coverage combine
python -m coverage report --fail-under=90
python -m build
python -m twine check dist/*
python tools/generate_sbom.py dist
```

还必须：

- 在全新虚拟环境安装 wheel，验证 `--version`、`--help` 和 MCP initialize；
- 验证 wheel/sdist 不含日志、索引、缓存、个人路径或凭据；
- 验证默认白名单为空；
- 验证 SBOM schema、组件、依赖关系和 checksum；
- 验证 GitHub PR 的 Linux/macOS/Windows、Python 3.10/3.13 全矩阵成功；
- 验证 CodeQL、Dependency Review、pip-audit 成功；
- 检查远端 `main` 与候选提交一致且无未合并的修复；
- 保持综合覆盖率至少 90%，不得只满足旧的 85% 下限。

## 停止规则

只有同时满足以下条件才可停止：

1. P0-1 至 P0-4 全部关闭；
2. 所有新增回归测试和现有完整测试通过；
3. 综合覆盖率 `>= 90%`；
4. Ruff、Mypy、pip-audit、CodeQL、Dependency Review 全部通过；
5. build、Twine、clean install、MCP smoke test 全部通过；
6. SBOM 内容准确、CycloneDX schema 合法、checksum 已验证；
7. Release workflow 在创建公开 Release 前有完整门禁；
8. 本地与远端历史已安全整合，GitHub required checks 全绿；
9. 不存在真实占位符、弃用构建警告或无法解释的管理员设置未知项；
10. 更新审计报告，附上最终 commit SHA、PR、checks、Release 候选和测试证据；
11. 按同一评分表复评 **>90**。目标为 **100/100**；若为 91-99，必须列出剩余扣分和明确原因，不能声称 100。

若遇到必须由仓库所有者提供的联系地址、审批或 GitHub 设置权限，立即把它标为外部阻塞并提交其余可完成工作；不要编造数据，也不要因为外部阻塞就把项目判为已完成。
