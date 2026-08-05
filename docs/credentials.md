# 交互式凭据输入

`CredentialResolver` 是供应商无关的凭据入口。FRED 现在使用它，未来 DeepSeek 或其他需要 API Key 的组件也可以复用同一契约，而不在各 Provider 中重复实现弹窗和终端输入。

## 解析优先级

Resolver 严格按以下顺序查找：

1. `CredentialSpec.environment_variable` 指定的环境变量；
2. 当前进程已经取得的内存凭据；
3. 仅在调用者显式启用 interactive 时显示 tkinter 安全窗口；
4. GUI 不可用且标准输入是 TTY 时，使用 `getpass.getpass`；
5. 全部不可用时返回 `interactive_prompt_unavailable`。

环境变量存在但格式错误时会返回 `credential_invalid`，不会跳过错误值改用弹窗。非 interactive 模式缺少凭据时返回 `credential_missing`，不会为了方便自动显示窗口。

## Windows 窗口

FRED 窗口标题为 `MarketCheckAgent – 需要 FRED API Key`。窗口显示供应商和简短说明，输入框始终以 `*` 隐藏，只提供“确定”和“取消”：

- 不提供显示 Key；
- 不提供保存或记住 Key；
- 格式错误只在窗口中显示普通错误，不输出用户输入；
- 取消或关闭窗口返回 `credential_input_cancelled`；
- 窗口有有限等待时间，并在成功、取消、超时或异常后销毁 Tk root。

GUI 无法创建时，只有在显式 interactive 且当前标准输入是 TTY 的情况下才回退到 `getpass`。重定向输入、CI 或其他非 TTY 环境不会尝试终端读取。

## CLI 模式

自动化任务推荐使用进程环境变量：

```powershell
$env:FRED_API_KEY="<your-own-key>"
python -m market_validator data fred fetch examples/data_requirements/fred_wti_spot_initial.json --live
```

本地交互应用可以在缺少环境变量时明确请求窗口：

```powershell
python -m market_validator data fred fetch examples/data_requirements/fred_wti_spot_initial.json --live --interactive
```

没有 `--live` 时，无论是否写了 `--interactive`，命令都只进行 dry-run，不读取 Key、不显示窗口、不访问网络。

## Secret 生命周期

凭据使用 Pydantic `SecretStr` 包装，普通 `repr()`、`str()` 和 JSON 序列化只得到掩码。真实值只在 HTTPS Transport 构造请求的最内层显式解包。交互输入可在同一个 Resolver 实例中复用，但只存在于当前进程内存，进程退出后不会保存。

当前不接入 Windows Credential Manager。如果未来提供“记住 Key”，必须使用操作系统 Credential Manager，并另行设计授权、更新、删除和审计行为；不得退化为明文配置文件。

不要把 Key 写进 Prompt、源码、JSON、命令行参数、截图、日志或错误报告。`.env.example` 只能保留空占位符。
