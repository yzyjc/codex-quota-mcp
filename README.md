# Codex Quota MCP

一个只读 MCP 服务，通过本机 `codex app-server` 查询 Codex/ChatGPT 的额度窗口。

## 提供的工具

`get_codex_quota` 支持四种模式：`5m`、`15m`、`30m` 分别返回对应滑动窗口；`task_start` 在每次新任务或新的用户请求开始时使用一次，绕过低频门控并返回完整的 5/15/30 分钟短报。

Agent 应在每次开始处理新的用户请求时调用一次 `mode=task_start`，即使仍在同一个对话线程中；持续处理当前任务时再按需使用三种时间窗口模式。

默认最多每 10 分钟汇报一次；如果额度变化达到约 3 个百分点，会提前汇报。非汇报时只返回 `report_due: false`，避免每轮把资源规划重新注入 Agent。采样历史最多保留 64 条，以覆盖较长的滑动窗口。

资源规划由 Agent 自己完成，MCP 只提供事实，不返回行动建议、不做预测、不限定策略枚举。工具描述要求 Agent 只用极小预算读取遥测并做必要的局部决策，然后立即回到主任务。

工具可以接收可选字段 `model` 和 `context_used_percent`；未知时保持为空，不根据模型名称猜测消耗。

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

