# FRED 数据适配器

`FredProvider` 是项目第一个真实网络数据适配器。它只读取美国圣路易斯联储 FRED 官方 HTTPS API，不是实时交易行情源，也不会计算收益率、进行频率聚合、跨市场对齐或统计分析。

## Key 与本地配置

可从 [FRED API Key 官方说明](https://fred.stlouisfed.org/docs/api/api_key.html) 申请个人 Key。自动化任务优先从 `FRED_API_KEY` 环境变量读取32位小写字母和数字；显式 interactive 模式也可通过安全窗口或 TTY 输入。不要把真实 Key 写入 `.env.example`、JSON requirement、命令行参数或仓库文件。

Windows PowerShell 中可为当前终端临时设置：

```powershell
$env:FRED_API_KEY="<your-own-key>"
```

数据目录由 `MARKET_VALIDATOR_DATA_DIR` 指定；缺省为当前目录的 `.market_validator/data`。`.market_validator/` 已被 Git 忽略，历史快照不会被自动覆盖或清理。

## Dry-run 与 live fetch

下面的命令只做本地映射、能力、修订策略和参数检查，不要求 Key，也不联网：

```powershell
python -m market_validator data fred status
python -m market_validator data fred fetch examples/data_requirements/fred_wti_spot_initial.json
python -m market_validator data fred fetch examples/data_requirements/fred_usd_broad_initial.json
```

真实调用必须同时满足：代码构造 provider 时传入 `allow_network=True`，或 CLI 明确添加 `--live`；CredentialResolver 获得格式合法的 Key；requirement 使用已核验映射和受支持能力。仅仅设置环境变量不会自动联网。

```powershell
python -m market_validator data fred fetch examples/data_requirements/fred_wti_spot_initial.json --live
```

缺少环境变量时，本地用户可显式启用交互输入：

```powershell
python -m market_validator data fred fetch examples/data_requirements/fred_wti_spot_initial.json --live --interactive
```

不带 `--interactive` 的 live 命令保持适合自动化的非交互失败；不会突然弹窗。凭据优先级、取消和 GUI 回退行为见 [`credentials.md`](credentials.md)。

项目验收和自动化测试不运行这条 live 命令。

## 结构化错误诊断

FRED 的 HTTP 400 不能单独说明错误原因。Transport 会读取一次官方 JSON 错误 body，提取并清洗 `error_code` 与 `error_message`，再结合语义和 HTTP 状态分类为：

- `authentication_failed`：Key 格式错误、无效或未注册；
- `series_not_found`：series 不存在；
- `invalid_request`：缺少参数、日期或 `output_type` 不合法；
- `permission_denied`：权限不足；
- `rate_limited`：限频；
- `provider_unavailable`：服务端暂时不可用；
- `malformed_provider_error`：错误 body 不是可解析的 FRED JSON。

安全输出包含 `http_status`、`provider_error_code`、清洗后的 `message`、endpoint path、不含认证字段的 `public_parameters` 和 `retryable`。完整 URL、查询字符串、Key、Key 片段或哈希、未经处理的响应 body 均不会输出。例如原来的笼统 `invalid_request` 现在可显示它发生在 `/fred/series` 还是 `/fred/series/observations`、FRED error code、公开参数和是否可重试。

## 官方接口与原始数据规则

基础主机固定为 `https://api.stlouisfed.org`，实现以下官方 GET 接口：

- [`/fred/series`](https://fred.stlouisfed.org/docs/api/fred/series.html)：核对 series ID、标题、原生频率、单位、季调说明、更新时间和 notes。
- [`/fred/series/observations`](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)：显式传入 `file_type=json`、`units=lin`、`sort_order=asc`、日期范围和 `output_type`。

日频 requirement 只接受原生 `Daily` series。分页会检查 `count`、`offset`、`limit` 和最终条数，不会静默接受截断。FRED 的缺失值 `"."` 不转成 0，也不创建伪 Observation；它会成为 `provider_missing_value` warning，原始响应仍被保存。源顺序错误和重复记录会进入质量报告，不会被静默排序或去重。

## 修订值与 available_time

`latest_available` 使用当前可获得的最新修订数据。此模式可能把后来修订的历史值带入研究，因此始终产生 `latest_revision_hindsight_risk`。系统只声明这些值在实际抓取时刻可得，不虚构首次发布时间。

FRED 将两组日期用于不同目的：[observation period](https://fred.stlouisfed.org/docs/api/fred/series_observations.html) 中的 `observation_start`、`observation_end` 限制经济观测日期；[real-time period](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html) 中的 `realtime_start`、`realtime_end` 限制修订版本或 vintage 日期。两组日期不能互换。FRED 未显式设置 real-time period 时通常默认到请求当天，因此只请求当天到当天可能找不到目标序列的 vintage。

`initial_release` 使用 `output_type=4`，只返回初次发布版本。为了让 FRED 在全部历史 vintage 中寻找每条观测的首次版本，每个 observations 分页请求都显式包含：

```text
realtime_start=1776-07-04
realtime_end=9999-12-31
```

这两个边界表示 FRED 支持的完整 real-time 范围，不表示下载从1776年开始的油价。实际经济观测仍严格受 requirement 的 `observation_start` 和 `observation_end` 限制；例如示例仍只请求 `2020-01-01` 至 `2024-12-31`。第一页和后续页复用完全相同的 `output_type` 与 real-time period，只有 `offset` 随分页变化。

每条返回记录的 `realtime_start` 被视为首次可得日期；FRED 通常没有精确小时和分钟，因此系统保守设为该日 `23:59:59.999999 UTC`，标记 `availability_precision=date`，并保存日末假设。`observation_time` 则是 observation date 当日 `00:00:00 UTC`，标记 `observation_precision=date`；它不是实际发布时间。

`as_of_date` 尚未实现，会明确失败。Provider 不会替用户选择修订策略。任何未来跨市场防泄漏判断必须比较 `available_time` 和 ResearchSpec 的 `information_cutoff`；本轮不执行该对齐。

## 已核验系列的含义

- `global.crude_oil.wti_spot` → `DCOILWTICO`：WTI 现货参考价格，不是原油期货。
- `global.crude_oil.brent_spot` → `DCOILBRENTEU`：Brent 现货参考价格，不是原油期货。
- `us.dollar.nominal_broad_index` → `DTWEXBGS`：名义广义美元指数，不是 DXY。
- `fx.usd_cny.reference_rate` → `DEXCHUS`：一美元对应多少人民币，单位方向不能倒置。

连续原油期货示例 `global.crude_oil.continuous_front` 保持原有经济含义，没有映射到 FRED 现货。每个映射的官方系列页面保存在注册表的 `verification_source_uri` 中。

## 快照产物

一次成功 live fetch 只有在以下产物全部持久化后才返回成功：

```text
.market_validator/data/
  raw/fred/          # series 与每一页 observations 的原始响应字节
  bundles/fred/      # 标准化 DataBundle
  manifests/fred/    # 路径、SHA-256、公开参数和修订策略
```

写入使用同目录临时文件和原子替换，并在目标已存在时拒绝覆盖。manifest、路径、文件名、DataBundle 和来源 URI 均不得包含 Key 或带 Key 的完整查询 URL。更完整的信任边界见 [`provider_security.md`](provider_security.md)。
