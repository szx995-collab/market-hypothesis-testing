# Analysis interpretation (v0.4.0 Phase 3)

## Python 计算，LLM 解释

统计与结论全部由确定性 Python 代码完成：`build_analysis_evidence_package`
把已验证的 `AnalysisResult` 编译为不可变 `AnalysisEvidencePackage`
（schema 1.0，含证据 hash、总体结论、每个 primary test 的
`deterministic_statement`）。LLM 只接收 EvidencePackage + 语言 + 风格，
只负责解释。

## LLM 不改变结论

总体结论由 `derive_overall_conclusion` 确定性聚合：

```text
single test  → 该 test 的 conclusion
全部 supported → supported；全部 not_supported → not_supported
全部 inconclusive → inconclusive；其他组合 → mixed
```

LLM 的 `acknowledged_overall_conclusion` 必须精确等于 EvidencePackage 的
总体结论，否则拒绝（`llm_conclusion_mismatch`）。

## 不发送原始数据

发送给 LLM 的内容只有：AnalysisEvidencePackage、语言、解释风格。原始
Bundle、observations、aligned rows、credentials、API key、本地绝对路径、
环境变量、authorization receipt、完整 residual 数组一律不发送。

## 不允许数字编造

headline / plain_language_summary / explanation /
limitations_explanation / cannot_conclude / suggested_followups 中拒绝
新出现的阿拉伯数字、百分号、货币数值、小数、科学计数法
（`ungrounded_numeric_claim`）。所有统计数字由 Markdown renderer 从
EvidencePackage 确定性插入。

## 不提供投资建议

`validate_interpretation_matches_evidence` 扫描中英文交易词（buy/sell/
long/short/position/trade/买入/卖出/做多/做空/建仓/仓位），命中即拒绝
（`forbidden_advice`）。suggested_followups 最多 3 项且必须来自白名单
（改变样本窗口/尝试已声明方法/检查异常时期/检查滞后设定/增加明确控制
变量/收集更长或更高质量数据/执行 robustness）。

## 未执行 robustness

EvidencePackage 的 limitations 固定包含"未执行 robustness"，与声明
方法相关的固定限制（Pearson：线性相关≠因果；Spearman：单调关系≠因果；
OLS：回归≠因果等）全部由 Python 注入，LLM 不得删改。

## 固定 prompt

prompt 版本化（`interpretation-prompt-v1`），明确：你不是统计计算器、
不得改变结论、不得重新计算、不得增加数字、不得声称因果、不得提供投资
建议、只返回符合 schema 的 JSON；temperature=0；不要求 chain-of-thought，
不持久化隐藏推理。

## LLM clients

- `FixtureLLMClient`：确定性离线实现，用于测试/CI/synthetic example。
- `ChatCompletionsHTTPClient`：标准库最小 adapter；网络默认禁止
  （`allow_network=True` 才放行）；只允许 https 与 loopback http；
  API key 只进 Authorization header，不落盘、不进日志；无 retry、无
  fallback、不跟随任何 redirect；响应大小受限；超时安全失败；错误不含
  response body / API key / endpoint query。

## Markdown 验证结果

`render_validation_report_markdown` 确定性渲染 11 节：

```text
# 假设验证结果
## 结论 / 原始假设 / 研究设定 / 主要统计结果 / 通俗解释 /
## 各检验解释 / 数据与方法限制 / 不能得出的结论 /
## 后续验证建议 / Provenance / 免责声明
```

统计表格（test/estimate/confidence interval/raw p/adjusted p/alpha/
conclusion）由 Python 渲染；结论措辞固定（supported/not_supported/
inconclusive/mixed），不写"已经证明/必然/确定导致/未来一定"。

## 持久化

独立不可变目录（不改动 analysis run）：

```text
analysis-interpretations/<analysis_result_id>/<interpretation_id>/
  evidence-package.json
  interpretation.json
  validation-report.md
  interpretation-manifest.json
```

staging → 逐文件 hash → atomic rename → create-only；manifest 绑定
analysis_result / evidence_package / interpretation / validation_report
hashes + prompt_version + model identifier；不保存 API key、
Authorization header、credential、本地绝对路径、原始 HTTP response。

## 错误码

invalid_interpretation_input / analysis_result_mismatch /
evidence_mismatch / llm_network_not_authorized / llm_request_failed /
llm_response_too_large / llm_response_invalid / llm_conclusion_mismatch /
unknown_test_reference / missing_test_interpretation /
ungrounded_numeric_claim / forbidden_advice /
interpretation_output_conflict / interpretation_output_error。
