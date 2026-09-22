# Codex Quota MCP

一个只读 MCP 服务，通过本机 `codex app-server` 查询 Codex/ChatGPT 的额度窗口。

## 提供的工具

`get_codex_quota` 返回低频资源遥测：剩余比例、重置时间，以及在两次汇报之间观测到的消耗速度。

默认最多每 10 分钟汇报一次；如果额度变化达到约 3 个百分点，会提前汇报。非汇报时只返回 `report_due: false`，避免每轮把资源规划重新注入 Agent。

资源规划由 Agent 自己完成，MCP 不返回行动建议、不做预测。工具描述要求 Agent 只用极小预算选择一个内部状态：`CONTINUE`、`REDUCE_EXPLORATION` 或 `CHECKPOINT_SOON`，然后立即回到主任务。

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

