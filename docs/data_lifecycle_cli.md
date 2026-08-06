# Unified data-lifecycle CLI (v0.3.0)

`market-validator data-lifecycle` 是正式数据生命周期的统一 JSON-only CLI。
`python -m market_validator data-lifecycle ...` 与 console entry point
`market-validator data-lifecycle ...` 完全等价。

CLI 是现有 domain API 的薄适配层；它不复制业务逻辑，并且**永不自动确认、
自动授权、自动选择 Provider 或自动执行下一步**。

## 原则

- JSON-only stdout 与安全 stderr；稳定 exit codes；严格 UTF-8。
- 拒绝重复 JSON key、未知字段、NaN/Infinity、symlink/traversal 路径。
- 不输出 secret、raw response 或 traceback；不修改输入；不自动覆盖输出。

## Exit codes

| code | 含义 |
|------|------|
| 0 | 成功 / ready / verified |
| 1 | 未预期内部错误 |
| 2 | 无效输入 / domain failure / blocked 状态 |
| 3 | execution attempt 失败（授权已消费） |
| 4 | 不可变输出冲突 |

## 命令

```text
data-lifecycle schema <artifact-type>
data-lifecycle validate <artifact-type> <path>
data-lifecycle source-selection generate|confirm|validate-confirmation
data-lifecycle acquisition generate|validate
data-lifecycle authorization create|validate
data-lifecycle execution run
data-lifecycle session-schedule validate|persist
data-lifecycle readiness assess|finalize|verify
```

### schema / validate

支持的 artifact type：

```text
source-selection            source-selection-confirmation
provider-capability         acquisition-request
data-access-authorization   authorization-receipt
session-schedule            snapshot-manifest
readiness-assessment        data-ready-manifest
```

`validate` 输出 `{valid, artifact_type, schema_version, sha256}`。

### source-selection

- `generate`：显式输入 research spec / data plan / data plan confirmation /
  registries / decisions；**decisions 缺失时不自动选择唯一 mapping**。
- `confirm`：绑定全部输入 artifact 输出 confirmation。
- `validate-confirmation`：严格重验 confirmation 与 selection 匹配。

### acquisition

`generate` 使用 schema 1.2；capability 与 session schedule 的 hash 必须
进入对应 artifact 绑定；unresolved 仍输出 Draft plan 并以业务非零状态退出；
不自动授权。

### authorization

`create` 不接受 paid/retry/fallback 开关；`--authorize-network` 只在 plan
确实包含 network request 时接受（local file 同理）；Draft/unresolved plan
不可授权；single-use 固定；request IDs 必须精确等于 plan。

### execution

`run` 按 plan 中的精确 provider_id 路由（确定性 adapter routing，不是
Provider selection）；不接受 `--provider`/`--interactive`/`--retry`/
`--fallback`；receipt 先持久化；执行失败后授权仍 consumed；输出 snapshot
summary（不含 raw bytes）；不自动运行 readiness。

### session-schedule

`validate` / `persist`（可选 `schema`）。不接受 Python module/import path，
不提供"自动生成工作日"命令。

### readiness

- `assess`：从 snapshot 重载 bundles；blocked assessment 仍持久化并以
  业务非零状态退出；不生成 manifest。
- `finalize`：重跑必要绑定验证；只有 Ready assessment 可创建 manifest；
  不因 assessment 自称 Ready 而信任。
- `verify`：严格重验 DataReadyManifest 与全部输入 artifact。

## Legacy FRED 边界

```text
market-validator data fred fetch [requirement.json] [--live]
```

保留为 **legacy provider diagnostic**：不使用 `DataAccessAuthorization`、
不生成 Phase 3 transactional snapshot、不能产生 `DataReadyManifest`。
其输出包含：

```json
{"formal_lifecycle": false, "data_ready": false, "authorization_artifact_enforced": false}
```

正式流程请使用 `data-lifecycle execution run`。
