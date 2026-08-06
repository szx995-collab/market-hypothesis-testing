# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 的结构。

## [Unreleased]

## [0.3.0] - 2026-08-06

### Added

- 完整 SourceSelection 生命周期：确定性 `SourceSelectionDecision`、严格验证、
  显式 `SourceSelectionConfirmation`（固定确认语句）、provenance 与原子只创建
  持久化；未解决需求返回结构化 unresolved requirement，绝不猜测。
- Capability/pre-sample 请求规划：Provider capability snapshot（来自真实
  Provider 能力事实）、acquisition window（schema 1.2，含 FRED
  observation_start=acquisition_start）、显式 pre-sample 解析方法与未解决原因。
- `DataAccessAuthorization`：显式 network/local-file/credential 授权布尔、
  paid/retry/fallback 恒为 false、single-use 固定、精确 request-id 绑定；
  `DataAccessAuthorizationReceipt` 先持久化后执行，消费后不可复用。
- 精确 FRED 请求契约：`PublicRequestStep`（metadata + observations 两步）、
  HTTP 参数只含 public fields、分页策略显式、旧 schema 严格 fail closed。
- Provider execution adapters：FRED（capture 路径零重试）与 local CSV
  （绝对路径/盘符/URI 规范化、symlink 组件拒绝、单次读取、stat 前后校验、
  32MB 上限）；执行失败后授权仍 consumed，不允许 retry/fallback。
- 事务性 acquisition snapshot：staging → fsync → 原子 rename、manifest 与
  outcome 严格校验、全量 hash 绑定、verify 交叉校验、immutable 持久化。
- `DataReadinessAssessment`/`DataReadyManifest` schema 1.1：完整 expected/
  observed sample & pre-sample session-set SHA-256、session schedule snapshot
  哈希绑定、assessment_id/data_ready_id 派生自完整 canonical core；
  旧 1.0 artifact 严格拒绝。
- `ExplicitSessionScheduleSnapshot`：精确 session 日期集合、coverage 语义、
  固定验证声明、canonical 哈希与 create-only 持久化；adapter 只返回明确
  列出的日期，绝不从名称/市场/星期推导。
- 统一 JSON-only `data-lifecycle` CLI：schema/validate、source-selection、
  acquisition、authorization、execution、session-schedule、readiness
  assess/finalize/verify；稳定 exit codes（0/1/2/3/4）、无自动确认/授权/
  选择/执行。
- TEST-ONLY synthetic 离线示例 `examples/v0.3_data_ready/`（DataPlan
  Confirmed → Data Ready，显式 local CSV + 显式 session schedule）与
  `scripts/verify_v0_3_offline_example.py` 验收脚本。

### Changed

- `AcquisitionRequestPlan` schema 1.0/1.1 → 1.2（acquisition window、steps
  进入 canonical hash、HTTP 参数净化）。
- `DataReadinessAssessment`/`DataReadyManifest` 1.0 → 1.1（完整 session
  evidence 绑定）；旧 artifacts 必须重新生成。
- 正式文档只推荐 `data-lifecycle execution run`；`data fred fetch --live`
  明确标注 legacy provider diagnostic，输出 `formal_lifecycle=false`。

### Security

- 正式生命周期不自动选择 Provider、不自动确认、不自动授权、不自动执行；
  无 retry/fallback/paid 开关；credential 仅非交互解析且绝不进入 artifact。
- schedule snapshot 不接受 Python import path、不加载任意代码、不输出
  secret；URI 拒绝 credential；持久化拒绝 traversal/symlink/覆盖。
- 统一 CLI 拒绝重复 JSON key、未知字段、非有限数、输出冲突，内部异常不
  输出 traceback，secret sentinel 永不泄漏。
- 信任边界明确：Data Ready ≠ Analysis Authorized；schedule snapshot
  确认不是数字签名；legacy FRED live fetch ≠ 正式生命周期。

## [0.2.0] - 2026-08-05
### Added

- 独立、严格的 `ResearchHypothesisProposal`，用于从通用自然语言问题保存变量角色、概念变换、时间方向、样本/对齐草案、目标参数、H0/H1、歧义和不支持请求。
- 供应商无关的 `HypothesisProposalService`、确定性 JSON/Schema/SHA-256、原子只创建持久化，以及离线 `hypothesis schema` / `hypothesis validate-proposal` CLI。
- 显式联网、单请求且无重试的 `propose-hypothesis` DeepSeek 路径；Codex Plus 仍未实现。
- 稳定 ambiguity ID、严格 `ClarificationAnswers`、白名单确定性应用及完整 Proposal 再验证。
- 绑定规范化 Proposal SHA-256 的显式确认记录，以及只映射已确认字段的确定性 ResearchSpec 编译器。
- ResearchSpec canonical serialization、确认 provenance sidecar 和离线澄清/确认/编译 CLI。
- 锁定契约测试：Ready 不等于必然可编译（`follows_outcome` 等关系在编译期返回 unresolved 而非猜测）、Proposal hash 对字段显式性敏感、asset type/对齐冲突分支、controls 组合冲突确定性拒绝。
- `ResearchSpecCompletionAnswers`：显式补全 `research_spec_inputs`，生成新 Proposal 版本、强制旧确认失效，且只能设置 `research_spec_inputs`（不允许修改 claim/变量/样本/对齐/method/H0/H1 等）。
- 确定性 `DataPlan` 生命周期：严格序列化与 canonical SHA-256、registry canonical snapshot 哈希绑定、readiness 检查（verified identity + verified provider mapping）、`DataPlanConfirmation`（绑定 ResearchSpec/DataPlan/双 registry 哈希）、provenance sidecar 与原子只创建持久化。
- 离线 CLI：`hypothesis completion-schema` / `validate-completion` / `apply-completion`，以及 `data-plan generate` / `validate` / `confirmation-schema` / `confirm` / `validate-confirmation`。
- 端到端示例：油/A 股 completion 链（ResearchSpec 成功生成、DataPlan 保持 unresolved），以及明确标注 TEST-ONLY 的 synthetic 完整链路 fixture。

### Security

- 拒绝重复 JSON key、非标准浮点、Markdown 包裹、未知字段、路径/命令/凭据/供应商身份/Bundle 身份进入生成字段，并在模型调用前检查 Key、网络授权和输出冲突。
- DeepSeek 假设草案与固定 WTI plan provider 复用同一套官方 HTTPS 主机、超时、响应大小和清洗传输边界。
- 澄清、确认和 ResearchSpec 输出拒绝重复 JSON key、非有限数、未知字段、符号链接、静默覆盖与哈希不匹配；缺少必填映射时不创建伪造 ResearchSpec。
- 补全与 DataPlan 确认同样严格拒绝未知字段、重复 JSON key、非有限数、Markdown 围栏、JSON 外文本、哈希不匹配与覆盖；DataPlan 确认不触发网络、下载、Provider、分析、回测或 workflow。
- 明确边界：DataPlan Ready ≠ DataPlan Confirmed；DataPlan Confirmed 不授权任何数据获取；verified mapping 不构成自动 provider 选择。
- 文档明确：确认是绑定一致性记录而非数字签名或身份认证；重复确认因新审计时间与不可变确认文件冲突时不会静默覆盖；Proposal 身份基于当前 canonical 表示，显式默认值与省略可能产生不同 hash。

## [0.1.0] - 2026-08-05

### Added

- 严格 `ResearchSpec`、数据需求、Bundle、质量和来源模型。
- 显式授权的 FRED Provider、凭据输入边界和不可覆盖数据快照。
- 缺失值感知的价格收益率与带符号绝对价格变化转换。
- 固定 WTI 价格变化波动分析、Bootstrap 和稳健性检查。
- 确定性分析 artifact、Manifest、统一 workflow 和离线 CLI。
- AI proposal、哈希确认、Bundle 绑定和 WorkflowPlan 编译契约。
- Proposal-only DeepSeek 适配器及显式 `propose-plan --allow-network` 命令。
- 离线发布审计、可复现 wheel/sdist 构建与独立安装验收脚本，以及 Windows/Linux GitHub Actions。

### Security

- Provider 官方 HTTPS 地址限制、请求超时、响应大小限制和无隐式付费重试。
- API Key、Authorization header、异常、文件路径和持久化内容的 sentinel 防泄漏测试。
- 原子只创建写入、内容冲突保护、严格回读和外部 Manifest 哈希锚定。

### Changed

- 完善包元数据、源码发行清单、`.gitignore` 和发布文档。
- 为 Windows 声明 IANA 时区数据库条件依赖，确保全新安装后的 `zoneinfo` 验证可用。
- 排除测试生成的 `.runtime_*` 目录，并在分发包验收时拒绝此类运行时产物。
[Unreleased]: https://github.com/szx995-collab/market-hypothesis-testing/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/szx995-collab/market-hypothesis-testing/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/szx995-collab/market-hypothesis-testing/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/szx995-collab/market-hypothesis-testing/releases/tag/v0.1.0
