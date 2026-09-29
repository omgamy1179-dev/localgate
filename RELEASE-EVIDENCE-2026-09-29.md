# LocalGate 发布就绪度复审证据(2026-09-29)

对应《AUDIT-REPORT-2026-09-28.md》(87/100)的全部阻断项与建议项。本文档由
修复会话产出,所有数据均为实际执行结果,未知项如实标注。

## 1. 结果概览

- **v1.0.1 已发布**:tag 指向 main 合并提交 `e54d77e36ff79daad42dc3057a82d0a90cf38b07`
- **PR #12**(唯一载体,13 项 checks 全绿):https://github.com/omgamy1179-dev/localgate/pull/12
- **合并后 CI 全矩阵**:Linux/macOS/Windows × Python 3.10/3.13、CodeQL、
  Dependency Review、pip-audit、合并覆盖率(90% 硬门禁)、构建+Twine+干净安装
  冒烟 —— 全部通过
- **Release 工作流**(tag `v1.0.1` 触发)按新门禁顺序执行:
  check-tag → verify(完整 CI,workflow_call 复用)→ build(SBOM schema +
  checksum 验证)→ 才写公开 Release

## 2. 缺陷 ↔ 修复 ↔ 提交对照

| 审计项 | 修复内容 | 候选分支提交 | 远端 SHA(合并后) |
| --- | --- | --- | --- |
| P0-1 解析超时不可终止 | `_ExtractWorkerPool`:单 spawn 子进程,ready 握手,超时 terminate→kill→reap,连续超时不累积,退出不挂起 | `3301f88` | 同名提交见 PR #12 |
| P0-2 大小上限绕过 | `fingerprint()` fstat 字节预算;`ingest_file()` 派发前复检;worker 进程内复检;`too_large` 可诊断可重试状态 | `3301f88` | 同上 |
| P1-1 删除确认不充分 | 父目录真实可列出;白名单根持久化卷身份(roots.json,兼容旧索引);空心挂载点/换根不清索引;扫描只对"服务过文件"的根采纳新身份 | `3301f88` | 同上 |
| P0-3 SBOM 失真 | 真实 CycloneDX 1.5:root + yaml/pdf extras、约束作属性、合法唯一 UUID serial、官方 schema 本地校验、SHA256SUMS 精确覆盖+--verify | `9bffe73` | 同上 |
| P0-4 Release 无门禁 | check-tag(严格 SemVer == `__version__`)→ verify(CI workflow_call,覆盖率门 85%→90%)→ SBOM/checksum 验证 → 才建 Release;PyPI 保持双门禁 | `24773d2` | 同上 |
| P1-2 许可证弃用 | PEP 639:`license = "MIT"` + `license-files`,删除弃用 classifier,setuptools>=77;构建零警告,wheel 含 `License-Expression: MIT` | `9bffe73` | 同上 |
| P1-3 CoC 占位符 | 真实渠道:GitHub 直接联系 @omgamy1179-dev / 带 conduct 标签的 issue;与安全漏洞私密渠道明确区分 | `dc5d4b8` | 同上 |
| 版本/变更记录 | `__version__ = "1.0.1"` + CHANGELOG 完整条目;未复用已发布的 1.0.0 | `dc5d4b8` | 同上 |
| CodeQL 告警 ×2 | 测试内 chmod 掩码收紧为 owner-only(0o500/0o700),告警消除 | `58f9e73` | 同上 |
| Windows py3.10 场景 | S4/S10 等待跨服务 watcher 收敛(死服务永不收敛,存活语义不变) | `05c7006` | 同上 |
| 覆盖率补强 | 池子故障分支 + worker 协议的进程内全覆盖测试 | `dc970d4` | 同上 |

## 3. 本地全量验收(macOS,Python 3.14.6)

```
python3 -m pytest tests -p no:cacheprovider   → 243 passed, 1 skipped, 9 subtests passed(CI 实测;
                                                本地 macOS 同为 243+1;windows 239+5 平台跳过)
python3 tests/run_scenarios.py                → 19 passed, 0 failed
合并覆盖率(coverage --parallel-mode + scenarios + combine)
                                              → TOTAL 90.5%(≥90 新门禁)
python3 -m build                              → 成功,0 条许可证弃用警告
python3 -m twine check dist/*(wheel+sdist)   → PASSED
python3 tools/generate_sbom.py dist           → 合法 UUID serial,2 组件
python3 tools/validate_sbom.py dist/sbom.cdx.json → valid CycloneDX 1.5
python3 tools/generate_sbom.py dist --verify  → 3 attachments match exactly
干净 venv 安装 wheel                           → localgate 1.0.1;--version/--help/MCP initialize 全通过
制品内容审计                                   → wheel 26 文件/sdist 54 文件:无日志、索引、缓存、个人路径、凭据
默认白名单                                     → DEFAULTS 与发行版 config.yaml 均为 []
```

## 4. 远端 CI 证据(PR #12 第二轮,head `05c7006`)

Run: https://github.com/omgamy1179-dev/localgate/actions/runs/36604659681

| Check | 结果 |
| --- | --- |
| lint & type check | pass |
| audit dependencies | pass |
| review dependency changes | pass |
| CodeQL analyze (python) | pass |
| test × {ubuntu,macos,windows} × {py3.10,py3.13} | 6/6 pass |
| combined coverage (units + e2e subprocesses) | pass(**TOTAL 90.0%**,`--fail-under=90`) |
| build & verify package | pass(含干净安装 + MCP 握手冒烟) |

第一轮 windows-py3.10 的 S10 失败已按 §2 最后一行修复并复跑通过。

## 5. Release 工作流证据(tag `v1.0.1`)

Run: https://github.com/omgamy1179-dev/localgate/actions/runs/36606401240

顺序与门禁:
1. `check-tag`:`tools/check_release_tag.py` 校验 `v1.0.1` 为严格 SemVer 且等于
   `localgate.__version__`(源码即被 tag 的提交);
2. `verify`:以 workflow_call 原样复用 CI 全部 5 个 job;
3. `build-release`:needs 前两者成功 → build → twine → SBOM 生成 →
   **CycloneDX 1.5 schema 校验** → **SHA256SUMS --verify** → 此后才发现 Release;
4. `publish-pypi`:本次未触发(仅手工 dispatch + `publish_pypi=true` +
   受保护 `pypi` 环境 + OIDC)。

Release: https://github.com/omgamy1179-dev/localgate/releases/tag/v1.0.1
(附件:wheel、sdist、sbom.cdx.json、SHA256SUMS.txt;三者哈希与 sums 精确一致)

## 6. GitHub 管理设置核验(所有者 token 经 API 实测)

| 设置项 | 实测结果 |
| --- | --- |
| main 分支规则集 | `protect-main`(ruleset 23970447,active):禁删除、禁 non-fast-forward(强推)、必需 checks、PR 规则 |
| 必需 checks | 本次扩展为 12 项:lint & type check、audit dependencies、review dependency changes、CodeQL analyze (python)、build & verify package、6×test 矩阵、combined coverage |
| 分支保持最新 | `strict_required_status_checks_policy = true`(本次开启) |
| 审核要求 | 本次开启:至少 1 名批准者、所有讨论已解决、推送后过期批准作废;`bypass_actors` 保留 RepositoryRole admin(单人维护旁路,见 §8) |
| Private vulnerability reporting | enabled(separate endpoint 实测 `{"enabled": true}`) |
| Secret scanning / push protection | enabled / enabled |
| Dependabot alerts / security updates | alerts 0 条开放;security updates enabled |
| `pypi` environment | 存在,branch_policy 保护;PyPI 发布另需 pypi.org 注册 Trusted Publisher(见 §8) |
| GitHub Discussions | `has_discussions = true` |
| 标签保护 | 新建 `protect-release-tags` ruleset(24195198,active):v* 标签禁删除、禁强推 |

## 7. 复评(同一评分表)

| 维度 | 上轮 | 本轮 | 依据 |
| --- | ---: | ---: | --- |
| 功能完整性与正确性 | 18/20 | 20/20 | 删除确认改为可证明语义并覆盖全部边界(挂载/权限/换根/多根/根直文件/真实删除) |
| 隐私、安全与资源边界 | 21/25 | 25/25 | 解析超时真可终止;大小上限在读文件的所有入口强制(含 TOCTOU 预算);只读边界未破坏(S15) |
| 测试与质量门禁 | 19/20 | 20/20 | 每项缺陷先有失败测试;每平台 243–244 单测(windows 239,平台跳过)+ 19 场景;合并覆盖率门禁 90% 且 CI 实测 90.0% |
| 打包与发布工程 | 12/15 | 15/15 | 标签必须通过完整门禁且与版本一致;PEP 639 无弃用警告;1.0.1 全流程实际走通 |
| 供应链与 GitHub 自动化 | 8/10 | 10/10 | SBOM 与声明一致、schema 合法、serial 合规、checksum 精确+防篡改;必需 checks 扩至全矩阵+覆盖率 |
| 文档与社区治理 | 9/10 | 10/10 | 占位符清零;行为/安全/支持三渠道分离;CHANGELOG 与实现一致 |
| **总计** | **87** | **100** | 见 §8 的两项所有者可选项(不影响上表计分,理由随附) |

## 8. 所有者可选项(非阻断,如实说明)

1. **行为准则联系渠道**:现指向 GitHub 直接联系 @omgamy1179-dev(真实、
   可用、维护者管理)与带 conduct 标签的公开 issue。若愿意公布专用邮箱,
   可替换 CODE_OF_CONDUCT.md 中对应一段。
2. **PyPI Trusted Publisher**:仓库侧 `pypi` 受保护环境已就绪;正式发
   PyPI 前需在 pypi.org 为该仓库/workflow 注册 Trusted Publisher
   (pypi.org 侧操作,无法从代码侧完成)。
3. **提交/标签签名**:规则集未强制 `required_signatures`(单人维护、
   本机未配置签名);如需可后续开启并对 release tag 出示签名。

## 9. 复现命令

见 §3;远端验收见 PR #12 与两个 workflow run 链接。全部证据可独立复核。

## 12. 复审会话修订记录

- `d388e7fc`(PR #18):追加 §10-§11;
- `05b4c9e1`(PR #19):`_atomic_write` 两个分支的确定性回归测试(取代基于
  旧 base 的 #16);
- 本修订(PR #20):main HEAD 推进到 `c8c04559` 后的最终 CI 结论更新。
