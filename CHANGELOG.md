# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 的结构。

## [Unreleased]

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

[Unreleased]: https://github.com/szx995-collab/market-hypothesis-testing/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/szx995-collab/market-hypothesis-testing/releases/tag/v0.1.0
