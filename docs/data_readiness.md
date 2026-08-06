# Data Readiness 与 DataReadyManifest（v0.3.0 Phase 4）

## 1. workflow 位置

```text
hypothesis_draft → spec_review → data_plan → source_selection
→ acquisition_request → data_access_authorization
→ provider_execution → snapshot_verified
→ data_readiness   ← 本阶段
→ data_ready
→ analysis
```

本阶段推进：

```text
Snapshot Verified
→ Bundles Reloaded
→ Quality Assessed
→ Sample Coverage Verified
→ Pre-sample Coverage Verified
→ DataReadiness Ready
→ DataReadyManifest Created
→ Data Ready
```

## 2. Snapshot Verified 与 Data Ready 的区别

Snapshot Verified 只证明：被授权的响应与 DataBundle 已作为一个不可变快照提交并通过
hash 验证。Data Ready 额外证明：每个 DataPlan requirement 恰好有一个经过 snapshot、
identity、quality、sample coverage、pre-sample coverage、availability 与 revision
验证的 DataBundle，并由一个精确绑定的 DataReadyManifest 聚合证明。

```text
Snapshot Verified ≠ Quality Accepted ≠ Sample Coverage Verified
≠ Pre-sample Coverage Verified ≠ DataReadiness Ready
≠ DataReadyManifest Persisted ≠ Analysis Authorized ≠ Analysis Executed
```

## 3. acquisition window 修复

request 同时表达 `sample_start/sample_end` 与 `acquisition_start/acquisition_end`。
Phase 4 之前 FRED HTTP step 使用 `requirement.start_date`，quality selection 也只
保留 sample 窗口——导致 "pre-sample 已解析 ≠ pre-sample 已请求 ≠ pre-sample 已保留"。

## 4. schema 1.2

acquisition request schema 升级为 `1.2`：

- FRED HTTP step 的 `observation_start = request.acquisition_start`、
  `observation_end = request.acquisition_end`（具体日期已解析的请求）；
- 旧 `1.0`、`1.1` artifact 严格 fail closed（`Literal["1.2"]`），不静默迁移；
- Phase 2/3 的 AcquisitionRequestPlan 与 DataAccessAuthorization 必须重新生成；
- endpoint、日期、step、pagination、window 任一变化都改变 plan hash，旧
  authorization 不得匹配新 plan。

## 5. Bundle 保留 pre-sample

正式 capture path 显式传 acquisition window 给 `build_quality_report`
（`selection_start/selection_end` 可选参数，默认保持 legacy 的 requirement 边界）。
`DataBundle.requirement` 仍等于原始 DataPlan requirement；legacy
`FredProvider.fetch` / `CSVProvider.fetch` 行为不变。Bundle 可以包含 sample start
之前的 pre-sample observations；不修改 raw 数据、不生成 derived 值。

## 6. Quality PASS / WARN / FAIL

- **FAIL**：阻止 Data Ready（no_observations、unexpected_instrument/field、
  currency/unit/timezone mismatch、duplicate、time order、invalid encoding、
  missing/unexpected columns、invalid/non-finite numeric、invalid date/time 等）。
- **WARN**：允许进入 Data Ready，但 warning code 全部保留并出现在 requirement
  assessment 与 DataReadyManifest aggregate warnings 中
  （latest_revision_hindsight_risk、conservative_date_availability、
  provider_missing_value 等）。
- **PASS**：仍必须通过 coverage、pre-sample、availability、revision 与 identity
  检查。

## 7. WARN 允许 ≠ Coverage 自动接受

`provider_missing_value` 等 warning 是否造成 coverage 缺口，由 sample/pre-sample
coverage 检查单独决定。DataReady 层不"修好"任何 quality 问题，不自动删除、不插值、
不填充。

## 8. sample coverage 需要 verified sessions

- 只严格支持 `Frequency.ONE_DAY`；其他 frequency 是结构化 blocker。
- expected sessions 来自 calendar 的 `schedule_adapter` 引用的
  `VerifiedSessionScheduleAdapter`（调用方显式注入，禁止按 calendar 名称或 market
  自动选择）。
- 必须验证：expected/observed 无重复、每个 expected session 恰好一个 observation、
  无缺失、无额外 session、identity/field/timezone/currency/unit 正确、时间顺序正确。
- 没有 adapter → `sample_coverage_unverifiable`。

## 9. N periods ≠ N 自然日

绝不用自然日当作 sessions。`previous_sessions` 返回严格递增、数量精确、全部严格
早于 sample start 的 verified sessions。

## 10. pre-sample completeness

- `required_pre_sample_periods == 0`：expected=0；快照中出现计划外更早 observation
  阻止 Data Ready。
- verified calendar sessions：expected = `previous_sessions(start_date, N)`；
  `request.acquisition_start` 必须等于第一个 expected pre-sample session；Bundle 包含
  全部；无计划外更早 session。
- provider-native：仅当 capability snapshot 明确
  `supports_previous_observations = true` 且 request 绑定该语义；FRED/CSV 当前不冒充
  支持。
- 不完整 → `pre_sample_missing_observations` / `pre_sample_extra_observations` /
  `pre_sample_request_window_mismatch` / `pre_sample_method_unverifiable`。

## 11. source content identity

不信任 Bundle 内 `source.content_sha256` 本身，重新计算：

- FRED：raw observation pages 的 length-prefixed 拼接 SHA-256（metadata 响应不混入；
  页缺失或顺序不明时阻止；与 trace/pagination 顺序一致）。
- local CSV：`sha256(exact captured source bytes)`。
- 未知 provider：不猜测，无显式 verifier 时结构化 blocker。

## 12. availability cutoff

- observation `available_time` 必须 timezone-aware；`available_time >= observation_time`
  重新确认。
- 明确 information cutoff：按模型真实语义比较（specified_local_time 与
  observation_time 比较）；超过 cutoff 的 observation 阻止 requirement readiness，
  不删除后继续宣称完整。
- 无法确定性求值（before_target_open/close 需要 alignment）→
  `information_cutoff_unverifiable`。
- retrieval time 不作历史 availability time；latest-available 保留 hindsight warning。

## 13. revision consistency

每个 observation 的 `revision_policy` 必须与 requirement 一致：

- `initial_release`：`vintage_date` 必须存在，不得用 retrieval date 替代；
- `latest_available`：允许，保留 hindsight-risk warning；
- `not_applicable`：不得出现伪造 vintage；
- 不支持的模式：blocker，不自动转换。

## 14. aggregate requirement completeness

Data Ready 前必须证明：每个 DataPlan requirement 恰好一个 request、每个 request
恰好一个 bundle artifact、无未知/重复/缺失 bundle、bundle 严格解析且 SHA-256 与
snapshot manifest 一致、bundle requirement 与 DataPlan requirement 完全相等、
provider/symbol/dataset identity 一致、source URI 与 public parameters 无 secret、
`is_fallback=false`、observations 非空、snapshot 无额外 bundle artifact。

## 15. DataReadinessAssessment

确定性 artifact（schema 1.0）：assessment_id、status（ready/blocked）、snapshot_id、
snapshot_manifest_sha256、9 个上游 hash、按 requirement_id 排序的
RequirementReadinessAssessment（quality/coverage/pre-sample/availability/revision
逐项字段 + count 与 session-set 边界 + blockers + warnings）、排序去重的 aggregate
blockers/warnings。无生成时间；输入顺序不影响 hash；snapshot/plan/registry/bundle
变化改变 identity；blocked assessment 可生成和持久化，但不得生成 DataReadyManifest。

## 16. DataReadyManifest

仅当 assessment 为 ready 时生成（schema 1.0）：data_ready_id、
readiness_assessment_sha256、snapshot_id、snapshot_manifest_sha256、9 个上游 hash、
按 requirement 排序的 DataReadyBundleRecord（bundle_relative_path 为
snapshot-relative，无绝对机器路径）、排序去重的 warnings。不含生成时间、credential、
raw data、统计结果或分析参数；不授权 analysis。

## 17. deterministic hash binding

assessment 与 manifest 的 canonical JSON（UTF-8、单对象、拒绝 duplicate keys 与
NaN/Infinity、extra fields 禁止、稳定 key 排序、canonical separators、尾部换行、
round-trip 相等）确定 SHA-256；持久化 create-only、byte-identical 幂等、同路径不同
内容冲突、traversal/symlink/non-regular 拒绝、sidecar 成对回滚、reload 后重新验证
hash。

## 18. blocked assessment

真实示例（油价/A 股）若 calendar 无 schedule adapter、pre-sample 无法证明或 sample
sessions 无法证明，生成 blocked assessment（如 `sample_coverage_unverifiable` /
`pre_sample_method_unverifiable`）——这是正确结果。不修改真实 registry 来展示
data_ready；fully synthetic、offline、verified-calendar 示例才能达到 Data Ready。

## 19. real examples 可保持 blocked

不得为了展示 data_ready 而修改 config/instruments.json 或 config/calendars.json。
缺失/无法证明即为 blocked。

## 20. Data Ready 不授权 analysis

```text
Data Ready 只表示：当前 DataPlan 的每个 DataRequirement 均有且仅有一个经过
snapshot、identity、quality、sample coverage、pre-sample coverage、availability
和 revision 验证的 DataBundle，并由一个精确绑定的 DataReadyManifest 聚合证明。
```

它不是分析授权、假设检验结果、robustness 结果、报告、交易建议或交易授权。
analysis / robustness / report 属于后续阶段；本阶段不实现、不执行、不描述为已有。
