# Analysis execution (v0.4.0 Phase 2)

## Authorization consumption

`AnalysisAuthorizationConsumptionReceipt`（schema 1.0）在执行任何 Bundle
读取之前持久化。固定顺序：

1. 验证 plan（id 与 canonical core 自洽）；
2. 验证 confirmation 与 plan 匹配；
3. 验证 authorization 与 confirmation/plan 匹配（test IDs 精确）；
4. 验证 Data Ready 上游绑定（spec hash、正式
   `validate_data_ready_manifest_matches` 全字段、manifest 绑定 plan）；
5. create-only 持久化 receipt；
6. 之后才读取 Bundle 并执行统计。

receipt 已存在时返回 `authorization_already_consumed`；执行中失败
authorization 仍保持 consumed，绝不自动 retry。

## Transformations

`TransformedVariableSeries` 记录每个变量的变换合同与审计字段
（session_date / source_session_date / value / available_time /
source_observation_time / input/output/excluded 计数）。

- `level_v1`：原样使用 bundle observation；
- `simple_return_adjacent_v1` / `log_return_adjacent_v1`：复用
  `transform_price_bundle`（同一套公式，不复制实现）；非正价格拒绝；
- `signed_first_difference_v1`：复用 `transform_absolute_price_change`。

price 变换合同要求原始观测（level）bundle；分析 bundle 携带非 level
变换标注时，执行器在临时目录创建字节一致的 level 视图副本用于公式复用，
绝不修改 snapshot 或 Bundle。所有中间值必须有限；provider missing gap
不填充。

## Strict session alignment

只执行 `strict_same_session_v1`（`join_policy=strict_match`、
`max_staleness_days=0`）。对每个 target session：

```text
effective_shift = lag_periods + availability_lag_periods
```

变量使用 target schedule 中向前移动 `effective_shift` 个 session 的
transformed observation（session identity，不是自然日、不是行号）。

## No-lookahead

`information_cutoff=null` 时 cutoff = 当前 target session 的 outcome
observation available_time；`specified_local_time` 时 cutoff = session
date + 声明 local_time + timezone。任何变量 `available_time > cutoff`
均不得进入该行（drop 政策下计数删除；error 政策下
`lookahead_detected`）。`before_target_open` / `before_target_close`
无精确 session-time 合同，planner 保持 Draft
（`information_cutoff_not_executable`）。

## Missing policy

- `drop_observation_v1`：任意变量缺失或超 cutoff → 整行删除，记录
  `candidate_row_count / retained_row_count / dropped_row_count /
  drop_reason_counts`；
- `error_v1`：任何缺失或 cutoff violation → execution failed
  （`missing_data_error` / `lookahead_detected`）；
- `keep_missing_v1`：不可执行，planner 保持 Draft
  （`analysis_missing_policy_not_executable`）。

不插值、不 forward/back-fill、不均值填充。

## Pearson

`compute_pearson_result`：r、n、df=n-2、t、p（Student-t 双侧）、
Fisher-z 置信区间（confidence level = 1 - alpha）。零方差安全失败
（`zero_variance`）；|r|=1 映射为有限大 t 与 p=0，NaN/Infinity 永不进入
artifact。

## Spearman

average ranks（ties 取平均）+ 对 ranks 的 Pearson；t 近似；Fisher-z
近似置信区间。结果记录 `tie_method=average_rank_v1` 与
`inference_profile=spearman_t_approximation_v1`；绝不切换 exact
permutation test。

## OLS

支持多 predictor、零或更多 control、可选 intercept（来自 plan 记录的
显式决策）。输出系数、标准误、t、p、置信区间、残差、SSE、自由度、R²、
adjusted R²。

## Covariance estimators

- `classic`：s²(X'X)⁻¹；
- `HC1`：夹心估计 × n/(n-k)；
- `Newey-West`（Bartlett kernel）：`max_lags = AnalysisPlan 中显式值`，
  无自动 bandwidth。

安全拒绝：奇异设计矩阵、n ≤ 参数数、零残差自由度、非有限结果。

## p 值与置信区间

确定性的纯标准库 Student-t CDF（正则化不完全 beta + Lentz 连分数）与
逆 CDF（二分）；正态分位数用 erf 逆。所有持久化浮点统一规范化为 12 位
有效数字（`python_binary64_deterministic_v1`）。

## Multiple testing

支持 `none`、`bonferroni`、`holm`、`benjamini_hochberg`（correction
必须等于 AnalysisPlan 声明，不按结果更换）。输出同时保存
`raw_p_value` 与 `adjusted_p_value`。

## Conclusion rules

- `supported`：adjusted p ≤ alpha 且方向满足且 |estimate| ≥
  minimum_effect_size；
- `not_supported`：adjusted p ≤ alpha 且显著指向相反方向，或 two-sided
  显著但未达 minimum effect；
- `inconclusive`：其余情况。

"不显著"绝不直接写成"假设错误"。

## AnalysisResult

`AnalysisResult`（schema 1.0）绑定 plan / receipt / manifest / aligned
dataset hashes；`model_summary` 是严格 discriminated union
（`CorrelationResultSummary` / `OLSResultSummary`）；`analysis_result_id`
由除自身外完整 canonical core 派生。结果目录：

```text
analysis-runs/<attempt_id>/
  execution-manifest.json
  execution-outcome.json
  transformed/<variable_id>.json
  aligned-dataset.json
  analysis-result.json
```

同文件系统 staging → 逐文件 hash 验证 → atomic rename → create-only；
失败不留下部分成功目录。

## AnalysisResult ≠ LLM 解释

AnalysisResult 只含统计合同输出；本阶段不调用 LLM、不生成自然语言
结论、不执行 robustness、不生成最终报告。

## 非投资建议

Result Verified ≠ Investment Advice；系统不执行真实交易、不提供自动
下单能力。
