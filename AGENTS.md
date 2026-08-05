# Market Validator 项目契约

本文件适用于整个仓库。后续开发、评审和自动化修改都必须持续遵守以下原则。

## 职责边界

- LLM 负责理解自然语言、规划研究和解释结果；LLM 的输出是待验证的研究建议，不是数值计算结果。
- 确定性 Python 工具负责数据处理、计算和统计检验；关键计算必须可重复运行并可测试。
- 核心研究代码只能依赖供应商无关的 `StructuredGenerationBackend`，不得直接依赖 DeepSeek、Codex 或未来的具体模型后端。
- `deepseek_api` 是未来使用环境变量配置的 API 路径；`codex_plus` 是未来复用本机 `codex login` 状态、通过 `codex exec --output-schema` 执行的 CLI 路径，不是普通 HTTP API，也不使用 OpenAI API Key。
- 所有模型后端最终必须返回同一种供应商无关的结构化结果。无论使用哪个后端，模型都不得直接完成数据处理、数值计算或统计检验。
- 在正式分析开始前，必须向用户清楚展示数学定义、变量、样本、时间范围和检验方法，并取得用户确认。
- 相关性不能被描述为因果关系。除非未来另有经过审查的因果识别设计，否则报告只能陈述关联。
- 系统不得执行真实交易，也不得提供自动下单能力。

## 可复现性与质量

- 每次运行必须保存数据来源、参数、代码版本和运行结果；还应保存足以审计运行过程的输入、确认记录和错误信息。
- 每个开发阶段都必须有测试和明确验收标准。未通过验收的阶段不得被标记为完成。
- 不得静默替换数据、方法或用户已确认的定义。发生缺失、歧义或不一致时，应明确失败或中止并保留诊断信息。
- `instrument_id` 必须保持供应商无关；provider symbol 只能保存在独立映射层，不得写入 ResearchSpec 或 DataRequirement。
- 不同供应商的数据不得静默混合。每个数据包必须保存单一来源元数据和原始内容 SHA-256。
- 数据质量问题不得通过静默排序、去重、填值或改写元数据来掩盖。
- 外部提供商和框架应通过后续架构决策选择；在作出决定前，不把具体数据提供商、LLM 提供商或 Agent 框架写入核心契约。

## 当前阶段边界

当前完成 `scaffold`、结构化模型后端接口、ResearchSpec 1.0、数据需求规划、离线 CSV 数据契约、首个显式调用的网络数据适配器 `FredProvider`，以及两种彼此独立的不可信 AI 草案：固定 WTI workflow 使用的 `MarketValidationPlanProposal`，和把通用自然语言问题结构化为变量、时间关系、方法、检验方向与待澄清项的 `ResearchHypothesisProposal`。通用假设草案现支持确定性的白名单结构化澄清、绑定规范化 Proposal SHA-256 的显式用户确认，以及在所有现有 ResearchSpec 必填映射均明确时生成 ResearchSpec 和 provenance sidecar；缺少映射时必须返回结构化 unresolved requirements，不得猜测。Ready 不等于 Confirmed，Confirmed 不等于已执行，生成 ResearchSpec 不授权数据下载或分析。AI provider 必须显式授权联网，只能接收问题、固定提示、Schema 和能力说明；不得确认、绑定 Bundle、编译或执行 workflow。FRED 只允许 HTTPS 官方主机、经核验映射和显式 `allow_network=True`/`--live`；默认状态检查与 dry-run 不联网。每次成功 live fetch 必须保存原始响应、哈希、标准化数据包和无密钥 manifest。通用数据获取、跨市场对齐计算、通用统计执行、回测和因果推断仍不可用，不得描述为已有能力。

FRED 的修订策略必须显式选择；适配器不得替调用者静默选择 `latest_available` 或 `initial_release`。自动化任务从 `FRED_API_KEY` 读取 Key；只有调用者显式启用 interactive 时才可通过通用 CredentialResolver 安全输入。Key 只在当前进程内存中使用，不得出现在日志、异常、URL 元数据、文件名或持久化产物中。Tushare 因官方传输安全契约尚未确认而暂缓，不得猜测 HTTPS 端点或增加 Token 配置。
