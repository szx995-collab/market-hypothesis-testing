# 架构与研究工作流

## 设计边界

系统未来采用显式状态流转。LLM 只承担自然语言理解、研究规划草拟和结果解释；可验证的数据转换、计算与统计检验由确定性 Python 代码承担。任何 LLM 输出都不能替代原始数据、可执行计算或用户确认。

当前仓库实现 `scaffold`、结构化模型后端接口、ResearchSpec 1.0、数据需求规划、离线 CSV 数据契约、显式授权的 FRED 官方 HTTPS 适配器，以及独立的通用 `ResearchHypothesisProposal` 草案、结构化澄清、哈希绑定确认和确定性 ResearchSpec 编译层；通用数据绑定、跨市场对齐执行、分析及报告执行仍是后续开发契约，不代表现有功能。

## 通用假设草案边界

`ResearchHypothesisProposal` 属于 `hypothesis_draft`，与固定 WTI workflow 的 `MarketValidationPlanProposal` 相互独立。前者表达原始/规范化问题、association 或 predictive claim、唯一 outcome、predictors、controls、概念变换、时间关系、样本与对齐草案，以及 Pearson、Spearman、OLS 或 lead-lag regression 的目标参数和检验方向。它不能包含本地路径、供应商 symbol、Bundle/artifact 身份、哈希、凭据、命令或可执行代码。

LLM 只选择可审阅的结构和 `target_parameter`/`direction`。Python 根据 correlation 的总体 `rho` 或 regression 的总体 `beta` 确定性验证 H0/H1，拒绝重复/未知变量引用、非严格 JSON、未来信息方向不明却声称 ready，以及把因果、回测或交易请求偷偷降级为关联研究。缺失时间范围、变换、market session/calendar、information cutoff、代理、换月、controls、显著性或效应阈值时，草案必须保留 `ambiguities` 并设置 `ready_for_spec_review=false`。

该阶段的状态流为：

`自然语言问题` → `不可信 LLM Proposal` → `确定性严格解析与数学校验` → `结构化澄清` → `ready` → `用户显式确认 Proposal hash` → `确定性 ResearchSpec`

澄清只能修改白名单字段，并在每次应用后重新执行完整 Proposal 验证。Ready 不等于 Confirmed；Confirmed 不等于已执行。只有匹配规范化 Proposal SHA-256 的显式确认才能进入 ResearchSpec 编译。编译器只映射已确认且能明确映射的字段；若缺少 instrument/field、样本、对齐、复权/修订、多重检验、稳健性或限制等现有 ResearchSpec 必填项，则返回结构化 unresolved requirements，不使用虚构默认值。

`ready_for_spec_review=true` 只表示 Proposal 层已完成验证、可供用户审查和显式确认，不保证所有 ResearchSpec 必填映射已存在；编译仍可能返回 unresolved requirements。`follows_outcome` 等无法无损映射为现有 ResearchSpec 非负 lag 的时间关系会在编译期被拒绝，不会被反向解释、猜测或静默转换。Proposal 身份基于当前 canonical serialized representation：默认值字段是否被显式提供属于该表示的一部分，显式提供默认值与省略可能产生不同 hash；这是保守的防漂移规则，不是纯语义等价哈希。未来若改为纯语义 canonical，需要单独设计 schema/version 和确认迁移策略。确认记录是绑定一致性记录，不是数字签名或身份认证；同一 Proposal 再次确认会写入新的审计时间，与既有不可变确认文件发生输出冲突时不会静默覆盖。

ResearchSpec 编译是纯 Python 操作，不调用 LLM、网络、数据源或 shell。输出必须通过现有 ResearchSpec 验证与 canonical serialization，并使用 sidecar 保存 Proposal hash、confirmation hash 和审计时间。确认或生成 ResearchSpec 不会下载数据、编译 workflow 或执行统计。

当编译返回 unresolved requirements 时，`ResearchSpecCompletionAnswers` 只能填充 `research_spec_inputs` 并生成新 Proposal 版本：旧确认因 hash 变化自动失效，用户必须重新显式确认新 Proposal 后才能编译；补全记录本身不是确认，且不存在 `--yes`/`--force` 绕过路径。补全输入必须精确覆盖声明变量集合与 asset type，未知/重复/缺失/冲突映射一律拒绝。

`DataPlan` 生命周期（`generate` → `validate` → `confirm` → `validate-confirmation`）为纯 Python 离线确定性流程：DataPlan identity 绑定 ResearchSpec、DataPlan 与双 registry snapshot 的 canonical SHA-256；requirement 仅在 instrument 已注册且 identity 已验证、registry 元数据一致、calendar 存在且存在符合契约的 verified provider mapping 时才可确认。状态保持分离：Proposal Ready ≠ Proposal Confirmed ≠ ResearchSpec Generated ≠ DataPlan Ready ≠ DataPlan Confirmed ≠ Data Fetch Authorized ≠ Executed。`DataPlanConfirmation` 只供后续来源选择审查，不授权网络、下载、Provider 请求、付费、分析、回测、交易或下单；任一有效内容改变（spec/plan/任一 registry）都会使旧确认失效。verified mapping 的存在不代表自动 provider 选择或数据获取授权。

`SourceSelection` 生命周期（v0.3.0 第一阶段）在 `DataPlan Confirmed` 之后运行：`generate_source_selection` 把 `DataPlanConfirmation`、当前双 registry 快照与用户显式的 `SourceSelectionDecision` 组合为确定性 artifact。决策只以 `provider_id` + `provider_symbol` + `dataset_or_endpoint` 精确匹配 registry mapping；最终 selection 的 identity 与 verification metadata 一律从 registry 复制。单一候选也不自动选择，缺失决策生成 `source_not_selected` unresolved；选择不存在、unverified 或 market 冲突的 mapping 是结构化 hard failure。`SourceSelection` 绑定 ResearchSpec/DataPlan/DataPlanConfirmation/双 registry 六个 canonical hash，`selection_id` 由绑定与选择内容确定性派生；readiness 为空才可创建 `SourceSelectionConfirmation`，其含义仅为“供后续 acquisition-request planning 审查”，不授权网络、Provider、credential、下载、付费、retry、fallback、分析、回测、交易或下单。registry 内容或任一绑定变化都会使旧确认失效，仅条目顺序变化不影响 identity。

## ResearchSpec 领域边界

ResearchSpec 1.0 是 `spec_review` 状态将使用的供应商无关协议。目前只支持创建、JSON 序列化/反序列化、JSON Schema 生成和确定性验证，不负责自然语言解析、数据获取或分析。

协议通过结构保证单一 outcome、至少一个 predictor、结构化公式变量引用，以及显式的跨市场 `alignment`。`information_cutoff` 与有上界的历史观测连接规则共同定义目标交易时段之前哪些信息可用；禁止负滞后和无限向前填充。完整语义见 [`research_spec.md`](research_spec.md)。

## 数据规划与离线数据边界

`plan_data_requirements()` 确定性地把 ResearchSpec 中的 outcome、predictors 和 controls 转换为供应商无关的 DataRequirement。DataPlan 不执行自由文本公式、不选择供应商，也不修改 ResearchSpec。规范化 instrument 与 provider symbol 分属注册表的身份层和映射层；未配置映射时计划仍可生成，但必须标记 unresolved。

CalendarRegistry 当前只管理内部身份和 IANA 时区，不计算真实交易日或时段。未来节假日、半日市、夏令时和临时休市由独立适配器处理。

当前 DataProvider 包括显式本地路径的 CSVProvider，以及只访问 `https://api.stlouisfed.org` 的 FredProvider。FredProvider 必须先解析经核验的 provider mapping，再检查能力和显式修订策略；只有 `allow_network=True` 才能执行请求。它保留原始字节、标准化 Observation、运行质量检查，并在返回成功前持久化完整快照。一个 DataBundle 只包含一个来源；跨供应商组合与自动回退均未实现。完整契约见 [`data_contracts.md`](data_contracts.md) 与 [`fred_provider.md`](fred_provider.md)。

## 结构化模型后端边界

核心研究代码只能面向 `StructuredGenerationBackend` Protocol 编程。输入统一为 `StructuredGenerationRequest`，输出统一为 `StructuredGenerationResult`；后端特有认证、传输和执行细节不得渗入研究状态或统计代码。

| 后端 | 路径 | 配置与认证边界 | 未来结构化输出方式 |
| --- | --- | --- | --- |
| `deepseek_api` | DeepSeek API | API Key 只从 `DEEPSEEK_API_KEY` 环境变量读取；Base URL 和模型分别来自 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_MODEL`；调用必须显式允许联网 | 单次、无重试、受限 HTTPS 响应转换为统一的 `StructuredGenerationResult` |
| `codex_plus` | 本机 Codex CLI；不是普通 HTTP API | 复用 `codex login` 建立的登录，不使用 OpenAI API Key；项目不得读取、复制或解析 `~/.codex/auth.json` | 通过 `codex exec --output-schema` 获得结果，再转换为统一的 `StructuredGenerationResult` |

状态诊断不得调用模型。DeepSeek 诊断只检查相关环境变量是否存在，不验证或暴露密钥；Codex Plus 诊断只检查 CLI，并可执行有超时的 `codex login status`。无法可靠判断 Codex 登录属于 ChatGPT 还是 API Key 时，认证方式必须记为 `unknown`。

模型输出仅可用于自然语言理解、规划和解释。数据处理、数值计算、统计检验、稳健性检验和结果校验仍必须由确定性 Python 代码完成。`DeepSeekApiBackend.generate()` 只有在调用者显式启用网络、配置 Key 并提供模型时才允许一次受限请求；默认诊断和所有测试离线。`CodexPlusBackend.generate()` 仍明确未实现。

## 状态定义

| 状态 | 输入 | 主要职责 | 输出 | 责任方与确认点 |
| --- | --- | --- | --- | --- |
| `hypothesis_draft` | 用户的自然语言假设及可选背景 | 识别待研究的关系、对象、时间语境和明显歧义 | 结构化但尚未批准的假设草案，以及待澄清问题 | LLM 草拟；用户补充歧义信息，不执行计算 |
| `spec_review` | 假设草案、用户补充信息 | 明确定义变量、数学表达、样本范围、频率、时间区间、基准、检验方法和判定标准 | 有版本的研究规格与确认记录，或修改请求 | LLM 解释规格；确定性代码校验结构与必填项；**用户必须明确确认数学定义和研究范围** |
| `data_plan` | 已确认的研究规格 | 将每个变量映射为数据需求、字段、频率、时间覆盖、质量要求和来源选择标准 | 可审阅的数据计划，不绑定当前尚未选定的提供商 | LLM 可提出需求映射；Python 校验计划与规格的一致性；来源限制或成本变化需要用户再次确认 |
| `source_selection` | 已确认的数据计划与当前 registry 快照 | 将每个 requirement 显式绑定到一个 registry-backed、verified 的 Provider mapping | 确定性的来源选择与确认记录，不包含网络或获取授权 | **用户必须为每个 requirement 明确选择 mapping**；Python 只做精确匹配与哈希绑定，绝不自动选择 |
| `acquisition_request` | 已确认的来源选择、能力快照与 registry | 把选择转化为精确的公开获取请求，证明 pre-sample 解析并显式授权单次访问 | AcquisitionRequestPlan 与 DataAccessAuthorization（single-use），不执行任何数据访问 | 确定性 Python 负责能力校验与 pre-sample 证明；pre-sample 无法证明时保持 unresolved，不得授权 |
| `data_ready` | 已批准的数据计划、获取配置及来源凭据 | 获取、校验、规范化并冻结分析所需数据 | 带来源元数据、质量检查、版本或哈希的分析数据集 | 确定性 Python 负责；若数据缺失导致样本或定义变化，必须返回 `spec_review` 并由用户确认 |
| `analysis` | 已确认规格、冻结数据集、分析参数 | 按规格执行计算和统计检验 | 机器可读的主要估计、检验结果、诊断和运行日志 | 确定性 Python 负责计算；LLM 不生成或改写数值结果 |
| `robustness` | 主分析结果、规格中约定的稳健性方案、冻结数据 | 执行敏感性分析、替代定义或子样本检验，并记录偏离 | 机器可读的稳健性结果、比较表和异常说明 | 确定性 Python 负责计算；新增且可能改变结论的检验应由用户确认后执行 |
| `report` | 已验证的主分析与稳健性结果、运行元数据 | 组织证据、限制、可复现信息和面向用户的解释 | 研究报告及其引用的完整产物清单 | LLM 可起草叙述；Python 注入已验证的数值与溯源信息；不得把相关性写成因果关系 |

## 状态流转与用户确认

正常流转顺序为：

`hypothesis_draft` → `spec_review` → `data_plan` → `source_selection` → `acquisition_request` → `data_ready` → `analysis` → `robustness` → `report`

`spec_review` 是正式分析前的强制闸门。没有用户对数学定义、变量、样本和方法的明确确认，不得进入数据执行或分析。任何会实质改变已确认规格的情况——例如数据不可得、替代变量、时间范围变化或新增方法——都必须生成变更记录并返回用户确认。

## 失败与中止原则

- 输入不完整、规格含糊、确认缺失、数据质量不达标或计算前提不满足时，应失败关闭，不得猜测后继续。
- 不得静默替换数据来源、变量、样本、统计方法或阈值。
- 每个失败都应记录所处状态、错误类别、可理解的原因、已完成步骤和可恢复建议；已产生的有效产物应保留并标为不完整。
- 用户可以在任何状态中止。中止后不得继续获取数据或计算，并应保存中止状态与已有审计记录。
- 只有当前状态的验收标准和测试通过后才能进入下一状态；报告不得掩盖失败、缺失或不稳健的结果。

## 未来每次运行的可复现产物

每次运行应使用稳定的运行标识，并保存：

- 原始用户输入、澄清对话、假设草案、研究规格各版本及用户确认记录；
- 数据计划、来源标识、获取时间、许可或使用限制、原始数据引用或快照、内容哈希和质量检查结果；
- 规范化数据、字段定义、转换步骤及其可执行参数；
- 分析与稳健性配置，包括样本规则、随机种子（如适用）、模型或检验参数和判定阈值；
- Python 与操作系统环境信息、依赖锁定信息、代码版本或提交标识；
- 结构化计算结果、诊断、日志、警告、错误和状态流转记录；
- 最终报告，以及报告中的每个数字和结论到对应结果产物的引用关系。

产物格式、目录约定、保留策略以及具体提供商将在后续阶段单独决策。
