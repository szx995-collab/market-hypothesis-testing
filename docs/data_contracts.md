# 数据规划与离线数据契约

## 从研究协议到数据包

当前数据层的流程是：

```text
ResearchSpec
  -> DataPlan
  -> DataRequirement[]
  -> 显式选择的 DataProvider
  -> Observation[] + DataSourceMetadata + DataQualityReport
  -> load_data_bundle()
  -> ReturnSeries / AbsolutePriceChangeSeries
```

除需要显式授权的 FRED 官方 HTTPS 适配器外，这一阶段不会连接其他真实金融数据 API。当前只实现严格的价格简单收益率、对数收益率和绝对价格变化转换；仍不会执行复权、换月、跨市场对齐或统计分析。FRED 默认命令是离线 dry-run。

## DataPlan 与 DataRequirement

`DataPlan` 是一份确定的数据需求清单。相同 ResearchSpec 和相同注册表总会得到相同 `plan_id`；ID 使用规范化输入的 SHA-256 摘要，不包含当前时间。

每个 outcome、predictor 和 control 各产生一个 `DataRequirement`。Requirement 保留变量的资产类型、原始字段、转换说明、日期、频率、复权、期货换月、代理关系、滞后和信息截止点，但不选择供应商。

先规划再调用供应商能让系统在读取数据前发现身份冲突、缺少映射和所需历史窗口。规划器不读取或执行 `model.formula`；该字段目前只供人阅读。

### DataPlan 生命周期与确认

`DataPlan` 生命周期由 `data_plan_review` 提供：`generate` 严格重验证 ResearchSpec 与双 registry snapshot 后调用 `plan_data_requirements`，`validate` 做 strict JSON/canonical SHA-256 回读，`confirm` 只接受完全 ready 的 DataPlan。DataPlan identity 绑定 ResearchSpec、DataPlan 与 InstrumentRegistry/CalendarRegistry 各自 canonical snapshot 的完整 SHA-256；`plan_id` 仍是短摘要，完整绑定在 `GeneratedDataPlan` 与 `DataPlanConfirmation` 中。

一个 requirement 只有在以下条件全部满足时才可确认：instrument identity 已注册且 `identity_status=verified`、registry 元数据与 ResearchSpec 一致、calendar 存在、至少存在一个 `verified=True` 的 provider mapping，且 field/frequency/sample/transformation/lag/revision 信息完整、cross-market alignment 完整、无 unresolved instrument 与阻塞性 warning。缺失或未验证的映射保持 `UNRESOLVED`；多个 verified mapping 保留给后续来源选择，绝不自动选择。

`DataPlanConfirmation` 绑定四个 canonical SHA-256（research_spec、data_plan、instrument_registry、calendar_registry）与固定确认声明，只表示“该 provider-neutral DataPlan 可供后续来源选择审查”，不授权网络、下载、Provider 请求、付费、分析、回测、交易或下单。任一有效内容改变（spec、plan、任一 registry snapshot）都会使旧确认失效。输出使用原子只创建持久化，provenance sidecar 记录全部相关哈希与审计时间（审计时间不进入 DataPlan 确定性内容）。

### required_pre_sample_periods

这个字段表示在正式样本开始前，计算转换和滞后至少需要多少个原始目标频率观测：

| transformation | 转换本身需要的额外周期 |
| --- | ---: |
| `level` | 0 |
| `simple_return` | 1 |
| `log_return` | 1 |
| `pct_change` | 1 |
| `difference` | 1 |
| `rolling_mean(window=N)` | `N-1` |
| `zscore(window=N)` | `N-1` |

最终结果再加 `lag_periods`。因此 `log_return + lag_periods=2` 得到 `required_pre_sample_periods=3`。`availability_lag_periods` 表示信息实际发布或可取得的延迟，不属于价格计算窗口，不能悄悄加进这个数值。

## instrument_id 与 provider symbol

`instrument_id` 是框架内部、供应商无关的规范化身份，例如概念性的 `jp.nikkei_225.index`。provider symbol 是某个供应商、数据集和市场使用的外部代码，只能放在 `ProviderSymbolMapping` 中。

注册表对原有示例资产不编造 provider symbol；这些 requirements 仍会标为 `unresolved`。另有四个通过官方系列页面记录核验来源的 FRED 映射，仅供 FredProvider 显式解析。规划器不会自动选供应商，也不会自动回退。

## 数据修订策略

`VariableSpec.revision_policy` 会原样传递到 `DataRequirement`。旧协议默认 `not_applicable` 以保持兼容，但 FredProvider 要求调用者显式选择：

- `latest_available`：抓取当前最新修订值；`available_time` 是本次实际抓取时刻，并产生 `latest_revision_hindsight_risk`，不能当作历史时点已知数据。
- `initial_release`：使用 FRED `output_type=4`；以每条记录的 `realtime_start` 作为首次可得日期。由于只有日期精度，保守使用当天 `23:59:59.999999 UTC`，同时标记 `availability_precision=date`。
- `as_of_date`：模型可表达，但 FredProvider 本轮明确拒绝，不能静默退化。
- `not_applicable`：保留给不涉及修订的来源；FredProvider 不接受。

`Observation.observation_precision` 描述观测时点精度，FRED 日值为 `date`；`vintage_date` 与 `availability_assumption` 使发布日期精度和保守假设可审计。详见 [`fred_provider.md`](fred_provider.md)。

不同供应商的数据不能静默混合。一个 `DataBundle` 只对应一个 requirement 和一个 `DataSourceMetadata`；未来若要合并来源，必须有显式、可审计的上层决策。

## CalendarRegistry 当前能做什么

日历注册表目前只保存内部 ID、显示名称、IANA 时区、日历类型和未来适配器位置。已登记中国 A 股、美国、日本、韩国股票市场、24/5 外汇、国际能源期货和宏观发布时间序列。

当前日历组件不能：

- 判断某一天是否开市；
- 计算开盘、收盘或交易时段；
- 处理节假日、半日市、夏令时或临时休市；
- 判断“缺失交易日”。

请求这些运算会得到明确的 `CalendarOperationNotImplementedError`，不会返回猜测结果。以后由独立日历适配器实现。

## 原始数据、标准化数据与衍生变量

- 原始数据是用户指定 CSV 的原始字节。系统保存它的 SHA-256，且不会修改文件内容。
- 标准化数据是严格长表 `Observation`。时间可统一序列化为 UTC，但 `session_date` 保留原市场交易日期。
- 当前可从严格加载的正价格 `DataBundle` 计算 `simple_return` 和 `log_return`，也可从包含负值或零值的价格 Bundle 计算 `absolute_price_change`。其他差分、滚动均值和 z-score 仍只在 DataRequirement 中描述，不进行计算。

## 缺失值感知的收益率与价格变化转换

`transform_price_bundle()` 必须通过 `load_data_bundle()` 读取新旧 Bundle，不修改源 Bundle、Manifest 或 raw 文件。简单收益率定义为 `current_price / previous_price - 1`，对数收益率定义为 `log(current_price / previous_price)`；第一条价格没有前序端点，因此不产生收益率。

每条收益率保留两个端点的 observation time、available time、session date、源行号和实际日历间隔。收益率 `available_time` 始终取两个端点中较晚的时间，不能把结果标记为在任一价格尚不可得时已经可知。输出还保存源 request ID、Bundle SHA-256、变换公式和缺失处理参数。

缺口只由源质量报告中带行号的 `provider_missing_value` 判断。若两个有效价格之间存在这种记录，该价格对会产生 `provider_missing_value_gap` warning，并从标准收益率序列排除；不会前向填充、插值或跨缺口冒充普通单期收益率。星期五到星期一等自然日期跨度若没有 provider missing 记录，仍是合法相邻端点，并保留真实的 `interval_days`。

收益率输入价格必须有限且严格大于零；重复主键、时间倒序、非正价格或无法唯一映射的缺失源行会被明确拒绝。当前转换不执行统计分析、回测、跨市场对齐或结果持久化。

`transform_absolute_price_change()` 计算原单位下的带符号一阶差分 `current_price - previous_price`，不调用 `abs()`，因此下跌为负数。该转换允许负价格和零价格，并在每条记录中保留起止价格、原单位、request ID 和源 Bundle SHA-256；非有限值仍会被严格加载或转换验证拒绝。简单收益率和对数收益率继续要求所有输入价格严格大于零，不会因价格变化转换而放宽。

## 固定 WTI 价格变化波动比较

`compare_price_change_volatility()` 只执行一个预先固定的回顾性比较：比较 2020-03-01 至 2020-05-31 与 2021-01-01 至 2024-12-31 的 per-observation price-change volatility。它强制调用 `transform_absolute_price_change()`，按变化记录的结束 session date 分组，并仅使用 `available_time <= 2025-01-03T00:00:00Z` 的记录。

主统计量为两组样本标准差（`ddof=1`）之比；95%区间使用组内独立、长度5、10000次、种子20260804的 moving-block percentile bootstrap。主分析保留合法的周末跨度，稳健性检查分别只保留 `interval_days == 1`，以及排除以 2020-04-20 为任一端点的变化。该入口不是通用统计框架，不计算相关性、回归、显著性检验、年化波动率或回测，也不能解释为因果、预测能力或交易盈利证据。

## observation_time 与 available_time

`observation_time` 是观测所对应的市场或经济时点，例如收盘时刻。`available_time` 是该值实际可以被研究系统知道的最早时刻。后者不得早于前者。

防止未来数据泄漏时，关键问题不是“记录属于哪一天”，而是“在目标市场的信息截止点前是否已经可得”。未来对齐代码必须比较 `available_time` 与 ResearchSpec 的 `information_cutoff`；本阶段不执行该比较。

## CSV 标准格式

CSV 使用 UTF-8 严格长表，列必须恰好为：

```text
instrument_id
field
value
observation_time
available_time
session_date
timezone
currency
unit
```

`observation_time` 和 `available_time` 必须是带偏移量的 ISO 8601 时间；`session_date` 是 ISO 日期；`timezone` 是 IANA 名称。每行是一条观测，主键为 `(instrument_id, field, observation_time)`。

检查明确标记为 synthetic 的示例文件：

```powershell
python -m market_validator data csv inspect examples/data/synthetic_market_data.csv
```

文件名、文档和用途都将该文件标记为 synthetic；它不代表真实行情。

## 来源与 SHA-256

`DataSourceMetadata` 保存本地 URI、读取时刻、数据集名称、公开参数、许可说明及原始字节 SHA-256。哈希可证明后续运行使用的是完全相同的文件内容。

公开参数和 URI 禁止出现名称含 `api_key`、`token`、`secret`、`password` 或 `authorization` 的认证信息。CSV Provider 不需要认证，也不需要网络。

## 数据质量报告

质量状态为 `pass`、`warn` 或 `fail`；问题严重度为 `warning` 或 `error`。检查包括：

- 读取行数、成功解析观测数和 UTC 覆盖范围；
- 空值和非有限数值；
- 重复主键；
- 源文件时间顺序倒置；
- naive datetime；
- `available_time < observation_time`；
- 相对 requirement 的意外 instrument 或 field；
- 币种、单位或时区冲突；
- 同一序列内部的币种、单位或时区不一致。

检查不会自动排序、去重、填值、改写币种或单位，也不会隐藏无效行。存在 error 时 `DataBundle.status` 和质量报告状态均为 `fail`。由于交易日历计算尚未实现，本阶段不会声称发现了“缺失交易日”。

## 从示例 ResearchSpec 生成计划

```powershell
python -m market_validator data plan examples/research_specs/oil_to_a_share_energy.json
python -m market_validator data plan examples/research_specs/nikkei_to_us_market.json
```

计划成功但存在 unresolved 不表示真实数据可用，只表示数据需求已经可验证地表达出来。
