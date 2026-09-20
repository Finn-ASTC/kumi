# OMO：oh-my-opencode / oh-my-openagent

2026-09-17 实测：即使普通聊天不调用工具，插件仍会更新 cwd 下 `.omo/run-continuation/<当前 session ID>.json`。不要承诺整个宿主零写盘。验收时记录这些实际运行状态变化，并独立检查业务文件和既有协议结果；不能把整个 `.omo` 排除，因为其中也可能有用户计划和需要保护的项目状态。

本参考面向 **OpenCode 上的 OMO**。本地安装包为 `oh-my-openagent@4.19.4`，仍提供 oh-my-opencode/omo 兼容 CLI 名称。不要与 `omp`、插件的 Codex Light 版或其命令混淆。使用前同时读取[原生交互](native.md)。

## 配置与能力确认

OpenCode 的插件声明在它的配置中；本机 `opencode.jsonc` 声明 `oh-my-openagent@latest`。OMO 当前统一配置是 `~/.omo/omo.jsonc`，平台段为 `"[opencode]"`，不是凭空新建 `platforms.opencode`。项目及祖先目录 `.omo/omo.jsonc`/`.json` 也可能参与合并；旧 `oh-my-opencode.jsonc`、`oh-my-openagent.jsonc` 等属于兼容/迁移来源。不要为一次编排重跑安装、迁移配置或把旧教程结构写回全局文件。

启动观察区分三层：OpenCode 宿主进程、OMO 服务端功能、OMO TUI 侧栏。侧栏显示/隐藏不能独自证明服务端插件开关；配置声明和缓存文件存在也不能证明每个功能已在当前 session 启用。

按需核对当前角色、命令表、原生工具 schema、disabled_agents/commands/hooks/skills、goal、default_mode、team_mode、tmux 等实际配置。模型可用性也会影响角色注册和自动切换，配置中的角色名称不等于该角色一定可运行。只提取相关字段，避免输出 provider 凭据或整份配置。

## 角色与调度

主角色切换、pure 的 Build/Plan 与 OMO 替换关系、本机实际注册清单、模型兼容性及规划角色回传限制见[模式与角色选择](modes.md)。下面列的是职责，不代表这些名称都会出现在 Tab 主角色列表中。

| OMO 角色键 | 用途 | 控制方选择原则 |
|---|---|---|
| `sisyphus` | 主协调者、任务执行与分派。 | 默认通用入口；当前会话可以被用户选择覆盖。 |
| `hephaestus` | 深度自主执行。 | 模型兼容 hook 可能限制非 GPT 模型，不擅自换模型或声称任意配置都可用。 |
| `prometheus` | 访谈、范围澄清和计划生成。 | 用于规划；不能因为未产生代码就判任务失败。写入受 `.omo` Markdown hook 限制，普通 JSON 回传可能无法完成，不能直接宣称兼容整个受控协议。 |
| `atlas` | 接手计划并组织执行。 | `/start-work` 的首选角色，未注册时运行代码回退 sisyphus。 |
| `metis`、`momus` | 计划咨询、计划评审。 | 子任务结果要被主角色消费和核验。 |
| `oracle` | 架构、诊断和复杂推理咨询。 | 通常通过 task 指定实际可用子角色。 |
| `librarian`、`explore` | 文档/外部资料检索、代码库快速探索。 | 只读任务可并发；不据此授权外部发布或无关写入。 |
| `multimodal-looker` | 图像等多模态分析。 | 取决于实际模型和工具能力。 |
| `sisyphus-junior` | 按类别派发的执行者。 | 类别与具体模型不同，不把类别名当作可执行文件。 |

配置键和 TUI 显示名可能不同，例如 `Sisyphus - ultraworker`、`Prometheus - Plan Builder`。检查当前列表再向 OpenCode `--agent` 传入它接受的实际名称；不能拿 herdr 的目标别名代替内部角色名。

本地类别包括 visual-engineering、ultrabrain、deep、artistry、quick、unspecified-low、unspecified-high、writing。选择类别或 subagent_type 二者之一，并显式传 load_skills；参考当前 task schema，不复制不同版本的示例参数。

类别是委托路由，不能用 Tab 切到 `quick` 或 `deep`。当前 task 实现明确拒绝把 Prometheus 当作普通 subagent_type；需要规划主角色时走角色选择入口，不照抄失效的 `task(subagent_type="prometheus")`。

## 当前内置斜杠命令

以下 **7 个定义**来自本地 `createBuiltinCommandDefinitions`。运行中仍可被配置、自定义同名命令或可用功能改变；先查当前命令面板。

| 命令 | 实际作用与参数 | 编排注意事项 |
|---|---|---|
| `/goal [objective]` | 设置/替换或查看持续目标；`pause`、`resume`、`clear`。 | Goal hook 依赖 `goal.enabled`，默认 false；看到命令不代表自动续跑已启用。暂停目标与中断正在执行的工具是两回事。 |
| `/start-work [plan-name] [--worktree <path>] [--make-pr] [--ship]` | 接手 Prometheus 计划，通常交给 Atlas；读 `.omo/plans/` 和 boulder 状态。 | 不是“开始访谈”的入口；可自动恢复已有工作。先核对计划归属和实际 cwd，避免接管用户旧任务。 |
| `/stop-continuation` | 停止当前 session 的续跑机制，清 Goal、清当前项目 boulder，异步取消当前 session 的运行/排队后代任务。 | 有项目级副作用；不是通用、无副作用的退出键。详见下面停止流程。 |
| `/refactor <target> [--scope=<file\|module\|project>] [--strategy=<safe\|aggressive>]` | 带分析、计划、实现和验证的重构流程。 | 模板可能委托、建立 Git 提交检查点；提交/扩大范围必须已有授权，普通小修可直接发任务。 |
| `/remove-ai-slops [request]` | 清理分支改动中的冗余或不合适代码并评审。 | 会修改文件，不能作为只读探测。 |
| `/handoff [goal]` | 生成给新会话接手的上下文摘要。 | 摘要不是本项目 result.json；保留原生 session、活动 request、模式和资源归属，不能据摘要盲重发。 |
| `/hyperplan [planning-request]` | 基于 team mode 的对抗规划，并交给计划角色。 | 依赖 team 工具/配置，可能派生多个 agent；最深层不可使用此入口。 |

`/start-work --make-pr` 包含建工作树、推送和开 PR；`--ship` 进一步要求合并和清理。只在用户明确授权这些交付动作时选择相应参数；普通实现任务不能自动添加。

当前 `disabled_commands` 的 schema 枚举中没有 handoff，尽管运行定义中有它。因此不能承诺简单加入 `disabled_commands: ["handoff"]` 就受支持。这是版本差异，不在本项目里修改已安装插件。

### 关键词不是斜杠命令

当前检测器有 `ultrawork`/`ulw`、`team mode`（也接受 team-mode/team_mode/teammode）、`hyperplan`/`hpp` 及相邻的 hyperplan+ultrawork 组合。单独 `team` 不是该检测器的触发词。它们是在普通提示中触发扩展的文本，不等于保证存在 `/ultrawork`、`/team` 等命令。扩展还受 disabled_keywords、enabled_expansions、default_mode 和当前角色影响；规划角色/子会话有额外过滤。完整区别见[工作流表](modes.md#omo-工作流不是主角色列表)。

不要给所有契约默认加 `ulw`。只有用户需要对应工作流时才加入，并约束范围、深度与本轮终点。任务中只是讨论这些词时，注意原始输入可能被关键词 hook 解释；必要时把长任务放入可读 prompt 文件，提交清楚的文件读取指令。

### 旧命令和兼容层

本地内置命令表**没有** `/ralph-loop`、`/ulw-loop`、`/cancel-ralph`、`/init-deep`。README 和部分模板仍保留相关旧措辞，不能把这些当成已验证可用的 OpenCode 指令。某台机器可能通过自定义命令/Skill 重新提供它们；必须看到实际注册再使用。

CLI 的 `omo ulw-loop` 在当前代码中委托到 **Codex** 的实现，也不能据此声称 OpenCode TUI 支持 `/ulw-loop`。本参考不要求安装或操作其他平台。

## 原生工具与身份

这些是目标 agent 内部的工具，不是要原样打进 shell 的命令。外部控制方让目标调用，或通过自己的已授权 API 观察。

| 工具/入口 | 关键事实 |
|---|---|
| `skill(name=...)` | 加载已发现 Skill；共享这 6 个项目 Skill，遵循实际权限。 |
| `task(category=... 或 subagent_type=..., load_skills=[...], run_in_background=...)` | 派生工作；当前 schema 用 `task_id="ses_..."` 继续已有子会话。不要照旧示例随意改成 session_id。 |
| `background_output(task_id="bg_...")` | 读取后台任务；bg ID 来自启动/完成通知，不能拿 ses ID 代替。`block`/`timeout` 只是有界等待。 |
| `background_cancel(taskId="bg_...")` | 精确取消；当前参数是驼峰 taskId。`all=true` 覆盖当前会话后代，只有都属于本轮时才可使用。 |
| `session_read/session_info/session_search(session_id="ses_...")` | 会话证据与恢复；和 bg ID、协议 job_id、round_id、herdr pane ID 分开记录。 |
| `call_omo_agent` | 另一个内部委托入口；同样受任务范围和深度约束。 |
| `team_*` | team 创建、消息、任务分配、状态、关闭/删除等；依赖 team_mode 启用及具体 schema，按实际 teamRunId 记账。 |
| task/todo、goal 工具 | 插件内部执行进度；更新完成不是发布本项目最终结果的替代操作。 |
| LSP、grep/glob、编辑、skill_mcp、interactive_bash 等 | 业务工具，按实际可见 schema 使用；其进程、文件和交互资源也纳入本轮归属。 |

`bg_...` 是后台任务 ID，`ses_...` 是原生 session，herdr `wN:pM`/tmux `%N` 是 pane，协议 job/round 是 32 位十六进制身份。它们不能互换，也不能把进入子 session 的画面误认成父会话。

## 受控轮次、后台任务与停止

### 接单和回传

1. 先记录实际 OMO 模式、根 session、当前角色、cwd 和已有 `.omo/` 状态。并发写任务使用独立工作树/cwd；独立 pane 不隔离项目级 boulder 或工作文件。
2. 普通任务提交完整契约，不额外启用持续目标。目标应遵循本轮范围，把普通问题写入 blocked 文件；原生 Question/permission 界面仍需按 UI 处理。
3. 若允许内部委托，父目标保留子 session/bg/team ID，给子任务传清楚范围和深度。最深层只能本地执行；不要调用内部 task、call_omo_agent、team 继续派生。协议 helper 无法强制限制 OMO 自身进程/会话派生。
4. 发布最终 JSON 前，收集并验证本轮子任务，完成或取消本轮剩余 todo 和可继续写入的后台任务。父回传包含委托净变化。不能让子任务在父报告 success 后仍修改交付。
5. 发布一次后停止本轮。若插件在 idle 后继续启动工作，记录为生命周期不一致并停止续跑；不能覆盖有效结果来掩盖报告后的新改动。新的业务、澄清答案或修订用新轮次。

`.omo/plans`、notepads 等若是本轮要求保留的交付，应准确列入文件变化。运行时 ledger/状态文件单独记在资源或验证凭据中；不要一律删除 `.omo/`，也不要把用户旧计划当成自己的产物。

### 取消和退出顺序

1. 核对目标根 session 与本轮资源；按当前原生中断绑定停止活动 turn/工具。本机 1.18.29 第一次 Esc 显示 `esc again to interrupt` 后需及时再次 Esc；先检查弹窗是否仍占输入焦点，单次 Esc 不保证取消。
2. 对**全部属于本任务的独立 session 与项目状态**，可提交一次实际注册的 `/stop-continuation`。当前实现会停当前 session 的后代任务，并清当前项目 boulder；共享 cwd 有其他活动计划时，不把它当无害的通用清理步骤。
   运行代码还调用插件管理器的 `cancelAllCountdowns()`；在共享服务中不能假定它只影响一个倒计时。
3. 如果项目 boulder/团队还属于其他工作，用当前可用的 `/goal pause` 或精确后台 task 取消等范围更窄的方法，核对作用域；不能自动清全局/项目状态。若这些方法不足以保证本目标停止，保留句柄并报告具体未停止资源。
4. 等待取消兑现，再查看 bg/team 状态、实际子进程和延迟写入。`/stop-continuation` 对后代取消使用异步 Promise，命令返回不证明所有写入已经停止；插件内 team/tmux 资源也要核对自身生命周期。
5. 仅在本轮完成或取消已核实时，用 `/exit` 退出目标，再按资源归属关闭容器。不要把 `opencode session delete`、删除 `.omo` 或 kill-server 当作日常收尾。

停止状态在当前代码中不是每次普通聊天都自动解除。`/start-work` 会显式清除 stop guard；`/goal resume` 改 Goal 状态，但不能因此断言所有续跑机制已恢复。若是普通受控下一轮，直接新契约手动执行即可；若确需恢复持续执行，检查对应机制，不为解除 guard 擅自开启新计划。

## OMO CLI 与诊断

已安装包提供 `oh-my-opencode`、`oh-my-openagent`、`omo` 三种 CLI 别名，但本机 PATH 不一定有命令。先 `command -v`；没有时可用已核对的本地包入口查看 help。不要为了查命令直接 `bunx ...@latest`，那可能下载/升级不同版本。

| CLI 子命令 | 边界 |
|---|---|
| `version` | 查看本地插件版本。 |
| `doctor --platform opencode [--status\|--verbose\|--json]` | 安装诊断；可能涉及外部检查，原始输出先检查再分享。 |
| `boulder --directory <cwd> [--work-id <id>] --json` | 查询计划进度/时间统计；不等于协议结果。 |
| `run <message> --directory <cwd> [--agent <name>] [--session-id <ses_id>] [--json]` | OMO 的非交互执行器，带 todo/后台完成约束；不同于原生 `opencode run`，也不同于本项目 result.json。 |
| `get-local-version`、`refresh-model-capabilities` | 可能检查更新/网络或修改缓存，不属于纯本地帮助读取。 |
| `config migrate`、`install/setup` | 修改配置/安装状态，本次适配不执行；`cleanup/uninstall` 当前 CLI 指向 Codex 清理，不能误用于 OpenCode。 |

`run --on-complete` 是 shell 回调；不要把模型输出或任务原文拼进去。`run --attach`/`--port` 可能复用已有服务，先确认该服务的模式、session 与归属。

## 版本差异与来源

主要依据是已安装 `oh-my-openagent@4.19.4` 的 `dist/index.js`、`dist/tui.js`、`dist/cli/index.js` 和 schema；对应源码位置可在 bundle 的注释中定位：

- `packages/omo-opencode/src/features/builtin-commands/commands.ts`：7 个内置命令及 `/start-work` 参数/角色。
- `plugin/stop-continuation.ts`、`hooks/stop-continuation-guard/hook.ts`、`plugin/command-execute-before.ts`：停止范围、异步取消及恢复条件。
- `tools/delegate-task`、`tools/background-task`、`tools/session-manager`：任务 ID/参数和结果读取。
- `hooks/keyword-detector`、`config/schema`、`plugin/tool-registry.ts`：关键词、功能开关和工具注册。
- `packages/skills-loader-core`、配置解析：Skill 路径及统一配置入口。

[上游项目](https://github.com/code-yeongyu/oh-my-openagent)用于追踪后续变更。当前 README 的 `/start-work` 解释、Ralph/ulw-loop 宣传与运行命令表有差异；仅有旧名字出现在文字模板中不算当前功能注册证据。本机配置包含 14 个角色键的 DeepSeek 模型覆盖，但配置项不等于实际注册角色。当前 CLI 查询确认 Sisyphus、Prometheus、Atlas 为业务主角色，Hephaestus 未注册；各角色与工作流并未全部做模型验收。本次没有修改用户配置。
