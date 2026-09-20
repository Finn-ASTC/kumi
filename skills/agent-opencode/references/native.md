# OpenCode 原生 CLI、TUI 与 Skill

适用于默认 OMO 和显式 pure 的宿主交互；OMO 在此基础上加角色、命令、hook 和工具。命令存在与权限仍以当前安装版本和 TUI 为准。核对基线：OpenCode 1.18.29，2026-09-17。

Build/Plan、OMO 主角色及其切换、模型 variant 与工作流的区别见[模式与角色选择](modes.md)。选择规划角色前务必核对其结果文件写入能力。

## CLI：在 shell 中执行

| 命令 | 意义与边界 |
|---|---|
| `opencode [project]` | 在当前/指定目录启动交互 TUI；本机默认加载 OMO。 |
| `opencode --pure [project]` | 禁用外部插件的 TUI；保留正常配置、模型、权限和 Skill 发现，不等于全新用户环境。 |
| `opencode --agent <name>` | 启动时选择已注册角色；pure 可选择原生 build/plan，OMO 角色另查实际列表。 |
| `opencode --model <provider/model>` | 当前启动指定模型；不要擅改用户角色的模型选择。 |
| `opencode --session <ses_id>` | 恢复精确原生会话；恢复时保留原来的 omo/pure 模式。 |
| `opencode --continue` | 恢复“最近一次”会话；并发受控任务应使用精确 `--session`。 |
| `opencode --fork --session <ses_id>` | 分叉原生会话；不是同一任务轮次的普通继续，需重新绑定实际 session。 |
| `opencode run [message..]` | 非交互运行；和 TUI 是不同传输行为，不拿它替代已经在运行的目标。 |
| `opencode run --pure --format json --dir <cwd> <message>` | pure 非交互运行并输出 JSON 事件流；事件流不是本项目 schema v1 的 result.json。 |
| `opencode run --agent <name> --variant <variant> <message>` | run 可指定角色和模型变体；根 TUI help 未列出 `--variant`，不要跨入口套用。`--thinking` 只控制输出显示。 |
| `opencode run --command <name> <args...>` | 调用实际已注册命令，message 参数作为命令参数；插件命令需要相应模式。 |
| `opencode session list` | 列出原生会话。这里是 CLI 单数 `session`。 |
| `opencode agent list` | 查询当前可用角色；可能初始化配置/插件，应保留诊断输出、避免泄露完整配置。 |
| `opencode debug skill` | 安装后检查 Skill 可见性；可以另加 `--pure` 检查纯模式。 |
| `opencode --version`、`opencode --help` | 核对当前版本与选项，随后按需查 `run/session/agent/debug --help`。 |
| `opencode models [provider]`、`opencode stats` | 查询模型清单、用量和成本；不是切换现有 TUI 的命令。 |
| `opencode --mini`、`opencode run --interactive` | 本机额外的简化交互入口，分别见根/run help；不是 Build/Plan 或 OMO 开关，本项目尚未验证这些 UI 的键盘配方。 |

`serve`、`web`、`attach`、`run --attach` 使用服务端会话。客户端 `--pure` 不能证明已经运行的服务端没加载 OMO；如切换到这些传输，需要另行核对服务端启动参数和归属。当前 herdr/tmux 配方使用交互 TUI。

本机 CLI 还提供 `agent create`、`mcp`、`providers`（别名 `auth`）、`acp`、`github`、`pr`、`plugin`（别名 `plug`）、`db`、`upgrade`、`uninstall`、`completion` 等管理/集成入口。需要时读取具体 `--help`；它们可能修改配置、账号、数据库或 Git 状态，不归类为切换运行角色的指令，也不作为启动探测自动执行。

`--auto` 是自动批准未明确拒绝权限的选项，不能当作减少卡顿的通用启动参数。`session delete` 删除原生历史，`export`/`import`涉及会话数据；关闭测试 TUI 不需要删除用户历史。插件安装/升级、账号登录也不是普通受控任务的启动步骤。

## TUI：在目标输入框中使用

| 命令 | 意义与副作用 |
|---|---|
| `/help` | 查看当前帮助。命令面板默认 `Ctrl+P`，可检查实际注册命令。 |
| `/new`、`/clear` | 新建原生会话。不是当前任务的普通追问；使用后重新记录原生 session。 |
| `/sessions`、`/resume`、`/continue` | 列出/切换原生会话。标准名字是复数 `/sessions`。 |
| `/models` | 打开模型选择。标准名字是复数 `/models`。 |
| `/compact`、`/summarize` | 压缩当前上下文；不更换本轮身份或结果路径，必要时重新提供活动契约。 |
| `/exit`、`/quit`、`/q` | 退出程序；先收尾当前工作，核对进程退出。 |
| `/details`、`/thinking` | 切换工具细节/思考内容的显示；后者不等于更改模型推理强度。 |
| `/themes` | 主题选择；一般无需为编排修改。 |
| `/editor` | 打开 EDITOR 指定的外部编辑器，会引入额外交互。 |
| `/export` | 导出会话并打开编辑器；不是本项目的结构化回传。 |
| `/init` | 创建或更新 AGENTS.md；会修改项目，不作为无副作用的就绪探测。 |
| `/undo`、`/redo` | 回退/重做消息及相关文件变化，需 Git；不能拿来修 result.json 或替代正常取消。 |
| `/connect` | 提供商/账号配置，可能请求凭据。 |
| `/share`、`/unshare` | 发布/撤销会话分享，不用于普通任务回传。 |

旧字典中的 `/session`、`/model`、`/agent` 不再作为保证存在的标准命令。角色选择使用当前命令面板或以下绑定；项目/插件自定义别名可能存在，但需现场确认。

## 输入、导航、中断

默认 Enter 提交，Shift+Enter/Ctrl+Enter/Alt+Enter/Ctrl+J 换行。传输仍使用单行契约和独立提交键。`@` 可引用文件，也可点名子 agent；后者可能派生子会话，仍受深度约束。消息开头 `!` 执行 shell，`/` 调用命令；不要把原始任务误发到这些入口。文件载入或粘贴后仍要核对内容和当前焦点。

| 默认键 | 行为 |
|---|---|
| `Ctrl+P` | 命令面板。 |
| `Tab` / `Shift+Tab` | 切换主角色；输入补全框打开时 Tab 可能补全，先检查焦点。 |
| `Ctrl+X`，再 `a` | 角色列表，逐项确认名称。 |
| `Ctrl+T` | 循环当前模型的 variant；可用项由 provider/model 决定，不是切换角色或 `/thinking`。 |
| `F2` / `Shift+F2` | 正向/反向切换最近使用的模型。 |
| `Ctrl+X`，再 `m/l/n/c/q` | 模型列表/会话列表/新会话/压缩/退出。leader 是两次按键，不是同时按三个键。 |
| `Esc` | 1.18.29 工作中第一次显示 `esc again to interrupt`，需要在确认窗口内再按一次；单次 Esc 实测没有停止工具。处于补全/选择/审批框时可能先关弹窗，以画面为准。 |
| `Ctrl+C` / `Ctrl+D` | 上下文相关：清空输入、删除字符、退出等；不能统一当成取消运行。 |
| `Ctrl+X`，再 `Down` | 进入子会话；子会话 Left/Right 切换，Up 返回父会话。观察子会话后要确认回到目标根会话再提交下一轮。 |

`tui.json`/`tui.jsonc` 可以重定义这些绑定。原生 permission 和 Question 选择框按实际选项操作；普通澄清优先通过本轮 blocked 回传，原生权限不能用文件绕过。

自定义斜杠命令可覆盖内置命令，并指定 `agent`、`model` 或 `subtask`，有时会派生子任务而非切换父会话角色。检查当前命令表和定义后再调用，详见[命令路由](modes.md#自定义命令与额外派生)。没有默认快捷键的命令面板动作不能被当作不存在，也不能凭动作名称猜 `/命令` 别名。

## 安装与发现 Skill

本项目的 6 个兄弟 Skill 应一起安装，保持相对引用。原生 OpenCode 与 OMO 共用推荐目录：`~/.config/opencode/skills/<name>/SKILL.md`。XDG 或自定义 OpenCode 配置目录以实际配置为准。

其他原生发现入口：项目 `.opencode/skills/`，以及项目/全局 `.claude/skills/`、`.agents/skills/`。OMO 还兼容部分旧单数 `skill/` 路径，但新安装统一使用 `skills/`。重复名称可能被更高优先级来源遮蔽，不能仅检查某个文件存在。

安装后要求目标使用 `skill({ name: "agent-orchestrator" })` 或 `skill({ name: "agent-controlled" })`，具体调用结构以实际工具 schema 为准。OMO 可能把发现的 Skill 也合入命令表，但原生 `skill` 工具是两种模式共用的入口；不要从 omp 复制 `/skill:<name>` 语法。

不可见时检查：大写 SKILL.md、name 与目录一致、实际加载路径、同名覆盖、`permission.skill` 是否为 deny/ask、该角色是否禁用了 skill 工具、OMO 的 `disabled_skills` 和加载范围。不要为了发现 Skill 自动关闭权限。`opencode debug config` 可能包含提供商配置，不把完整输出直接发到报告。本次 OMO 的该诊断输出未能作为配置 JSON 可靠解析，角色/权限事实改用实际 `agent list` 及安装代码核对；不要假定诊断 stdout 永远可直接交给 JSON 解析器。

## 来源

- 本地：`opencode --help`、`opencode run --help`、`opencode session --help`、`opencode debug --help`。
- [官方 TUI](https://opencode.ai/docs/tui/)、[键绑定](https://opencode.ai/docs/keybinds/)、[Skill 发现与权限](https://opencode.ai/docs/skills/)，读取于 2026-09-17。
- 当前本机 OMO/Sisyphus 和 pure/Build 的提交、原生权限、回传与双 Esc 取消已有真实模型证据；本次又查询了两种模式的实际角色/Plan 权限。其他角色切换、模型变体和所有管理命令未逐项实测，不能把字典收录视为验收通过。
