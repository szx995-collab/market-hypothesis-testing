# Acquisition Request 与 Data Access Authorization（v0.3.0 Phase 2）

## 1. workflow 位置

```text
hypothesis_draft → spec_review → data_plan → source_selection
→ acquisition_request   ← 本阶段
→ data_access_authorization
→ provider_execution（未授权）
→ data_ready
```

本阶段只推进：

```text
SourceSelection Confirmed
→ AcquisitionRequest Draft
→ AcquisitionRequest Ready
→ DataAccessAuthorization Ready
→ DataAccess Authorized
```

本阶段只生成和确认"未来允许执行什么"，不执行任何数据访问。

## 2. AcquisitionRequest Draft / Ready

- **Draft**：`generate_acquisition_request_plan` 的产物。可能存在 unresolved
  requirements（pre-sample 无法证明、local-file 身份无法规范化等）或未通过
  readiness 检查，可持久化审计，但不可授权。
- **Ready**：`acquisition_request_readiness_blockers` 为空——无 unresolved、
  双 registry hash 匹配、每个 request 唯一、capability snapshot 与 request
  一致、pre-sample 全部可证明、无阻塞 warning。
- **Draft ≠ Ready ≠ Authorized**。旧 SourceSelectionConfirmation 失效时不得
  生成 Ready request plan（生成时即校验 confirmation 的 6 个绑定 hash）。

## 3. public parameters 与 secret 的边界

`PublicAcquisitionRequest.public_parameters` 只能包含可公开记录的参数
（series_id、observation_start/end、frequency、revision policy、source_uri
等，与现有 Provider 实际支持的公开参数一致）。请求模型禁止任何 secret、
API key、authorization header、credential name/value；参数 canonical 排序，
顺序不影响 hash；request 不含执行时间、响应内容、retry 计数或 fallback 候选。

## 4. capability snapshot

`ProviderCapabilitySnapshot` 是离线、确定性、无凭据的能力视图：
provider_id、supported_access_modes、supported_frequencies、
supported_transforms、supports_date_range、supports_revision_policy、
supports_dry_run、supports_previous_observations、requires_credential、
paid_access_possible。`snapshot_provider_capabilities` 根据现有
`ProviderCapabilities` 代码事实建模（FRED→network、CSV→local_file；
FRED 无 dry-run 前的路径为 False 等）。能力不满足 requirement 时是
structured hard failure（`provider_capability_mismatch`），不虚构能力。

## 5. pre-sample resolution

`PreSampleResolution` 证明每个 requirement 的 acquisition_start：

```text
required_pre_sample_periods == 0 → none_required，acquisition_start = sample_start
verified calendar session adapter → acquisition_start 由适配器计算
provider-native previous-observation 方法 → 由 provider 参数表达
否则 → unresolved，不得 Ready，不得授权
```

## 6. 为什么 N periods 不等于 N calendar days

`required_pre_sample_periods` 是"变换历史 + 滞后"的观测期数，不是自然日。
只有经过验证的 calendar-session 适配器（能精确计算 N 个交易时段之前的
日期）或 provider 原生"前 N 个观测"参数才能证明 acquisition_start。
当前 CalendarRegistry 只有身份、没有可执行的 session 适配器时，系统绝不
用 `sample_start - N days` 猜测；FRED 当前接口无法精确表达"前 N 个有效
观测"，因此返回 unresolved。带 returns、difference、rolling 或 lag 的真实
例子可能保持 unresolved——这是正确行为，不是缺陷。

## 7. unresolved 如何阻止授权

unresolved code 包括 `pre_sample_calendar_adapter_unavailable`、
`pre_sample_provider_method_unavailable`、`pre_sample_resolution_ambiguous`、
`local_file_content_identity_required`、`source_selection_unresolved`。
任何 unresolved 都使 plan 无法 Ready、authorization 无法创建。

## 8. network / local-file 显式授权

`DataAccessAuthorization` 按 plan 实际需要显式设置
`network_access_authorized` 与 `local_file_read_authorized`：有 network
request 才授权 network，有 local-file request 才授权 local-file；不需要的
访问类型不得顺手授权。local-file request 必须绑定规范化 source URI/path
identity（绝对路径或 file:// URI）；相对路径无法证明内容身份时返回
`local_file_content_identity_required` unresolved。本阶段禁止读取文件内容、
禁止解析 symlink 目标、禁止计算文件内容 hash。

## 9. paid / retry / fallback 固定禁止

`paid_access_authorized`、`automatic_retry_authorized`、
`fallback_authorized` 为模型级 `Literal[False]`，无法构造为 true；
`interactive_credential_resolution_authorized` 恒为 false（credential 解析
属于后续阶段）。capability 的 `paid_access_possible=true` 只是信息，绝不
等于允许付费。

## 10. single-use

`single_use` 恒为 `true`。`consume_data_access_authorization` 生成
`DataAccessAuthorizationReceipt`（绑定 authorization hash 与
request-plan hash）；同一 authorization 第二次消费被拒绝
（`authorization_already_consumed`）。retry 或失败后的重试都需要新的
authorization。

## 11. consumption 不等于执行

消费授权只是把授权保留给一次未来执行尝试（receipt 状态 `consumed`、
含 `attempt_id`）。消费本身不调用 Provider、不访问网络、不读取文件、
不解析 credential，也不记录 request 成功或失败（请求尚未执行）。

## 12. authorization 不等于成功

```text
DataAccess Authorized ≠ Credential Resolved
DataAccess Authorized ≠ Provider Request Executed
Authorization Consumed ≠ Request Succeeded
Request Succeeded ≠ Snapshot Verified
Snapshot Verified ≠ DataBundle Verified
DataBundle Verified ≠ Data Ready
```

## 13. 本阶段不调用 Provider

没有 HTTP 请求、没有 FRED API 调用、没有 CSV 读取、没有 `fetch`、没有
`dry_run` 执行、没有 raw snapshot。`render_public_acquisition_request` 只
渲染 executor 未来应使用的精确公开请求描述，不执行。

## 14. 本阶段不生成 DataBundle

没有 DataBundle、没有 raw snapshot、没有数据质量执行、没有
DataReadyManifest。

## 15. 本阶段不进入 data_ready

没有 data_ready、没有 analysis、没有 robustness、没有 report。
`DataAccess Authorized` 之后必须等待后续已授权的阶段（Phase 3 及以后）。

## 不可变性

AcquisitionRequestPlan 与 DataAccessAuthorization 均严格
parse/serialize/SHA-256（重复键、NaN/Infinity、extra fields、无效 UTF-8
全部 fail-closed，round-trip 校验，canonical bytes）；持久化 create-only、
byte-identical 幂等、同路径不同内容冲突、path traversal/symlink/非
regular-file 拒绝、部分写入不得视为成功、provenance 时间信息放 sidecar；
authorization 与 receipt 分开持久化；错误消息不包含原始 JSON、secret 或
credential。未修改 Phase 1 的 canonical bytes 与既有语义。
