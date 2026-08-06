# Research Agent（个人研究会话编排）

`research` 子命令把自然语言问题沿既有生命周期推进为可恢复、可交互的个人
研究流程：假设提案 → 澄清/补全 → 确认 → ResearchSpec → DataPlan →
数据源选择 → 获取授权 → 数据获取 → Data Ready → AnalysisPlan →
分析授权 → 分析执行 → 解释 → 最终 Markdown 报告。

## ResearchSession

会话是不可变 revision 文件：

```text
<workspace>/
  session-revisions/
    000001.json
    000002.json
    ...
  artifacts/          # 每个已生成 artifact（proposal/spec/plan/...）
  snapshots/          # 数据获取快照
  analysis-runs/      # 分析执行结果
  analysis-interpretations/
  final/
    validation-report.md
```

每次状态变化写入新 revision（连续编号、session_id 不变、历史不可覆盖、
同 revision 冲突安全失败）。`status` 与 `resume` 读取最高有效 revision。

## start / status / resume / revise

```powershell
market-validator research start `
  --question-file hypothesis.txt --workspace .\research-001 `
  --provider deepseek_api --model <MODEL> --allow-llm-network

market-validator research status --session .\research-001 [--json]

market-validator research resume --session .\research-001

market-validator research revise `
  --session .\research-001 --instruction "把样本改成二〇二一年以后" `
  --workspace .\research-002 --provider deepseek_api --model <MODEL>
```

`revise` 创建子会话：`parent_session_id` 指向旧会话，原会话不变，
任何 confirmation/authorization/result 均不继承。

## 自动推进范围

Agent 只自动执行不联网、不消费 single-use authorization、不产生新承诺、
不修改既有 artifact 的操作：验证 artifact、计算 hash、编译已确认
Proposal 为 ResearchSpec、生成 DataPlan、从已确认 SourceSelection 规划
获取请求、对已完成 snapshot 运行 readiness、从 Data Ready 与明确决策
生成 AnalysisPlan 建议、验证 AnalysisResult、生成 EvidencePackage、
渲染 Markdown、状态恢复与报告路径展示。

## 用户必须确认的边界

以下操作 Agent 绝不自动执行，只有用户输入 `confirm` 才调用既有 API：

- 确认 hypothesis proposal / DataPlan / SourceSelection / AnalysisPlan；
- 选择 Provider mapping；
- 创建 DataAccessAuthorization 并执行获取（网络 Provider 还需
  `--allow-data-network`）；
- 创建 AnalysisAuthorization 并执行统计；
- 发起 LLM 请求（需要 `--allow-llm-network`）。

`cancel` 停止当前操作。按 Enter 不作为确认。

## 数据接口缺口

当 InstrumentRegistry 中没有与需求匹配的已验证 Provider mapping 时，
Agent 生成 `configure_data_interface`，列出 requirement 的全部维度
（requirement_id / variable_id / instrument_id / field / frequency /
start_date / end_date / timezone / currency / unit /
required_pre_sample_periods），并说明两种做法：

- A. 在 InstrumentRegistry 增加 verified provider mapping；
- B. 使用 local CSV 提供数据。

不得假装已经找到数据源。local CSV 文件必须：非 symlink、regular file、
UTF-8，并先通过现有 inspection/validation；不合格时展示现有 quality
issue，不静默修复。

## 中断恢复与幂等

已有且 hash 验证通过的 artifact 直接复用。不得重复调用 LLM、Provider、
消费 authorization、执行统计或生成同路径不同内容。single-use
authorization 已消费但结果缺失时进入 `blocked`，提示用户重新确认并
创建新的 authorization，不自动重试。

## 错误翻译

既有结构化错误被翻译为人类可读的 NextAction，并保留原始安全错误码
（`source_error_code`）。真实 blocker 不会被吞掉。

## 当前不支持

- 任意互联网数据搜索与网页抓取；
- robustness；
- Web UI / 聊天网页；
- 自动下单或交易相关操作。
