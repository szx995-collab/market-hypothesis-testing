# Source Selection（v0.3.0 第一阶段）

## 1. 当前 workflow 位置

```text
hypothesis_draft
→ spec_review
→ data_plan
→ source_selection   ← 本阶段
→ data_ready
```

本阶段只推进：

```text
DataPlan Confirmed
→ SourceSelection Draft
→ SourceSelection Ready
→ SourceSelection Confirmed
```

## 2. SourceSelection 的输入和输出

输入（全部为已严格验证的对象）：

- `GeneratedDataPlan`
- `DataPlanConfirmation`
- `InstrumentRegistry`（当前快照）
- `CalendarRegistry`（当前快照）
- `list[SourceSelectionDecision]`（用户对每个 requirement 的明确选择）

输出：

- `GeneratedSourceSelection`：`SourceSelection` + 其 canonical SHA-256 +
  全部绑定 hash（ResearchSpec、DataPlan、DataPlanConfirmation、
  InstrumentRegistry、CalendarRegistry）
- `SourceSelectionConfirmation`（readiness 通过后）
- provenance sidecar（持久化时生成）

`SourceSelection` 是确定性 artifact：`selection_id` 由绑定 hashes 与选择内容
确定性派生；`selections`、`unresolved_requirements`、`warnings` 全部确定性
排序（去重）；输入顺序变化不会改变 canonical bytes 与 SHA-256。

## 3. 为什么单一候选也不自动选择

即使一个 instrument 只有一个 verified mapping，系统也绝不自动选择。
选择必须是调用方提供的显式 `SourceSelectionDecision`。缺少选择时生成带
`source_not_selected` 的 structured unresolved requirement，并且
SourceSelection 无法进入 Ready，更无法确认。自动选择或按顺序挑选会掩盖
用户意图，属于禁止行为。

## 4. exact mapping identity

选择通过以下三元组精确匹配 registry mapping：

```text
provider_id
provider_symbol
dataset_or_endpoint
```

不允许模糊匹配、大小写猜测、provider 名称或别名近似。显式选择不存在的
mapping、unverified mapping 或与 registry 冲突的 mapping market 时，返回
结构化 hard failure（`source_selection_mapping_mismatch`），不会降级为普通
unresolved。

`SelectedProviderSource` 中的全部 identity 与 verification metadata
（`provider_id`、`provider_symbol`、`dataset_or_endpoint`、`market`、
`mapping_verified_on`、`mapping_verification_source_uri`）一律从当前
`InstrumentRegistry` 的精确 mapping 复制，不信任调用方传入的自由文本。

## 5. SourceSelection Draft、Ready、Confirmed 的区别

- **Draft**：已生成，可能存在 unresolved requirements 或尚未通过 readiness；
  可以持久化用于审计，但不可确认。
- **Ready**：`source_selection_readiness_blockers` 为空——每个 requirement
  恰好一个 selection、无 unresolved、无重复、instrument 与 mapping 仍存在且
  verified、registry hashes 仍匹配、无阻塞 warning。
- **Confirmed**：`confirm_source_selection` 只接受 Ready 状态，生成绑定
  exact hashes 的 `SourceSelectionConfirmation`。

```text
SourceSelection Draft ≠ Ready ≠ Confirmed
```

## 6. SourceSelectionConfirmation 绑定哪些 hashes

```text
research_spec_sha256
data_plan_sha256
data_plan_confirmation_sha256
source_selection_sha256
instrument_registry_sha256
calendar_registry_sha256
```

`validate_source_selection_confirmation_matches` 逐项校验全部 6 个 hash。
任何一项变化（DataPlan、DataPlanConfirmation、SourceSelection、任一
registry 内容变化）都会使旧确认失效。

## 7. confirmation 不是网络授权

`SourceSelectionConfirmation` 只表示：

> 用户确认了这份 DataPlan 中每个 requirement 所对应的精确 Provider
> mapping，后续可以进入 acquisition-request planning。

它不是数字签名、身份认证，也绝不授权网络访问、Provider 请求、credential、
下载、付费、retry、fallback、分析、回测、交易或下单。模型禁止任何
`allow_network`、`network_authorized`、`credential`、`api_key`、`token`、
`secret`、`password`、`paid_access`、`retry`、`fallback` 字段。

## 8. registry 变化如何使旧确认失效

- `generate_source_selection` 首先校验当前 registry hashes 与
  `GeneratedDataPlan` 绑定值一致；不一致即失败
  （`data_plan_confirmation_mismatch`）。
- `source_selection_readiness_blockers` 重新核对 instrument 存在性、
  identity 仍为 `VERIFIED`、mapping 仍存在且 verified、metadata 一致。
- confirmation 的 6 个绑定 hash 与生成结果逐项比对，任何变化都使确认失效。
- 只改变 registry 条目顺序（内容不变）时 canonical hash 保持不变。

## 9. structured unresolved requirements

`UnresolvedSourceSelectionRequirement` 结构化携带：

```text
requirement_id
variable_id
instrument_id
code
message
```

本阶段支持：`source_not_selected`、`instrument_not_registered`、
`instrument_identity_not_verified`、`no_verified_provider_mapping`。

identity 未注册/未验证等场景在实际管道中由更早层 fail-closed：
planner 与 DataPlan readiness 会拒绝非 `VERIFIED` identity 或未注册
instrument 的 DataPlan 确认；SourceSelection 层保留防御性检查。

## 10. 本阶段不执行 Provider

```text
SourceSelection Confirmed ≠ Provider Capability Validated
SourceSelection Confirmed ≠ Provider Executed
```

本阶段不检查 Provider 是否能够实际执行请求，不构造
AcquisitionRequestPlan，不触碰任何 Provider。精确 capability validation
属于 Phase 2（acquisition-request planning）。

## 11. 本阶段不生成 DataBundle

没有 `DataBundle`、raw snapshot、DataAccessAuthorization、网络授权或
credential 解析。SourceSelection 与确认文件不包含任何数据内容或获取授权。

## 12. 本阶段不进入 data_ready

没有 DataReadyManifest、数据质量执行、analysis、robustness 或 report。
`SourceSelection Confirmed` 之后必须等待后续已授权的阶段。

## 不可变性

`persist_generated_source_selection` 同时持久化 canonical SourceSelection
bytes 与 provenance sidecar（`.provenance.json`）：create-only、不覆盖
现有文件、相同路径不同内容明确冲突、拒绝 path traversal 与 symlink、
非 regular-file 输入拒绝、部分输出不得被视为成功。错误结构化且不包含
原始敏感输入。
