# Security Policy

## 当前支持范围

项目尚处于 `0.1.x` Alpha 阶段。发布前只维护当前主线；尚未承诺旧版本的长期安全支持周期。

## 报告安全问题

公开仓库创建后，应先启用 GitHub Private Vulnerability Reporting 或指定私密安全联系渠道。请不要在公开 Issue 中披露 API Key、Authorization header、未清洗的 Provider 响应、用户路径或可利用细节。

当前尚未配置公开安全联系地址，这是正式发布前需要仓库所有者完成的设置。若怀疑凭据已经暴露，应立即在对应 Provider 撤销凭据，而不是等待代码修复。

## 安全边界

- AI Provider 只能生成不可信 `MarketValidationPlanProposal`。
- `--allow-network` 只许可一次 proposal 网络请求，不代表确认、编译或执行授权。
- AI 不接收 Bundle、路径、来源身份、哈希、artifact 或凭据内容。
- Provider 原始输出必须通过现有严格解析器；未知字段、代码围栏和额外文本均被拒绝。
- DeepSeek proposal 请求只允许受控的官方 HTTPS 地址，单次请求有超时、token 上限和响应字节上限，并且没有隐式重试。
- FRED 使用官方 HTTPS 主机、拒绝重定向、限制响应大小，并只对明确的临时状态码进行最多两次重试。
- 凭据只保存在当前进程内存，不应进入日志、异常、URL 元数据、文件名或持久化产物。
- workflow 只信任严格加载、哈希校验和回读后的确定性结果。

## 不在安全保证内的事项

- Proposal SHA-256 证明内容一致，不证明作者身份或用户身份。
- Manifest SHA-256 不是数字签名；外部独立保存的哈希才可检测协调替换。
- 模型输出本身不确定，也可能错误、拒绝或随 Provider 更新而改变。
- 金融研究结果不是投资建议、因果证明或盈利保证。

## 发布前检查

```powershell
python scripts/release_audit.py --root .
python -m unittest discover -s tests
```

没有明确许可证、私密漏洞报告渠道和干净 tracked-files 审计时，不应发布正式版本。
