# kumi 使用指南

先按[部署与接入说明](INTEGRATION.md)安装六个 skills，并确认要使用的 CLI 能独立运行。以下自然语言请求交给**调度 agent**执行；它应加载 `agent-orchestrator`，再按目标读取宿主字典。日常使用不需要 E2E runner。

## 第一次只读派单

在准备分析的项目中打开调度方会话，给出目标、范围、验收和传输选择：

> 使用 agent-orchestrator，让 omp 只读检查这个项目的测试结构。仅阅读源码、测试和 README，不修改文件、不安装依赖。回传三个按优先级排序的问题，每项提供文件位置和理由。使用 tmux。你同时阅读 README，最后独立核对这些发现。

把目标换成自己的已配置 agent。宿主的 skill 调用语法不同，直接按名称要求加载即可；不要把某个宿主的 slash 命令原样用于其他宿主。

开始后，调度方应提供以下信息，便于查看和恢复：

- 实际任务目的、目标类型及工作目录。
- `run.json` 和当前 `request.json` 的绝对路径。
- 实际 session、pane、workspace/tab 身份及准确的查看命令。
- 当前进展、下一次需要用户决定的事项，以及最终验收依据。

没有加载 skill 时，先检查安装目录和会话发现情况。不要用复制整份项目报告代替加载运行包。

## 页面、名称和工作目录

herdr 中默认每个子任务新建一个 **workspace（space）**，初始 tab 使用任务标题；`--no-focus` 保留调度方当前页面。可以直接指定：

> 让 Hermes 在新 space 检查上传错误处理，名称“检查上传错误处理”。

> 让 Codex 在当前 workspace 的新 tab 验证兼容性，名称“验证上传兼容性”。

> 切到这个子任务的页面，让我看一下过程。

只有明确要求并排观察时才分屏。同类任务可加短后缀区分，内部唯一 ID 与可读标题分开。续轮通常复用原页面，不按名字猜测或重复创建资源。

从外部环境启动的 herdr 会话与当前 GUI 页面未必相同，要使用调度方给出的实际连接命令；独立会话的形式是 `herdr session attach <实际会话名>`。tmux 应提供带实际 socket/server 的连接命令，默认只读查看。关闭查看窗口不等于取消任务，需要接管输入时先让调度方暂停对该终端的输入。

**新页面不隔离磁盘文件。** 两个只读任务可共享项目；两个写任务应使用独立工作树/项目副本，或明确互不重叠的文件范围。任务目录隔离与宿主原生存储隔离也不同，Hermes 新任务的私有 profile 按[存储隔离配方](skills/agent-hermes/references/isolation.md)处理。

## 并行工作与权限处理

可以这样分工：

> 让一个 agent 检查后端，一个检查文档；都只读。你继续准备独立验收清单，定期检查两个任务的权限和追问，有问题及时处理。

调度方在提交前建立当前轮的 watch 和受管观察器，确认首次成功检查并登记监督责任。子任务开始后，继续推进不依赖结果的工作。每段工作前通过 `supervision.py` 查看允许的工作时长和下次复查时间；只有确实依赖子结果且没有独立工作时，才进入有界等待。

观察器默认约 15 秒检查当前轮文件、原生状态和终端画面，将变化和疑似权限弹窗记录成事件。调度方还要读取事件、核对实际 UI 并执行具体处理；后台事件文件不会自动唤醒一个已经结束的调度会话。

| 遇到的情况 | 处理方式 |
|---|---|
| 原生终端权限弹窗 | 核对具体操作和已有授权，在真实界面处理；确实需要用户决定时转达，继续其他可做的工作 |
| 子 agent 发布 `blocked` JSON | 保留当前轮结果，取得回答后生成新的轮次和结果路径 |
| 已询问用户，尚未答复 | 记录 `waiting_user`，恢复后继续可见，避免重复提问 |
| 观察器失活、检查过期、未知 UI | 先核对并恢复监督，再继续无关工作或提交输入 |

处理原生弹窗后继续当前轮业务，不重新发送任务。将事件记为 handled 不会批准宿主弹窗；推进读取游标也不会清空未解决事项。30 秒响应是当前目标，长时、多次审批下的稳定时效仍待实测。

操作细节见[监督检查点](skills/agent-orchestrator/references/supervision.md)、[生命周期](skills/agent-orchestrator/references/lifecycle.md)和[巡视与待办](skills/agent-orchestrator/references/efficiency.md)。

## 追问、恢复和取消

子 agent 通过独立 JSON 文件返回 `success`、`blocked` 或 `error`。每轮有新的 `round_id` 和 `result_path`，同一 job 同时只有一个活动轮次；有效结果发布后不覆盖。

| 意图 | 可以对调度方说 |
|---|---|
| 回答追问 | “按方案 B 继续这个任务，保留上一轮结果并生成续轮。” |
| 验收失败后返工 | “用这份失败证据让作者修正，重新交付并独立复验。” |
| 换会话恢复 | “恢复 `/实际路径/run.json`，先核对待办、终端和提交状态，再继续。” |
| 暂停/取消 | “停止这个子任务，核对原生后台是否结束，保留结果和证据，清理本任务拥有的资源。” |

恢复先读持久 run，核对当前轮、原生会话、观察健康和待办。发送超时、控制器中断或回执缺失都不能证明任务没有收到；`uncertain` 必须按实际证据核对，不能盲目重发。不要把旧提示词直接发给另一个 pane，也不要依据当前焦点猜测资源身份。

取消不能只看一个 `idle` 状态或一次 Esc；例如某些宿主还有后台子任务或自动续跑机制。按目标字典确认收尾，只处理本任务拥有的终端资源。恢复与状态命令见[持久 run](skills/agent-orchestrator/references/runs.md)、[提交回执](skills/agent-orchestrator/references/submission.md)。

## 如何判断真正完成

子 agent 的 `success` 是交付声明，调度方还应：

1. 校验当前轮身份、结果格式、文件及声明范围。
2. 固定源码快照，在独立目录运行与任务相符的验收；构建模式、测试环境及产物也纳入检查。
3. 记录接受或拒收及其证据，确认原生后台不再写入后再标记完成。

作者交付、验证轮结果与原生宿主收尾分别记账。验证者也可能漏测；关键任务应使用独立预期值、失败样例或负对照，不能只重复作者给出的“测试通过”。详见[作者／验证／返工](skills/agent-orchestrator/references/task-workflow.md)、[交付快照](skills/agent-orchestrator/references/delivery.md)、[完成条件](skills/agent-orchestrator/references/completion.md)。

## 怎样减少 token 和重复工作

把适合独立推进的工作拆出去，每个子任务说明目标、范围、验收、必要输入和已知事实。优先给文件路径与简短结论，不复制整段对话；小任务的调度成本可能高于收益。

正式工具提供 `prepare --task-packet … --brief`、事件去重、按游标读取和按需恢复。`--brief` 减少返回给调度方的重复任务正文，子 agent 仍收到完整契约。日常查看可用 `runs.py recover --run "$RUN_FILE" --summary --limit 10`：任务、watch、错误和两类待办独立分页，保留全局计数及异常提示；轮次和完成条件详情按需读取。默认完整恢复仍只分页待办。摘要限制行数，不是严格的字节/token 上限，也仍扫描完整状态。分页和检查方法见[恢复说明](skills/agent-orchestrator/references/runs.md)。

重复巡视较大 run 时，可选 `recover --delta`：复用元数据未变的源文件内容，任务/watch 按变化返回，权限待办、等待用户、错误及总数每次仍返回。首次回复提供 `cursor_path` 与 `next_cursor`，收到并处理后以 `--cursor … --since …` 续读；标识不匹配、缓存损坏或筛选变化会重新建立基线。此模式在 run 内写入私有游标，仍扫描目录、验证元数据和重新计算过期，并有游标自身的读写开销。详见[增量恢复](skills/agent-orchestrator/references/recovery-delta.md)；继续独立工作仍须通过监督检查。

用量需读取原生证据，缺失数据保持 `null`。Codex/omp JSONL、OpenCode SQLite/export 和 Hermes 可选主循环回执各有来源及归属条件；不能用日志长度估算 token，也不能把 Hermes 的累计变化直接当成某轮成本。

评估整个方案时，要同时计入调度方、子 agent、后台调用、重试和返工，并用相同任务与验收比较单 agent。当前尚无净 token 节省结论。账本、来源绑定和覆盖缺口见[用量工具](skills/agent-orchestrator/references/efficiency.md)、[来源审计](skills/agent-orchestrator/references/usage-sources.md)与[Hermes 支持层次](skills/agent-orchestrator/references/hermes-support.md)。

## 常见问题

| 现象 | 先检查 |
|---|---|
| 找不到 skill | 安装到了哪个宿主/profile、软链接是否有效、是否需要新开会话；`skip` 的旧目录是否仍是旧副本 |
| 有 skill，但无法启动目标 | CLI 是否可执行、既有认证/模型是否可用、终端 socket 和后台进程权限是否允许 |
| 默认装完后 Codex/OpenCode 看不到 | 无参数只安装 omp 和 Hermes；显式指定对应 `--target` |
| 页面不是预期位置 | 实际 herdr session 和父资源 ID；`--no-focus` 不会自动切过去 |
| 子 agent 一直等待 | 同时查结果 JSON、原生状态和实际画面；可能是权限、追问或观察器已到期 |
| 结果为 success，仍无法 completed | 检查独立验收和原生后台收尾证据 |
| Hermes 用量为空或不完整 | skill 安装不启用插件；检查实际 profile、启用时间和覆盖范围，辅助计量缺失不能补成零 |

开发者若要复现确定性小项目，见[无模型 E2E 演练](tests/E2E.md)；它验证终端和控制流程，不替代真实宿主长期验收。
