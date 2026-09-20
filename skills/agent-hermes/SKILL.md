---
name: agent-hermes
description: "Command dictionary for driving hermes (Hermes Agent) as a controlled agent. Lists hermes's slash commands and interaction facts needed by agent-orchestrator: /exit (alias /quit), /clear, /compact, /model, /help. Use when orchestrating a hermes instance."
---

# hermes 指令字典（供 agent-orchestrator 操控 hermes）

herdr kind: `hermes`

新建受控任务时，先读取[每次运行的存储隔离](references/isolation.md)：在保留既有模型、凭据路由、审批与扫描器的前提下，优先使用独立 home，并仅在该副本关闭自动后台审阅和 curator。已有会话使用原 home + 原生 ID 恢复，不搬迁数据库。

发布结果后、续轮或退出前，读取[宿主生命周期](references/lifecycle.md)：原生身份、后台审阅、私有 skill 写入和收尾证据。

日常派单、监督、回传和恢复独立于计量。需要用量审查时，先按[分层支持与状态检查](../agent-orchestrator/references/hermes-support.md)区分宿主接口、插件前置和实际回执，再读取[可选用量插件](../agent-orchestrator/references/hermes-hooks.md)。主循环和可选辅助计量分开，未覆盖部分保持未知。

## 关键 slash commands

| 命令 | 用途 |
|---|---|
| `/exit` | 退出（别名 `/quit`，支持 `--delete`）|
| `/clear` | 清空上下文（`--yes` 跳过确认）|
| `/compact` | 压缩上下文 |
| `/model <m>` | 切换模型（`--global` 永久 / `--once` 单次 / `--session` 会话级）|
| `/help` | 帮助 |

## 交互事实

- hermes 是交互式 chat TUI，直接输入 + Enter 提交。
- 大量子命令在 CLI 层（`hermes chat` / `hermes mcp` / `hermes sessions`），TUI 内用 slash command。
- 退出 `/exit`（= `/quit`）。
- 运行中按一次 `Ctrl+C` 中断；本地 v0.20.5 实测会停止前台 terminal 子进程。连续再按可能强制退出，空闲时也可能退出，先检查实际状态；中断后仍须核对残留进程和部分文件。
