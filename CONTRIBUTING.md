# Contributing

感谢你改进 Market Validator。项目把研究提案、用户确认、确定性执行和结果解释视为不同的安全边界；贡献不得绕过这些边界。

## 开发环境

需要 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m unittest discover -s tests
```

在 POSIX 系统中使用 `.venv/bin/python`。

## 必须遵守的原则

- AI 只能生成不可信 proposal 或解释严格回读后的结果。
- 数据处理、数学计算、统计检验和结论规则必须由确定性 Python 完成。
- 未经用户确认，不得编译或执行 AI proposal。
- 不得把 Bundle、路径、request ID、哈希、artifact 或凭据发给 AI Provider。
- 不得把相关性或回顾性结果描述为因果证据。
- 不得加入真实交易或自动下单能力。
- 不得静默填值、排序、去重、修改窗口或替换用户确认的定义。

## 测试和网络

默认测试必须完全离线。Provider、FRED 和 subprocess 行为使用 fake transport、mock 或固定响应；测试不得依赖真实 API Key、登录状态或用户机器上的绝对路径。

真实 Provider/FRED 冒烟测试不得加入默认测试套件。任何此类人工测试必须由用户单独明确授权，并使用专门的临时输出位置。

提交变更前运行：

```powershell
python scripts/release_audit.py --root .
python -m unittest discover -s tests
```

新增阶段或能力必须有清晰验收标准、失败语义、安全边界和可读测试。

## 安全和隐私

- 不提交 `.env`、API Key、认证缓存、真实响应 URL、用户目录或运行时 artifact。
- 测试凭据必须是明显的 fake/sentinel，并验证 stdout、stderr、异常和文件不包含它。
- 网络 URL 必须由代码中的受控 HTTPS 策略决定，不能由自然语言问题控制。
- 网络请求必须有超时和响应大小上限；付费 Provider 不得隐式重试。
- 不要在 Issue、日志或测试失败文本中粘贴真实凭据。

## 变更范围

保持每个 pull request 范围单一。不要在修复或加固工作中顺带加入新的统计方法、Provider、workflow 或数据源。更新相关文档和 `CHANGELOG.md` 的 `Unreleased` 部分。

## 发布

维护者在发布前应从干净 checkout 用 `scripts/build_release.py` 在两个空目录构建 wheel 和 sdist、比较实际字节哈希、在独立虚拟环境安装验证，并保存 artifact 哈希。构建必须使用明确的 `source-date-epoch`；不得用当前时间替代。许可证、仓库名和 README 语言必须已经确定，没有许可证文件时不得发布。
