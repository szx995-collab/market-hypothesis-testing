# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 的结构。

## [Unreleased]

### Added

- 独立、严格的 `ResearchHypothesisProposal`，用于从通用自然语言问题保存变量角色、概念变换、时间方向、样本/对齐草案、目标参数、H0/H1、歧义和不支持请求。
- 供应商无关的 `HypothesisProposalService`、确定性 JSON/Schema/SHA-256、原子只创建持久化，以及离线 `hypothesis schema` / `hypothesis validate-proposal` CLI。
- 显式联网、单请求且无重试的 `propose-hypothesis` DeepSeek 路径；Codex Plus 仍未实现。

### Security

- 拒绝重复 JSON key、非标准浮点、Markdown 包裹、未知字段、路径/命令/凭据/供应商身份/Bundle 身份进入生成字段，并在模型调用前检查 Key、网络授权和输出冲突。
- DeepSeek 假设草案与固定 WTI plan provider 复用同一套官方 HTTPS 主机、超时、响应大小和清洗传输边界。

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
