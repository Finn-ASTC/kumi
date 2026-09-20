---
name: agent-opencode
description: "Drive OpenCode with oh-my-opencode/oh-my-openagent (OMO) or in explicit --pure mode. Select the launch profile, use version-appropriate commands, manage plugin continuation and child tasks, and return controlled results through herdr or tmux."
---

# OpenCode / OMO 指令字典

本项目当前用户环境中，**`opencode` 默认运行 OMO，`opencode --pure` 才禁用外部插件**。这不是两个 herdr kind：两种模式都使用 `--kind opencode`。`omp` 是另一个独立工具，不能与 OMO 混用。

## 先选择启动模式

| 用户意图 | 启动 argv | 操作参考 |
|---|---|---|
| OpenCode、OMO、oh-my-opencode、oh-my-openagent；未要求纯净模式 | `opencode` | [原生交互](references/native.md) + [OMO 行为](references/omo.md) |
| 纯 OpenCode、pure、无外部插件 | `opencode --pure` | [原生交互](references/native.md) |

以用户当前机器的配置为准；其他机器不能仅凭命令名推断已安装 OMO。已存在的目标先检查实际 argv、当前 agent、插件/命令列表和精确 session。不要为了避开 OMO 机制而自行切换到 pure，也不要把指定 pure 的任务落到默认 OMO 实例。

## 再选择角色与工作流

启动 profile、主角色、模型 variant 和 OMO 工作流是不同层次。**pure 默认入口是 Build，可切 Plan；本机 OMO 默认是 Sisyphus，另有 Prometheus 规划、Atlas 执行计划。** OMO 默认会隐藏原生 build/plan，不能照搬 pure 的两角色切换。Hephaestus 是否注册取决于模型等条件，本机当前未注册。

需要指定角色、切换模式、规划后执行或使用 ulw/team/Goal 时，先读[模式与角色选择](references/modes.md)。默认通过 Tab / Shift+Tab 或 `Ctrl+X` 后按 `a` 选择当前可见主角色；`Ctrl+T` 切的是模型变体。等待目标就绪，切换后核对实际名称、模型和根 session，不按固定次数盲切，也不把 `/agent` 当作标准命令。

**Plan/Prometheus 可能不能写协议 result.json。** 尤其 Prometheus 有 `.omo` Markdown 写入 hook；指定规划角色前核对合法回传通道，失败时保留原生输出并报告限制，不以 bash 绕过或伪造结构化成功。详细选择与回传边界见模式参考。

本地核对基线为 OpenCode **1.18.29**、`oh-my-openagent` **4.19.4**（兼容名称 oh-my-opencode）。两种模式已完成真实模型测试，覆盖多轮回传、输入边界、深度、取消、报告修复和跨工具嵌套，OMO 另测了两个原生后台任务。pure 原生审批等待造成过监控超时，一条嵌套任务虽回传 success 仍超过时限；未逐一验收全部角色、命令或 Goal/team 配置。README、旧版教程与运行代码冲突时，使用当前实际注册的命令，详情见 [OMO 版本差异](references/omo.md#版本差异与来源)。

## 传输与模式记录

使用主 Skill 的[传输配方](../agent-orchestrator/references/transports.md)。Bash 示例：

```bash
# 默认 OMO
herdr --session "$SESSION" agent start "$AGENT" --kind opencode --pane "$PANE"
# 明确要求 pure 时使用这一条；不与上一条重复启动
herdr --session "$SESSION" agent start "$AGENT" --kind opencode --pane "$PANE" -- --pure
```

tmux 仍使用分开的 argv：`/usr/bin/env "$AGENT_BIN"` 或 `/usr/bin/env "$AGENT_BIN" --pure`。不要把 `opencode --pure` 当成一个可执行文件名。若要指定 `--agent`，先确认当前注册名称；内部角色可能有显示名或被配置禁用。

在控制方启动/提交凭据中保留 `kind=opencode`、`opencode_mode=omo|pure`、实际可执行路径与 argv、OpenCode/OMO 版本、原生 session ID、当前角色的配置键/实际注册名、实际模型/variant、启用的工作流，以及结果路径。切换角色后更新凭据；启动 profile 后续轮次必须继承。`resources.json` 的 transport mode 仍是 insider/isolated/tmux，不能用 omo/pure 或 primary/subagent 替换。

**pure 会禁用外部 herdr 集成插件。** 本机 herdr 的 agent-state 和 TUI session-selection 都以外部插件安装；pure 下不能假定它们仍上报准确状态/原生 session。核对屏幕、进程、当前轮文件；启动或状态检测不可用且任务尚未提交时，可使用 tmux。不要为恢复状态探测而去掉 `--pure`，也不依赖 `agent prompt --wait` 单独判定完成。

## 受控任务的收尾

普通工作直接提交完整 [agent-controlled 契约](../agent-controlled/SKILL.md)。不要自动添加 OMO 模式触发词或启动持续目标；需要规划、持续执行或团队任务时才使用对应入口，并把本轮边界交给目标。

OMO 收尾前必须核对本轮 todo、后台子任务与续跑状态：子任务仍可写入时不能提前发布最终结果。父 agent 汇总委托产物，最后只发布一次本轮 JSON；插件的后台完成通知、Goal 状态、侧栏进度、`idle` 均不能替代它。深度限制同样适用于 OMO 内部派生，不能通过 `task` 或 team 工具绕过最深层的本地执行要求；这仍是合作约束，详见 [OMO 生命周期](references/omo.md#受控轮次后台任务与停止)。

普通澄清走 blocked 文件；已经弹出的原生 Question/permission 界面按实际 UI 处理。1.18.29 工作中第一次 Esc 显示 `esc again to interrupt`，需要及时再按一次完成取消；OMO 与 pure 都已实测单次失败、双次通过。有 OMO 自动续跑时还要处理其停止机制及后台任务，再确认没有继续写入。仅结束本轮拥有的资源，保留用户既有计划、会话和团队状态。
