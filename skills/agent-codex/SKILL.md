---
name: agent-codex
description: "Drive Codex CLI through herdr or tmux, or run a bounded task with codex exec. Use Codex-specific skill discovery, sandbox scope, exact session recovery and interruption while preserving the agent-controlled round protocol."
---

# Codex 指令字典

用于实际启动/操控 Codex CLI；主编排流程见 [agent-orchestrator](../agent-orchestrator/SKILL.md)，回传语义见 [agent-controlled](../agent-controlled/SKILL.md)。本地 CLI 帮助核对：**0.155.1（2026-09-23）**；exec 恢复实验为 0.154.0，后续原生监督实验为 0.155.0，不能据此宣称 0.155.1 全行为通过。不同版本先查本机 `codex --help` 和对应子命令帮助。

## 选择入口

| 任务 | 入口 |
|---|---|
| 持续交互、原生审批、跨终端协作 | `codex`；herdr 使用 `--kind codex`，tmux 直接传可执行路径和 argv |
| 有界脚本任务、可收集事件的自动化 | `codex exec --json`，见 [exec 与恢复](references/native.md#非交互执行与恢复) |
| 恢复已知交互会话 | `codex resume <SESSION_ID>`；提交前核对 cwd、权限和当前轮 |

`--no-alt-screen` 便于终端保留滚动记录，不改变协议或审批。保留用户配置的模型/provider，不自动切换模型、清空配置或启用 `--dangerously-bypass-approvals-and-sandbox`。启动 argv、实际模型、原生 session ID、cwd 和额外可写目录记入控制方凭据。

## Skill 与写入范围

当前官方用户级目录是 `~/.agents/skills/`，项目级是从 cwd 到仓库根的 `.agents/skills/`；支持目录软链接。用 `bash install.sh --target codex` 安装全部六个兄弟 Skill，可用 `CODEX_SKILLS_DIR` 覆盖。既有 `~/.codex/skills` 是否兼容取决于版本，不把它作为新安装默认目录。

提示中用 `$agent-controlled` / `$agent-orchestrator` 显式选择；必要时直接读确切 `SKILL.md`。不要套用 omp 的 `/skill:<name>` 或 OpenCode 的 `skill({name: ...})`。本轮完整契约仍应在提示中，不依赖 Skill 自动选择成功。

**回传目录通常在 cwd 外。** 使用 workspace-write 的目标需要精确 `--add-dir <round-root>` 才能写入；只读业务也需要一个可写报告通道。启动前规划长期会话的专用 round-root，避免后续每轮目录更换后无权报告。不要把整个 HOME 或项目父目录设为可写来解决路径问题。`--add-dir` 不应被假定能把 read-only 变成可写；以实际权限为准。

## 交互与收尾

- 当前常用入口 `/help`、`/skills`、`/status`、`/permissions`、`/resume`、`/quit`（`/exit`）；以当前菜单为准。
- 工作中通常按 **Esc** 中断；先看焦点是否在弹窗/选择器。Ctrl+C 的含义随输入状态变化，不盲目连按。
- 普通澄清走 blocked 文件；已出现的原生审批仍按实际界面处理。不能为了让测试通过而关掉审批。
- `idle`、最终文字和 exec 退出码都不是本轮成功证据；验证精确 `job_id`、`round_id` 和 result 文件，再独立验收产物。
- 原生子 agent、后台命令及另起的 CLI 都受接收深度约束。最深层本地执行；父层汇总委托文件，并确认自己拥有的写入者结束后才发布不可变结果。
- Esc 中断对话不保证停止 unified_exec 的后台终端。用 `/ps` 核对；`/stop` 会停止**当前会话全部后台终端**，仅在这些终端均属于本次取消范围时使用。混有其他任务时按精确工具 session/PID 停止目标，不能扩大范围。中断后核对真实子进程和迟到写入；不关闭用户其他 Codex 会话或共享 app-server daemon。

详细命令、exec 输出与权限限制见 [原生接口参考](references/native.md)。
