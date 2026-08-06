# Market Validator

[English](#english) | [中文](#中文)

## English

`market-validator` is a security-conscious, auditable, and reproducible workflow for validating financial-market hypotheses. It strictly separates research proposals from deterministic execution: AI may propose a plan and explain a verified result, while data facts, transformations, statistics, decision rules, and artifact verification come from tested Python code.

The project is currently Alpha software. It can draft a general statistical hypothesis proposal, but its only end-to-end analysis remains a predefined comparison of WTI price-change volatility. It is not a general quantitative platform, investment adviser, causal inference engine, or trading system.

### Goals and non-goals

- Convert market questions into strict, serializable research protocols.
- Require explicit user review and hash-bound confirmation before analysis.
- Preserve data provenance, parameters, time semantics, quality findings, code version, and run artifacts.
- Strictly reload and verify Bundles, analysis results, reports, and Manifests.
- Produce auditable execution for the same confirmed plan and data snapshot.

The system never places trades. It does not describe association, retrospective comparison, or prediction as causation. AI cannot calculate statistics, confirm plans, bind local data, or execute workflows.

### Trust boundary

```text
User market question
→ optional AI Provider creates an untrusted proposal
→ strict parser validates and canonicalizes it
→ deterministic clarification applies only explicit whitelisted answers
→ user reviews and explicitly confirms the exact proposal SHA-256
→ Python deterministically compiles the confirmed proposal to ResearchSpec
→ a later, separately authorized data/workflow stage may use the ResearchSpec
→ user separately invokes run
→ deterministic transformation, analysis, atomic publication, and strict reload
→ AI may explain only the verified result
→ user makes the final decision
```

The optional AI Provider receives only the original question, a fixed system prompt, the proposal JSON Schema, and the supported-capability description. It never receives a Bundle, local path, request ID, hash, artifact identity, or credential. Model generation is nondeterministic; only canonical proposal bytes and their SHA-256 are reproducible. Every changed proposal requires fresh review and confirmation.

The generic `ResearchHypothesisProposal` path now supports clarification, hash-bound confirmation, and deterministic compilation into the existing `ResearchSpec` model. It does not bind a data provider, download data, or run analysis. The separate fixed WTI `MarketValidationPlanProposal` path still owns Bundle binding and the only implemented end-to-end analysis workflow.

`--allow-network` authorizes one proposal-generation network request only. It is not confirmation and does not authorize `compile-plan`, `run`, data downloads, or artifact publication. Provider names, models, and availability may change; verify current official documentation before making a real request.

### Current capabilities

- Python 3.11+, `src` layout, strict Pydantic v2 models, and Windows IANA timezone data.
- `ResearchSpec`, data requirements, calendar metadata, and an instrument registry.
- Offline CSV validation and an explicitly authorized HTTPS-only FRED Provider.
- Strict legacy/current DataBundle loading with provenance and SHA-256 auditing.
- Missing-aware simple returns, log returns, and signed absolute price changes.
- A predefined WTI `price_change_volatility` analysis with bootstrap and robustness checks.
- Deterministic analysis artifacts, Manifest validation, strict reload, workflow, and offline CLI.
- An optional proposal-only DeepSeek adapter with bounded requests and no automatic retries.
- A strict, provider-neutral `ResearchHypothesisProposal` for general natural-language association/predictive questions, deterministic H0/H1 validation, explicit ambiguities, Schema, canonical JSON, SHA-256, and safe create-only output.
- Stable ambiguity IDs, strict whitelisted clarification answers, explicit hash-bound user confirmation, and deterministic compilation to the existing `ResearchSpec` with an auditable provenance sidecar.

Implemented: ResearchSpec; DataPlan (confirmed, hash-bound); SourceSelection;
AnalysisPlan / AnalysisAuthorization contract (planning + authorization only,
no analysis execution); authorized deterministic analysis execution
(Pearson / Spearman / OLS with classic, HC1 and Newey-West covariance,
(Pearson / Spearman / OLS with classic, HC1 and Newey-West covariance,
session-based alignment, no-lookahead, immutable result directories);
deterministic evidence packaging with grounded LLM interpretation and
Markdown validation reports (no invented numbers, no trading advice);
AcquisitionRequestPlan 1.2 (capability + pre-sample planning, exact FRED
request steps); DataAccessAuthorization (single-use, no paid/retry/fallback);
transactional acquisition snapshots; DataReadinessAssessment 1.1 and
DataReadyManifest 1.1 with exact session-evidence binding; a unified JSON-only
`data-lifecycle` CLI; and a TEST-ONLY synthetic offline example that reaches
Data Ready.

The formally authorized FRED / local CSV acquisition lifecycle is implemented.
Not implemented: automatic provider selection, general cross-market calendar
execution, generic correlation/regression/causal analysis, backtesting,
automatic confirmation/execution, or trading.

### Quick start

```powershell
python -m pip install -e .
python -m market_validator doctor
market-validator doctor
python -m market_validator backends
python -m market_validator hypothesis schema
python -m market_validator hypothesis validate-proposal examples/hypothesis_proposals/oil_to_a_share_energy.proposal.json
python -m market_validator hypothesis clarification-schema
python -m market_validator hypothesis validate-clarifications --proposal examples/hypothesis_proposals/oil_to_a_share_energy.proposal.json --answers examples/hypothesis_proposals/oil_to_a_share_energy.clarifications.json
python -m unittest discover -s tests
```

The console and module entry points are equivalent. Default commands and tests are offline. A FRED fetch without `--live` is a dry run.

Validate an existing proposal and compile a separately confirmed plan:

```powershell
python -m market_validator validate-proposal examples/ai_planning/wti_price_change_volatility.proposal.json

python -m market_validator compile-plan `
  --proposal <proposal.json> `
  --confirmation <confirmation.json> `
  --bundle <bundle.json> `
  --output <workflow-plan.json>

python -m market_validator run `
  --plan <workflow-plan.json> `
  --bundle <bundle.json> `
  --artifact-root <artifact-directory>
```

The optional network proposal command only creates a strictly parsed proposal; it never confirms, compiles, or runs it:

```powershell
python -m market_validator propose-hypothesis `
  --question-file examples/hypothesis_proposals/oil_to_a_share_energy.question.txt `
  --provider deepseek_api `
  --model <verified-current-model> `
  --output <new-hypothesis-proposal.json> `
  --allow-network
```

This generic draft command identifies variables, timing, method, direction, deterministic H0/H1, and unresolved choices. Separate offline commands apply explicit clarifications, create a user confirmation record, and compile only a fully mapped proposal to `ResearchSpec`; none downloads data or runs analysis. The older fixed-capability workflow proposal remains separate:

```powershell
python -m market_validator hypothesis apply-clarifications `
  --proposal <draft-proposal.json> `
  --answers <clarification-answers.json> `
  --output <clarified-proposal.json>
python -m market_validator hypothesis confirm-proposal `
  --proposal <clarified-proposal.json> `
  --output <confirmation.json>
python -m market_validator hypothesis compile-research-spec `
  --proposal <clarified-proposal.json> `
  --confirmation <confirmation.json> `
  --output <research-spec.json>
```

When compilation reports `unresolved_requirements`, supply explicit provider-neutral mappings offline. This creates a new Proposal version, invalidates the old confirmation, and requires a fresh explicit confirmation:

```powershell
python -m market_validator hypothesis apply-completion `
  --proposal <clarified-proposal.json> `
  --answers <completion-answers.json> `
  --output <completed-proposal.json>
python -m market_validator hypothesis confirm-proposal `
  --proposal <completed-proposal.json> `
  --output <new-confirmation.json>
python -m market_validator hypothesis compile-research-spec `
  --proposal <completed-proposal.json> `
  --confirmation <new-confirmation.json> `
  --output <research-spec.json>
```

Generate and confirm a provider-neutral `DataPlan` from a compiled `ResearchSpec` and explicit registry snapshots. Confirmation binds the exact canonical hashes and never authorizes data access:

```powershell
python -m market_validator data-plan generate `
  --research-spec <research-spec.json> `
  --instrument-registry <instruments.json> `
  --calendar-registry <calendars.json> `
  --output <data-plan.json>
python -m market_validator data-plan confirm `
  --plan <data-plan.json> `
  --research-spec <research-spec.json> `
  --instrument-registry <instruments.json> `
  --calendar-registry <calendars.json> `
  --output <data-plan-confirmation.json>
python -m market_validator data-plan validate-confirmation `
  --confirmation <data-plan-confirmation.json> `
  --plan <data-plan.json> `
  --research-spec <research-spec.json> `
  --instrument-registry <instruments.json> `
  --calendar-registry <calendars.json>
```

Ready is not Confirmed, and Confirmed is not executed. Any effective proposal change invalidates the old confirmation hash. Missing existing ResearchSpec fields produce structured unresolved requirements rather than guessed defaults.

```powershell
python -m market_validator propose-plan `
  --question-file examples/ai_planning/wti_question.txt `
  --provider deepseek_api `
  --model <verified-current-model> `
  --output <new-proposal.json> `
  --allow-network
```

### Quick start to Data Ready (TEST-ONLY synthetic, offline)

```powershell
python scripts\verify_v0_3_offline_example.py
python examples\v0.3_data_ready\run_example.py --output-dir <new-empty-dir>
```

The example is fully synthetic and offline: explicit local CSV provider,
explicit session schedule snapshot, no credentials, no network. It prints a
summary with `status: "data_ready"` plus the manifest SHA-256.

```text
TEST-ONLY SYNTHETIC EXAMPLE · NOT MARKET DATA · NOT ANALYSIS · NOT INVESTMENT ADVICE
```

The legacy diagnostic command `market-validator data fred fetch --live`
does NOT use DataAccessAuthorization, does NOT create a Phase 3 transactional
snapshot, and cannot produce a DataReadyManifest. It must not be used as the
formal `data_ready` flow — use `data-lifecycle execution run` instead.

### WTI golden case

- Request ID: `fred-dcoilwtico-20260804T085757136534Z-58aa38ed3b12`
- Bundle SHA-256: `94e89062727b475168dc5605bef4b523e6046786f83474bca0334652a7fc3c7b`
- Artifact ID: `price-change-volatility-0796a66788e5cfd71dff6a3222dd5cab`
- Result SHA-256: `0796a66788e5cfd71dff6a3222dd5cab6062ab73d7b5dd9e1ccfbe9bbdc12435`
- Report SHA-256: `c6e0d4a7d2c8e57c9da8985f904de8bcec6d14f1be35d8893ea296aeb277967b`
- Manifest SHA-256: `5be9387e8425838931618c585805a19e046a4b4ba9da5950373d83385e960a2e`
- Fixed conclusion: `supported`

This conclusion applies only to the predefined retrospective per-observation price-change volatility comparison. It is not causal evidence and does not prove predictability, profitability, or annualized equal-interval daily volatility.

### Audit, testing, and reproducible builds

```powershell
python scripts/release_audit.py --root .
python -m unittest discover -s tests
python scripts/build_release.py --output-dir dist --source-date-epoch 1704067200
```

The artifact loader validates paths, file types, byte lengths, SHA-256 values, strict models, identities, parameters, conclusions, and deterministic report rendering. An in-package Manifest detects accidental corruption; only an independently stored Manifest SHA-256 can detect coordinated replacement. A hash is not a digital signature or proof of authorship.

GitHub Actions runs security auditing, offline tests, two reproducibility builds, and clean wheel/sdist installation checks on Windows and Linux with Python 3.11 and 3.13. The project is licensed under the [MIT License](LICENSE).

---


## 中文

`market-validator` 是一个强调安全边界、可审计性和可复现性的金融市场假设验证项目。它把“提出研究计划”和“执行确定性计算”严格分开：AI 只能提出待审阅的计划和解释已经验证的结果，数据事实、转换、统计量、结论判定与产物校验均来自确定性 Python 代码。

项目目前处于 Alpha 阶段，能够生成通用统计假设草案，但端到端分析仍只支持一个预先固定的 WTI 价格变化波动比较。它不是通用量化平台、投资顾问或交易系统。

## 项目目标

- 将自然语言市场问题逐步收敛为严格、可序列化的研究协议。
- 在正式分析前要求用户确认完整数学定义和 proposal SHA-256。
- 保存数据来源、参数、时间语义、代码版本、质量问题和运行产物。
- 对 Bundle、分析结果、报告和 Manifest 执行严格回读与哈希验证。
- 让同一数据快照和同一计划得到确定、可审计的执行结果。

明确的非目标：

- 不执行真实交易或自动下单。
- 不把相关性、回顾性比较或预测结果描述为因果关系。
- 不允许 AI 直接计算统计量、确认计划、绑定数据或执行 workflow。
- 不静默填补缺失数据、替换研究定义或绕过严格模型。

## 当前能力

已实现：ResearchSpec、DataPlan（已确认、hash 绑定）、SourceSelection、
AnalysisPlan / AnalysisAuthorization 合同（仅规划与授权，未执行任何分析）、
授权确定性分析执行（Pearson / Spearman / OLS classic、HC1、Newey-West，
基于 session 的对齐，no-lookahead，不可变结果目录）、
确定性证据包与 grounded LLM 解释及 Markdown 验证报告（禁止编造数字、
禁止交易建议）、
基于 session 的对齐，no-lookahead，不可变结果目录）、
AcquisitionRequestPlan 1.2（capability/pre-sample 规划、精确 FRED 请求步骤）、
DataAccessAuthorization（single-use，无 paid/retry/fallback）、事务性
acquisition snapshot、DataReadinessAssessment 1.1 与 DataReadyManifest 1.1
（完整 session-evidence 绑定）、统一 JSON-only `data-lifecycle` CLI，以及
TEST-ONLY 离线 synthetic 示例（可达 Data Ready）。

正式授权的 FRED/local CSV acquisition lifecycle 已实现；通用 Provider
自动选择、多市场通用 calendar execution、通用统计分析仍未实现。

- Python 3.11+、`src` 布局、Pydantic v2 严格模型；Windows 通过条件依赖 `tzdata` 提供 IANA 时区数据库。
- `ResearchSpec`、数据需求、日历和 instrument registry 契约。
- 本地 CSV 离线检查，以及显式授权的 FRED HTTPS 数据获取。
- 新旧 DataBundle 严格加载、质量报告、来源和 SHA-256 审计。
- 缺失值感知的简单收益率、对数收益率和带符号绝对价格变化。
- 固定 WTI `price_change_volatility` 分析、Bootstrap、稳健性检查。
- 确定性分析 artifact、Manifest、严格回读和统一 workflow。
- 可选、proposal-only 的 DeepSeek AI Provider。
- 独立、严格的通用 `ResearchHypothesisProposal`：表达 association/predictive 问题、变量角色、变换、时间方向、方法、目标参数、确定性 H0/H1、歧义和不支持请求。
- 稳定 ambiguity ID、白名单结构化澄清、绑定规范化 Proposal SHA-256 的显式确认，以及生成现有 `ResearchSpec` 和 provenance sidecar 的确定性编译器。

## 架构与信任边界

```text
用户市场问题
→ 可选 AI Provider 生成不可信 proposal
→ 现有严格解析器验证并规范化
→ Python 只应用用户给出的白名单结构化澄清
→ 用户审阅并显式确认精确的 proposal SHA-256
→ Python 确定性生成并严格验证 ResearchSpec
→ 后续数据或 workflow 阶段必须另行授权
→ 转换、分析、原子发布、Manifest 锚定和严格回读
→ AI 只能解释已验证结果
→ 用户作最终判断
```

### AI 边界

AI Provider 只接收原始问题、固定 system prompt、proposal JSON Schema 和当前能力说明。它不是数据源、统计引擎、确认者、编译器或执行者；它不会获得 Bundle、路径、request ID、哈希、artifact 身份或凭据内容。

模型生成本身不确定。只有经过严格解析后的规范化 proposal 字节及其 SHA-256 可复现；生成内容只要变化，就必须重新审阅和确认。

通用 `ResearchHypothesisProposal` 现在支持结构化澄清、哈希绑定确认和确定性 ResearchSpec 编译，但不会绑定真实数据源、下载数据或执行分析。固定 WTI `MarketValidationPlanProposal` 仍是独立契约，也是当前唯一已实现的端到端分析 workflow。

### 网络授权

所有默认命令和测试均离线。FRED live fetch 需要显式 `--live`，AI proposal 请求需要显式 `--allow-network`。`--allow-network` 只许可一次 proposal Provider 网络请求，不代表用户确认 proposal，也不授权 `compile-plan`、`run`、数据获取或 artifact 写入。

Provider 与模型名称、可用性和接口能力可能变化。运行真实请求前，应重新核对相应 Provider 的官方资料。当前 DeepSeek 适配器只允许受控的官方 HTTPS 地址，无隐式重试，并设置请求超时、输出 token 上限和响应字节上限。

## 快速开始

安装开发版本：

```powershell
python -m pip install -e .
```

两个入口行为一致：

```powershell
market-validator doctor
python -m market_validator doctor
```

常用离线检查：

```powershell
python -m market_validator backends
python -m market_validator spec schema
python -m market_validator spec validate examples/research_specs/oil_to_a_share_energy.json
python -m market_validator hypothesis schema
python -m market_validator hypothesis validate-proposal examples/hypothesis_proposals/oil_to_a_share_energy.proposal.json
python -m market_validator hypothesis clarification-schema
python -m market_validator hypothesis validate-clarifications --proposal examples/hypothesis_proposals/oil_to_a_share_energy.proposal.json --answers examples/hypothesis_proposals/oil_to_a_share_energy.clarifications.json
python -m market_validator data registry validate
python -m market_validator data fred fetch examples/data_requirements/fred_wti_spot_initial.json
python -m market_validator validate-proposal examples/ai_planning/wti_price_change_volatility.proposal.json
python -m unittest discover -s tests
```

不带 `--live` 的 FRED fetch 是 dry-run，不联网。

## 可选 AI proposal

API Key 只从 `DEEPSEEK_API_KEY` 环境变量读取，不写入参数、prompt、异常、日志、路径或 proposal。下面是供用户在验收后自行授权的命令；默认测试不会执行它：

通用统计假设草案：

```powershell
python -m market_validator propose-hypothesis `
  --question-file examples/hypothesis_proposals/oil_to_a_share_energy.question.txt `
  --provider deepseek_api `
  --model <明确且已核验的模型名> `
  --output <new-hypothesis-proposal.json> `
  --allow-network
```

该命令只生成不可信草案。后续离线命令可应用用户明确给出的结构化澄清、创建绑定规范化 SHA-256 的确认记录，并在所有必填映射都明确时确定性生成 ResearchSpec。任何一步都不会获取数据或执行统计。

当编译返回 `unresolved_requirements` 时，可离线提供显式 provider-neutral 补全（`hypothesis apply-completion`）：补全只能设置 `research_spec_inputs`，生成新 Proposal 版本并使旧确认失效，必须重新显式确认后才能编译 ResearchSpec。随后可从 ResearchSpec + 显式 registry snapshot 确定性生成并确认 provider-neutral `DataPlan`（`data-plan generate` / `data-plan confirm` / `data-plan validate-confirmation`）；DataPlan 确认绑定 ResearchSpec/DataPlan/双 registry 的 canonical SHA-256，不授权任何数据获取、Provider 请求、分析或回测。

固定 WTI workflow plan proposal：

```powershell
python -m market_validator propose-plan `
  --question-file examples/ai_planning/wti_question.txt `
  --provider deepseek_api `
  --model deepseek-v4-flash `
  --output <new-proposal.json> `
  --allow-network
```

两条命令都只原子写入严格解析后的 proposal，不会自动确认、编译、运行或生成 artifact。DeepSeek 的结构化生成必须显式授权联网且最多发送一次无重试请求；Codex Plus 生成仍未实现。

## 用户确认、编译和运行

通用 Hypothesis Proposal 的澄清、确认与 ResearchSpec 生成全部离线：

```powershell
python -m market_validator hypothesis validate-clarifications `
  --proposal <draft-proposal.json> `
  --answers <clarification-answers.json>
python -m market_validator hypothesis apply-clarifications `
  --proposal <draft-proposal.json> `
  --answers <clarification-answers.json> `
  --output <clarified-proposal.json>
python -m market_validator hypothesis confirm-proposal `
  --proposal <clarified-proposal.json> `
  --output <confirmation.json>
python -m market_validator hypothesis compile-research-spec `
  --proposal <clarified-proposal.json> `
  --confirmation <confirmation.json> `
  --output <research-spec.json>
```

Ready 不等于 Confirmed，Confirmed 不等于已执行。修改 Proposal 任一有效字段会改变规范化哈希并使旧确认失效。编译成功只生成 ResearchSpec 与 provenance sidecar；缺少既有 ResearchSpec 必填映射时会返回结构化 unresolved requirements，不会猜测或写出伪造规格。

下面是独立的固定 WTI WorkflowPlan 流程。

验证 proposal：

```powershell
python -m market_validator validate-proposal <proposal.json>
```

用户确认必须绑定 proposal 的精确规范化 SHA-256。随后显式编译计划：

```powershell
python -m market_validator compile-plan `
  --proposal <proposal.json> `
  --confirmation <confirmation.json> `
  --bundle <bundle.json> `
  --output <workflow-plan.json>
```

编译只绑定 Bundle request ID 和实际文件哈希，不执行分析。运行仍是单独命令：

```powershell
python -m market_validator run `
  --plan <workflow-plan.json> `
  --bundle <bundle.json> `
  --artifact-root <artifact-directory>
```

## WTI 黄金案例

固定离线黄金案例使用以下身份：

- Request ID：`fred-dcoilwtico-20260804T085757136534Z-58aa38ed3b12`
- Bundle SHA-256：`94e89062727b475168dc5605bef4b523e6046786f83474bca0334652a7fc3c7b`
- Artifact ID：`price-change-volatility-0796a66788e5cfd71dff6a3222dd5cab`
- Result SHA-256：`0796a66788e5cfd71dff6a3222dd5cab6062ab73d7b5dd9e1ccfbe9bbdc12435`
- Report SHA-256：`c6e0d4a7d2c8e57c9da8985f904de8bcec6d14f1be35d8893ea296aeb277967b`
- Manifest SHA-256：`5be9387e8425838931618c585805a19e046a4b4ba9da5950373d83385e960a2e`
- 固定结论：`supported`

该结论仅表示预设窗口下的回顾性 per-observation 价格变化波动比较获得支持。它不是因果检验，不证明预测能力、交易盈利能力或严格等间隔的年化日波动率。

## 审计与完整性

分析产物目录包含严格 `result.json`、可读 `report.md` 和 `manifest.json`。加载器验证文件名、普通文件类型、字节数、SHA-256、模型契约、artifact ID、来源身份、参数、结论及报告确定性渲染结果。

包内 Manifest 可发现意外损坏；只有独立保存的 Manifest SHA-256 才能检测 result、report 和 Manifest 被协调替换的情况。它不是数字签名或作者身份证明。

仓库发布前安全审计：

```powershell
python scripts/release_audit.py --root .
```

若目录存在 Git 元数据，脚本扫描 `git ls-files`；否则扫描发布候选文件并明确报告范围差异。脚本只输出路径、行号和规则，不输出疑似凭据值。

## 测试与构建

完整测试完全使用 fake provider、mock transport 或固定本地响应：

```powershell
python -m unittest discover -s tests
```

可复现发布构建使用显式的 `SOURCE_DATE_EPOCH`。脚本调用标准 `build` 前端，并只规范化 sdist 中由 setuptools 写入的 tar/gzip 时间与所有者元数据；包内文件内容和项目元数据不变：

```powershell
python scripts/build_release.py --output-dir dist --source-date-epoch 1704067200
```

发布验收会在两个空目录独立构建并比较 wheel/sdist 的实际 SHA-256，然后把 wheel 与 sdist 分别安装到全新虚拟环境，检查 `market-validator` 与 `python -m market_validator` 的行为一致。

GitHub Actions 在 Windows 和 Linux、Python 3.11 和 3.13 上执行上述流程。CI 仅在依赖引导步骤从 PyPI 下载声明的构建/运行依赖到本地 wheelhouse；随后的项目安装、测试、两次构建和分发包安装均设置 `PIP_NO_INDEX=1`，并清空 Provider 凭据变量。默认测试不会联网，也不会读取真实 `DEEPSEEK_API_KEY`。

## 文档

- [架构](docs/architecture.md)
- [ResearchSpec](docs/research_spec.md)
- [数据契约](docs/data_contracts.md)
- [FRED Provider](docs/fred_provider.md)
- [凭据](docs/credentials.md)
- [Provider 安全](docs/provider_security.md)
- [AI planning](docs/ai_planning.md)
- [统计假设草案](docs/hypothesis_drafting.md)
- [Workflow](docs/workflow.md)
- [CLI](docs/cli.md)
- [分析产物](docs/analysis_artifacts.md)
- [贡献指南](CONTRIBUTING.md)
- [安全政策](SECURITY.md)
- [变更记录](CHANGELOG.md)

## 当前限制与路线图

尚未实现：

- 自动数据源选择或联网市场数据 workflow；
- 通用跨市场交易日历对齐执行；
- 通用相关性、回归、因果推断、回测或报告执行；
- 自动确认、自动编译、自动执行和真实交易。

后续工作应继续遵守“AI 提案、用户确认、确定性执行、严格回读”的顺序，不得扩张 AI 权限。

## 发布信息

- GitHub 仓库：`szx995-collab/market-hypothesis-testing`
- 许可证：[MIT License](LICENSE)
- README：中英双语

发布操作必须显式执行；项目本身不会自动创建远程仓库、GitHub release 或推送代码。


## Research agent（研究会话）

把自然语言问题沿既有生命周期推进到最终 Markdown 报告：

```powershell
market-validator research start --question-file hypothesis.txt `
  --workspace .\research-001 --provider deepseek_api --model <MODEL> `
  --allow-llm-network
market-validator research resume --session .\research-001
```

报告输出在 `final/validation-report.md`。Agent 不会自动确认、授权、
联网或消费 single-use authorization；每一步需要用户决定时都会停下来
说明原因。
