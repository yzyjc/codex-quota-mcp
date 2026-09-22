# Codex Quota MCP

一个只读 MCP 服务，通过本机 `codex app-server` 查询 Codex/ChatGPT 的额度窗口。

## 提供的工具

`get_codex_quota` 支持四种模式：`5m`、`15m`、`30m` 分别返回对应滑动窗口；`task_start` 在每次新任务或新的用户请求开始时使用一次，绕过低频门控并返回完整的 5/15/30 分钟短报。短报同时包含 primary 和 weekly 的重置时间及窗口长度。

Agent 应在每次开始处理新的用户请求时调用一次 `mode=task_start`，即使仍在同一个对话线程中；持续处理当前任务时再按需使用三种时间窗口模式。MCP 本身无法接收 Codex 的任务生命周期事件，因此这一调用由 Agent 按工具描述执行，不是后台自动触发。

默认最多每 10 分钟生成一次普通资源短报；如果 primary 额度较上次资源短报变化达到约 3 个百分点，或 primary 重置时间发生变化，会提前生成。`task_start` 不受这个门控影响。非汇报查询只返回 `report_due: false`，避免 Agent 每轮重复展开资源规划。采样历史最多保留 64 条；这是数量上限，不保证在高频调用时覆盖完整 30 分钟。

资源规划由 Agent 自己完成，MCP 只提供事实，不返回行动建议、不做预测、不限定策略枚举。工具描述要求 Agent 只用极小预算读取遥测并做必要的局部决策，然后立即回到主任务。

采样发生在 MCP 被调用时，不是后台连续采样；因此 5/15/30 分钟模式返回的是对应时间窗口内已有采样点之间的观测值，实际覆盖时长由 `observed_minutes` 给出，不保证恰好覆盖完整窗口。它不是每个 token 或每个 turn 的精确遥测。工具可以接收可选字段 `model` 和 `context_used_percent`；未知时保持为空，不根据模型名称猜测消耗。

## 可靠性

- App Server 请求默认 15 秒超时，可通过 `CODEX_APP_SERVER_TIMEOUT_SECONDS` 调整。
- App Server 的 stderr 会在启动失败、超时或无响应时作为截断诊断信息返回。
- App Server 无论成功、失败还是超时都会被清理，避免遗留子进程。
- 历史文件使用跨进程锁和原子替换写入，减少多个 MCP 请求同时保存时互相覆盖的风险。

## 模拟测试

仓库包含一个假的本地 App Server 和可执行测试，覆盖四种模式、滑动窗口、低频门控、超时、stderr 诊断和历史文件边界。测试不会访问真实 Codex 账户：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

GitHub Actions 会在 push 和 pull request 时自动运行同一套测试。

## 启动

```powershell
python D:\AI工具\CodexQuotaMCP\server.py
```

MCP 客户端应以 stdio 方式启动它。Codex Desktop 的 MCP 配置示例：

```toml
[mcp_servers.codex_quota]
command = "python"
args = ["D:\\AI工具\\CodexQuotaMCP\\server.py"]
```

如果 `codex` 不在 PATH 中，可以设置 `CODEX_COMMAND` 为 `codex.exe` 的绝对路径。

## 安全边界

- 只调用 `account/rateLimits/read`。
- 不读取或保存 token。
- 不调用写入、发送消息或执行 shell 的 App Server 方法。
- 每次查询启动一个短生命周期 App Server 进程，便于 MVP 阶段隔离状态。

