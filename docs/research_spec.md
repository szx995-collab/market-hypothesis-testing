# ResearchSpec 1.0 使用说明

## ResearchSpec 是什么

`ResearchSpec` 是一份机器可读、可版本化的研究协议。它把“我想研究什么”写成明确的变量、样本、时间对齐、模型假设和稳健性检查。它不是研究结果，也不下载数据或执行统计计算。

先确认协议再下载数据有两个好处：一是防止看到结果后再修改定义；二是能提前发现变量含义、时间范围、交易日和信息可得时间等歧义。正式分析前，用户仍必须确认数学定义和研究范围。

第一版覆盖 A 股和美股个股、ETF、主要指数与行业板块、美元汇率、国际油价及日经 225、KOSPI 等国际市场指数。当前只允许日频 `1d`。

## 三种变量角色

- `predictor`（预测变量）：用于解释或预测结果变量的输入，例如美国开盘前已经公布的日经 225 收盘收益率。
- `outcome`（结果变量）：研究要解释或预测的目标，例如随后美国常规交易时段的市场收益率。协议通过单个 `outcome` 字段从结构上保证恰好一个结果变量。
- `control`（控制变量）：为了比较更公平而纳入模型的其他已知变量。放在 `controls` 中的变量必须明确写成 `role: "control"`。

每个变量包含一个供应商无关的 `InstrumentSpec`。`instrument_id` 是框架内部标识，不是 Tushare、FRED 或其他供应商代码。

## association、predictive 与 causal

- `association` 表示变量共同变化，但不说明时间上的预测方向。
- `predictive` 表示预测变量在结果时段之前可知，并检验它是否对之后的结果有预测关系。
- `causal` 表示因果效应，需要额外的识别设计。ResearchSpec 1.0 不允许 `causal`，因为相关或领先滞后关系本身不能证明因果。

## 为什么跨市场不能按日期直接连接

“2024-01-02”在东京、上海和纽约并不代表同一段可交易信息。市场收盘时间、节假日和夏令时都不同。直接按日期连接可能把目标时段结束后才知道的信息放进预测变量，造成未来数据泄漏。

跨市场或跨时区协议必须提供 `alignment`：

- `target_market`、`target_timezone`、`target_calendar` 和 `target_session` 定义结果变量的目标交易时段；
- `join_policy` 只能选择截止点之前最后一个已完成观测、目标市场前一个已完成交易日，或严格匹配；
- `max_staleness_days` 给任何历史观测复用设置有限上界；
- `missing_data_policy` 不提供无限向前填充选项。

## information_cutoff 如何防止未来数据泄漏

`information_cutoff` 是目标交易时段允许使用信息的最晚时点。它是结构化对象，而不是自由文本：

- `before_target_open`：只允许目标市场开盘前已知的信息；
- `before_target_close`：只允许目标市场收盘前已知的信息；
- `specified_local_time`：必须同时给出 ISO 8601 本地时间和有效 IANA 时区。

对每个预测变量，未来的数据准备代码都必须先判断观测是否已在截止点前完成，再应用 `join_policy`。本阶段只验证协议，不执行该对齐。

## lag_periods 的准确语义

`lag_periods=N` 表示：该变量必须至少在结果变量对应目标交易时段之前 **N 个目标交易周期**可知。

`N=0` 仍要求变量在当前目标时段的 `information_cutoff` 前可知；它不表示可以使用目标时段结束后的信息，也不等于按相同日历日期连接。`N=1` 表示至少提前一个目标市场交易周期。负数被禁止，因为它可能代表使用未来信息。

`availability_lag_periods` 另外记录观测从经济事件或市场时点到实际可取得之间的发布延迟，也不得为负数。数据准备阶段未来必须同时满足两种滞后和信息截止点。

## 现货油价与连续期货

现货油价（`commodity_spot`）描述即时现货市场价格，不存在期货合约到期和换月，因此禁止填写 `contract_roll_method`。

每个 instrument 都必须明确填写 `continuous_contract`，不能依赖隐含默认值。连续商品期货（`commodity_future` 且 `continuous_contract: true`）把不同到期合约拼接成时间序列。换月日会影响收益率，所以必须明确选择按日历、成交量或持仓量换月。协议只记录规则，不执行拼接。

## 行业指数与 ETF 代理

正式行业指数（`sector_index`）直接表示指数方法定义的行业组合。ETF（`etf`）是可交易基金，可能存在跟踪误差、费用和不同的成分调整。如果用 ETF 代替行业指数，必须在 `proxy_for` 记录它代理的正式概念；股票和 ETF 还必须明确 `price_adjustment`。

## 公式与结构化引用

`model.formula` 是供人阅读的表达式。用于验证和未来执行的权威绑定是 `formula_variable_ids`：它必须无重复，并且必须恰好引用顶层声明的 outcome、predictors 和 controls。这样无需猜测自由文本公式中的名称，也能拒绝未知变量。

## 使用示例

验证仓库中的两个示例：

```powershell
python -m market_validator spec validate examples/research_specs/oil_to_a_share_energy.json
python -m market_validator spec validate examples/research_specs/nikkei_to_us_market.json
```

生成可提供给未来结构化模型后端的 JSON Schema：

```powershell
python -m market_validator spec schema
```

验证成功只说明协议结构和领域约束通过，不代表数据存在、假设成立或分析已经运行。
