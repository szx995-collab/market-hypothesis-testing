# Analysis planning and authorization (v0.4.0 Phase 1)

## 1. workflow 位置

```text
… → data_ready → analysis_plan draft → analysis_plan ready
→ analysis_plan confirmed → analysis_authorized → [Phase 2: execution]
```

Phase 1 只到达 `Analysis Authorized`；不计算任何统计量、不执行变换、
不对齐时间序列、不生成分析结果。

## 2. Data Ready ≠ Analysis Ready

`DataReadyManifest` 证明数据可读且 quality/coverage/availability/revision
通过。它不包含执行合同：方法 profile、变换 profile、alignment 语义、
主检验与多重检验计划都必须由 Phase 1 显式编译。生成 `AnalysisPlan` 前会
重新调用正式 `validate_data_ready_manifest_matches`，绝不信任调用方传入
的 `ready=true`。

## 3. ResearchSpec 与 AnalysisPlan 的区别

- ResearchSpec：人类可读的研究声明（假设、公式文本、变量角色、样本窗口）。
- AnalysisPlan：不可变执行合同（method profile、transformation plan、
  alignment plan、primary tests、multiple testing plan、variable-to-bundle
  bindings、全部上游 canonical hashes）。

自由文本 `formula`/`null_hypothesis`/`alternative_hypothesis` 只作为
`human_*` 证据保留，**永不作为代码解析或执行**。

## 4. AnalysisPlanDecisions 白名单

`decisions_schema_version = "1.0"`；字段全部显式适用或显式 null：

```text
method_profile                    pearson_correlation_v1 | spearman_correlation_v1
                                  | ols_classic_v1 | ols_hc1_v1 | ols_newey_west_v1
primary_test_variable_ids         explicit subset of predictors (OLS) or the single predictor
include_intercept                 explicit true/false (OLS) or null (correlation)
covariance_estimator              classic | hc1 | newey_west（匹配 profile）或 null
newey_west_max_lags               explicit non-negative int（仅 Newey-West）或 null
same_market_join_policy           strict_same_session_v1 或 null
same_market_missing_data_policy   drop_observation_v1 | error_v1 | keep_missing_v1 或 null
transformation_decisions          每个变量一个显式 TransformationDecision
```

不存在隐藏默认值；缺失即 unresolved（`analysis_decision_missing`）。

## 5. 为什么自由文本 formula 不可执行

自由文本可以表达任何含义，不能形成精确执行合同。Phase 1 用
`PrimaryTestSpec`（parameter/direction/alpha/minimum effect/family）取代；
`human_formula` 等字段保留为证据。decisions 不得修改 H0/H1、direction 或
alpha（它们只来自 ResearchSpec）。

## 6. method profiles

- `pearson_correlation_v1`：exactly one predictor、zero controls、exactly
  one primary test；intercept/covariance/HAC 必须 null。
- `spearman_correlation_v1`：同上；tie handling 由 profile 固定
  （average-rank），不计算 rank 或 rho。
- `ols_classic_v1` / `ols_hc1_v1` / `ols_newey_west_v1`：至少一个
  predictor；`primary_test_variable_ids` 必须是非空子集；
  `include_intercept` 必须显式；covariance 必须匹配 profile；
  Newey-West 必须显式 `newey_west_max_lags`（无自动 bandwidth）。
- `lead_lag_regression` 与 predictive claims：生成 Draft +
  structured unresolved，绝不悄悄映射为普通 OLS。

## 7. variable-to-bundle binding

`AnalysisVariableBinding` 把每个 ResearchSpec 变量（outcome/predictors/
controls）精确绑定到一个 DataReady bundle record：全部 bundle/session
hashes、quality 状态与 warnings 原样保留（warning 不得丢失）。缺失、
重复、未知 bundle、instrument/field/requirement 不匹配都会产生
unresolved 或由正式验证器拒绝。bundle 或 session evidence 变化必然改变
AnalysisPlan 的 hash 与 id。

## 8. transformation profiles

```text
level_v1                       value = source value；首值保留
simple_return_adjacent_v1      current/previous − 1；首值省略；非正分母拒绝
log_return_adjacent_v1         log(current/previous)；两端必须为正；首值省略
signed_first_difference_v1     current − previous；首值省略
```

gap policy 固定为 provider-reported missing gap excluded；availability 取
端点最大值；`rolling_window_periods` 仅对 level 可显式为 null，其余 profile
不得携带窗口。`pct_change`/`rolling_mean`/`zscore` 保持 unresolved
（比例尺度、窗口完整性、minimum periods、ddof、标准化语义尚无精确合同）。
**本阶段不执行任何变换函数。**

## 9. alignment 与 no-lookahead

- ResearchSpec 有 `alignment` 时逐字段保留（target market/timezone/
  calendar/session/information cutoff/join policy/max staleness/missing
  policy）；decisions 不得修改。
- ResearchSpec 无 alignment 时，只有 decisions 显式提供
  `same_market_join_policy = strict_same_session_v1` 与
  `same_market_missing_data_policy` 才可 Ready。
- 第一版只允许 strict same-session（`max_staleness_days = 0`、
  `join_policy = strict_match`）；任何 previous/as-of/staleness join 必须
  来自 ResearchSpec 显式 AlignmentSpec。
- `no_lookahead` 恒为 true；不得从 instrument market 或 timezone 推断
  missing policy。

## 10. primary tests

`PrimaryTestSpec`：`parameter` 仅
`pearson_r | spearman_rho | ols_coefficient`；`null_value = 0`；
direction/significance/minimum effect 来自 ResearchSpec；test_id 确定性
派生（spec_id + variable_id + parameter 的 canonical hash 前缀）。

## 11. multiple testing family

`MultipleTestingPlan` 的 `correction` 必须等于 ResearchSpec 声明
（即使 size=1 family 也不得静默改成 none）；`family_size` 与 `test_ids`
确定性一致。

## 12. Draft / Ready

- `draft`：存在 unresolved requirements；可持久化、可检查；不可确认。
- `ready`：无 unresolved；可确认；确认后才可授权。
- status 一致性由模型校验（ready 不得携带 unresolved）。

## 13. confirmation

`AnalysisPlanConfirmation`（schema 1.0）固定声明：

```text
I explicitly confirm this exact AnalysisPlan for a separately authorized
analysis execution.
```

仅 Ready 且无 unresolved 的 plan 可确认；`confirmed_at` 必须是
timezone-aware UTC；plan 变化使旧 confirmation 失效；warnings 允许保留。

## 14. authorization

`AnalysisAuthorization`（schema 1.0）固定声明：

```text
I explicitly authorize one deterministic execution attempt of this exact
confirmed AnalysisPlan.
```

必须绑定 confirmation hash；`authorized_primary_test_ids` 必须精确等于
plan 的 primary test IDs（不允许缺失或额外）。

## 15. single-use

`single_use` 恒为 true；Phase 2 的 executor 将实现消费 receipt，Phase 1
不消费 authorization。

## 16. robustness 未授权

`robustness_execution_authorized` 恒为 false；`declared_robustness_checks`
只记录声明（含 canonical hash），`robustness_execution_not_planned` 作为
warning 而不是 readiness blocker。

## 17. report 未授权

`report_generation_authorized` 恒为 false；本阶段不生成任何分析或研究
报告 artifact。

## 18. 本阶段不计算统计量

不执行 Pearson/Spearman/OLS；不计算 p 值或置信区间；不拟合模型；
`numeric_profile = python_binary64_deterministic_v1` 只定义未来执行要求。

## 19. confirmation/hash 不是数字签名

Confirmation 与 authorization 的 SHA-256 绑定是内容完整性证明，不是数字
签名、不是用户对结果的认可、不是显著性判断。

## 21. 执行边界（Phase 2 合同）

`keep_missing_v1` 不可执行 → Draft（`analysis_missing_policy_not_executable`）；
`before_target_open` / `before_target_close` 信息截止无精确 session-time
合同 → Draft（`information_cutoff_not_executable`）。执行器只接受
`drop_observation_v1` / `error_v1` 与 `information_cutoff = null` /
`specified_local_time`。OLSR 的 intercept 与 Newey-West max lag 是显式
plan 记录的执行决策（无隐藏默认）。详见 `docs/analysis_execution.md`。

## 20. Result Verified ≠ 投资建议

即使未来某阶段产生 verified result，也绝不等于投资建议；系统不执行真实
交易、不提供自动下单能力。
