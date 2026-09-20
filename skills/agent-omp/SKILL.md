---
name: agent-omp
description: "Command dictionary for driving omp (oh-my-pi) as a controlled agent. Lists omp's slash commands and interaction facts needed by agent-orchestrator: /new, /clear, /compact, /shake, /exit, skill invocation, /model, /session, /usage, /mcp. Use when orchestrating an omp instance."
---

# omp 指令字典（供 agent-orchestrator 操控 omp）

herdr kind: `omp`

发布结果后、续轮或退出前，读取[宿主生命周期](references/lifecycle.md)：原生身份、产品交互、后台进程和收尾证据。

## 关键 slash commands

| 命令 | 用途 |
|---|---|
| `/exit` / `/quit` | 退出会话 |
| `/new` | 新会话 |
| `/fresh` | 全新会话 |
| `/clear` | 清空当前会话消息 |
| `/compact` | 压缩上下文 |
| `/shake` | 剥离重工具结果回收 token（`/shake images` 只删图片）|
| `/skill:<name>` | 调用已安装 skill |
| `/model` | 切换模型 |
| `/session` | 会话管理（info/delete/pin）|
| `/usage` | 查看 token 用量 |
| `/mcp` | 管理 MCP server |
| `/todo` | 任务清单 |

## 交互事实

- 输入框直接打字，Enter 提交；`#` 开头触发 prompt 动作，`/` 开头触发命令，`!` 执行 bash，`$` 执行 python。
- 完成一个 turn 后回到输入态（herdr 可能报 `idle` 或 `done`）；任务结果仍以匹配本轮身份的回传文件为准。
- 退出命令 `/exit`（同 `/quit`）。
- v18.2.3 实测运行中 `Esc` 可取消工具调用；取消后可能仍回传描述中断的 `error`。控制方仍记录为取消，并核对实际子进程与部分文件。
- 单次会话可通过 CLI `--approval-mode=always-ask` 请求原生工具审批。实测弹窗为 `Approve / Deny`，方向键选择、Enter 确认；每次读取实际命令后按已有授权处理，不修改全局审批配置。
