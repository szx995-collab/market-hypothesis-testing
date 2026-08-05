# 最小确定性 workflow

当前统一入口只串联一个已经实现并经过验证的离线分析：
`price_change_volatility`。它不是通用分析调度器，也没有尚未实现的分析类型占位符。

```text
用户确认计划
→ AI 生成严格 plan
→ 确定性 workflow 执行
→ 分析产物原子发布
→ 使用外部哈希严格回读
→ AI 仅解释已验证结果
→ 用户作最终判断
```

## Plan 边界

`MarketValidationWorkflowPlan` 是 AI 与确定性执行层之间的边界。它使用
Pydantic 严格验证并拒绝未知字段，包含：

- workflow schema version；
- 唯一支持的 `analysis_type="price_change_volatility"`；
- 预期源 request ID 与 Bundle SHA-256；
- 完整且预先固定的 `PriceChangeVolatilityParameters`；
- 可选的外部 `expected_artifact_manifest_sha256`。

AI 可以提出计划，但计划必须先由用户确认。执行层只执行验证后的字段，不能临时修改窗口、
as-of、Bootstrap 参数、随机种子或判定规则。`bundle_path` 和
`artifact_root` 只是本次运行位置，不属于决定性计划，不参与 artifact ID。

## 固定执行顺序

`run_market_validation_workflow(plan, bundle_path, artifact_root)` 严格按以下顺序执行：

1. 重新严格验证 plan；
2. 检查 Bundle 与 artifact 路径，拒绝穿越、符号链接、非普通源文件和目录重叠；
3. 调用现有 `compare_price_change_volatility()`；
4. 对比分析结果与 plan 的 request ID、Bundle SHA-256 和完整参数；
5. 拒绝包含 analysis error 的结果；
6. 调用现有原子持久化入口；
7. 使用外部 Manifest 哈希（若 plan 提供）或刚生成的 Manifest 哈希严格回读；
8. 要求回读结果与分析结果完全相等，并再次对比 plan；
9. 仅使用严格回读对象构造 `CompletedWorkflowRun`。

workflow 不读取 Bundle 内容，不绕过 `absolute_price_change` 转换，不重新计算统计量或结论，
也不清理结果字段。现有分析产物包仍是唯一正式输出；本阶段没有新增 workflow 产物包。

## 成功与失败不是统计结论

成功结果包含已验证的来源身份、artifact ID 与目录、三个文件的字节数和 SHA-256、
Manifest SHA-256、最终结论、严格回读后的完整结果，以及固定的已完成阶段列表。

失败通过 `WorkflowExecutionError.failure` 返回严格的 `WorkflowFailure`：

- `invalid_plan`
- `source_identity_mismatch`
- `analysis_contract_mismatch`
- `analysis_failed`
- `artifact_conflict`
- `artifact_verification_failed`
- `workflow_path_error`

执行失败不会被转换为 `insufficient_evidence`。后者只表示统计证据不足，不能代表文件冲突、
身份不符、路径错误或完整性验证失败。严格回读失败时，workflow 不删除、不修复也不覆盖文件。

## 当前限制

当前入口只能处理本地已有、经过验证的 Bundle，并执行预先定义的 WTI
`price_change_volatility` 比较。它没有 CLI，不解析自然语言，不获取网络数据，不执行回归、
相关性、回测或其他统计方法，也不能被描述为通用市场分析 workflow。
