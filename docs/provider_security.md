# 数据提供商安全边界

## 显式联网授权

网络访问默认关闭。FRED 的 `status`、`providers` 和不带 `--live` 的 `fetch` 都只检查本地配置、注册映射和公开参数。环境中存在 Key 不等于获得联网授权；库调用必须传 `allow_network=True`，CLI 必须由用户添加 `--live`。

自动化测试使用依赖注入的 FakeTransport 和可注入 sleep，不访问真实 FRED，不等待真实退避，也不读取用户认证文件。

## HTTPS 与主机约束

生产基础地址不可配置，固定为 `https://api.stlouisfed.org`。Transport 只允许两个已知路径和 HTTPS 443，拒绝 HTTP、其他主机、其他端口和 3xx 重定向。这样可以避免 Key 被重定向或发送到调用者提供的未知主机。

HTTP 429、500、502、503、504 最多重试两次；400、401、403、404、格式错误和数据验证错误不重试。错误只返回领域分类和经过删减的说明，不包含 Key、完整查询 URL或服务端响应正文。

FRED 单次请求默认超时为 15 秒，响应上限为 32 MiB。最多两次重试表示最多三次实际 HTTP 请求，而且只适用于上面列出的临时状态；连接错误和超时不会隐式重试。这样可以避免无界等待和无界响应读取。

可选的 DeepSeek 计划提案路径只允许受控的官方 `https://api.deepseek.com`（或其 `/v1` 路径），拒绝凭据化 URL、HTTP、其他主机、端口、查询参数和重定向出来的错误内容。一次 `propose-plan` 只发出一个请求，不做自动重试；默认超时 120 秒，响应上限 2 MiB，输出 token 上限固定为 8192。市场问题只能成为请求正文中的不可信用户文本，不能控制 URL、Header、工具或重试次数。

## Key 生命周期

环境变量或显式交互输入得到的 FRED Key 都以 `SecretStr` 在当前进程内流转，只在生产 Transport 构建请求时显式解包并加入查询参数。公共参数、`DataSourceMetadata`、异常、状态 JSON、dry-run JSON、manifest、文件名和快照内容都不允许包含 Key。存储层还会独立拒绝名称类似 `api_key`、`token`、`secret`、`password` 或 `authorization` 的参数，降低上层误用风险。当前不持久化交互凭据；未来如需保存，只能另行接入操作系统 Credential Manager。

`.env.example` 只保留空占位符，`.env`、常见密钥文件和 `.market_validator/` 均被 `.gitignore` 排除。代码不读取浏览器、Codex 或其他无关认证缓存。

## 来源与不可变快照

Provider 只接受注册表中标记 `verified=true`、有 `verified_on` 和 HTTPS `verification_source_uri` 的 FRED 映射。每次 live fetch 保存 series 原始响应、全部 observations 分页原始响应、各文件 SHA-256、DataBundle 和 manifest。目标路径存在时写入失败，不会覆盖历史快照；持久化失败时整次 fetch 报错，不会返回无法追踪来源的成功结果。

## 为什么暂缓 Tushare

当前 [Tushare 官方文档](https://tushare.pro/document/1?doc_id=130) 和其 [官方 Python 客户端源码](https://github.com/waditu/tushare/blob/master/tushare/pro/client.py) 所示基础地址涉及 `http://api.tushare.pro`。在官方安全传输契约获得确认前，项目不实现 TushareProvider、不增加 `TUSHARE_TOKEN`、不调用该服务，也不猜测一个未确认的 HTTPS 端点。

这一决定不代表对服务内容或质量的判断，只是当前项目拒绝让认证 Token 可能通过明文 HTTP 传输。
