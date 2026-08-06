# v0.4 Research Agent 离线示例

## 离线演示

```powershell
python examples\v0.4_research_agent\run_example.py `
  --output .\research-demo
```

成功时 stdout 输出一个 JSON 对象，包含 `status = completed`、
`analysis_result_id`、`overall_conclusion`、`report_path` 与
`report_sha256`。

## 查看状态

```powershell
market-validator research status `
  --session .\research-demo
```

## 查看报告

```text
research-demo/final/validation-report.md
```

## 说明

该示例使用 synthetic 数据与 fixture LLM，仅用于演示端到端流程。

```text
该示例使用 synthetic 数据与 fixture LLM
不代表真实市场结论
不联网
不需要 API key
不提供投资建议
```

示例的自动确认只存在于本 TEST-ONLY 示例脚本中，不会改变生产
`market-validator research` CLI 的确认边界。
