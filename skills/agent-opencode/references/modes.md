# 启动模式、角色与工作流选择

核对基线：OpenCode 1.18.29 / OMO 4.19.4，2026-09-17。本机角色清单来自在项目 cwd 实际执行的 `opencode --pure agent list` 和 `opencode agent list`；快捷键来自官方文档，插件规则来自已安装运行代码。角色已注册不代表其所有工作流已经实测。

## 先区分五件事

| 层次 | 例子 | 如何选择或切换 |
|---|---|---|
| 启动 profile | 默认 OMO / `--pure` | 启动进程时选择。Tab、换角色、隐藏侧栏都不会卸载插件。 |
| 当前主角色 | Build / Plan，Sisyphus / Prometheus / Atlas | 启动 `--agent`，或就绪时用角色列表、Tab / Shift+Tab。 |
| 子角色 | general、explore、oracle、librarian | 原生 `@` 或内部委托工具；会派生工作，不等于切换当前主角色。 |
| 模型与变体 | provider/model、high/max 等 | 模型列表与 `Ctrl+T` 变体切换；不是角色或权限切换。 |
| 插件工作流 | ultrawork、规划→执行、Goal、team mode | 提示关键词、实际注册的命令和对应配置；不是一组可以用 Tab 逐一切换的主角色。 |

配置中的 `agent.mode=primary|subagent|all` 描述角色可如何调用，既不表示 pure/OMO，也不表示模型推理强度。隐藏的 compaction/title/summary 可能仍在 CLI 清单里显示为 primary，不能因此把它们当作用户可切换的工作模式。

## pure：Build 与 Plan

| 角色 | 本机状态与用途 | 受控任务注意事项 |
|---|---|---|
| `build` | 默认开发主角色；此前真实测试也以 Build 启动。 | 用于实现和测试；仍受实际权限约束，不等于无审批。 |
| `plan` | 规划/分析主角色。 | 不直接开展实现；报告文件本身也是写入，先核对能否写 result.json。 |
| `general` | 子角色，通用多步任务，可具备写入能力。 | 不能仅因是子角色就当作只读；纳入深度和文件归属。 |
| `explore` | 代码探索子角色。 | 只读探索入口，实际可用工具以当前权限为准。 |
| `compaction`、`summary`、`title` | 内部系统角色。 | 不作为业务主角色选择。 |

官方通用文档称 Plan 的 edit/bash 默认为 ask；**本机实际 Plan 权限清单包含 `edit: deny`，仅特定 `.opencode/plans/*.md` 和宿主计划路径允许 edit**。因此不能保证“Plan 只要确认一下就能写任意文件”，也不应把 bash 的存在当成绕过规划限制的路径。官方当前文档还列出 Scout，但本次本机清单没有它；以安装版本的实际注册为准。

在 shell 中启动指定角色：

```bash
opencode --pure --agent build
opencode --pure --agent plan
```

没有指定时不要永远假设是 Build：有效 `default_agent`、自定义角色、恢复的会话及用户当前选择都可能影响入口。保持当前配置；启动后核对输入区角色名和原生 session。

## OMO：本机可用主角色

| 配置键 | 本机实际注册名 | 职责与选择 |
|---|---|---|
| `sisyphus` | `Sisyphus - ultraworker` | 通用协调与执行，当前默认入口。名字带 ultraworker 不证明本轮触发了 ultrawork 关键词或默认扩展。 |
| `prometheus` | `Prometheus - Plan Builder` | 访谈、澄清、产出计划；不把规划阶段自动扩展为实现。 |
| `atlas` | `Atlas - Plan Executor` | 执行已确定的计划；通常通过 `/start-work` 完成计划选择及状态绑定。仅切到 Atlas 不等于已经正确接管某份计划。 |
| `hephaestus` | 本机未注册 | 当前配置模型不满足 4.19.4 的构建条件；不能假定 Tab 可以切到它。 |

OMO 启用 Sisyphus 的默认组装路径会把原生 build 设为隐藏 subagent；在 `planner_enabled`、`replace_plan` 未关闭时，plan 也成为隐藏 subagent，由 Prometheus 提供主规划入口。只有启用 `default_builder_enabled` 才额外组装 `OpenCode-Builder`；本机未注册此入口。这些是配置能力，不是要求为每次任务修改配置。

因此在 OMO 下不要用 `--agent build` 表示“切回正常 OMO 执行”，也不要用 `--agent plan` 假定拿到 Prometheus。先看当前注册名称，再传 argv。本机当前的示例为：

```bash
opencode --agent 'Sisyphus - ultraworker'
opencode --agent 'Prometheus - Plan Builder'
opencode --agent 'Atlas - Plan Executor'
```

通过 herdr/tmux 启动时也保留 profile 和角色两个独立选择。例如已确定需要 pure Plan 时，herdr 启动参数末尾使用 `-- --pure --agent plan`；OMO 则用 `-- --agent "$ROLE"`，其中 ROLE 是实际注册名。tmux 直接传 `/usr/bin/env "$AGENT_BIN" --pure --agent plan` 或 `/usr/bin/env "$AGENT_BIN" --agent "$ROLE"`，不要把带空格的角色名拼成待执行的 shell 源码。规划任务仍须先满足下文报告通道条件。

启动名称可能受用户 displayName 覆盖；配置键、实际注册名和 herdr 的实例别名分开记录。底层插件有名称归一化逻辑，不保证宿主 CLI 在所有入口都接受短配置键。不要按固定次数 Tab 定位角色，列表排序和可用成员会变化。

本机 OMO 子角色清单还包括 `Sisyphus-Junior`、`Metis - Plan Consultant`、`Momus - Plan Critic`、oracle、librarian、explore、multimodal-looker，以及原生 general/build/plan。角色用途见 [OMO 字典](omo.md#角色与调度)。名称映射代码中出现 Athena 等兼容标识也不代表本机注册了它们。

Hephaestus 的构建器只接受当前代码列出的 GPT-5.3 Codex、GPT-5.4、GPT-5.5、GPT-5.6 名称族，不能泛化成“任意 GPT 都行”。消息 hook 还可能因角色/模型不兼容，在 Sisyphus 与 Hephaestus 之间改派。即使界面切换成功，提交后仍要核对实际角色和模型；本任务不自动替换用户模型或关闭这些 hook。

## 如何切换现有实例

1. 核对 omo/pure、根 session、当前角色和本轮状态。若只是进入了子会话查看，先返回父会话。
2. 等当前 turn 结束并处理仍在运行的子任务；不要在活动工作中盲按 Tab 或输入角色名。普通角色切换不清空原生历史，也不创建本协议的新轮次。
3. 默认 `Ctrl+X` 后按 `a` 打开角色列表；选择实际名称。Tab / Shift+Tab 可前后循环主角色，但补全、弹窗和自定义绑定可能拦截这些键。
4. 读取输入区确认角色。选择列表项的 Enter 与提交业务消息的 Enter 分开；不要把角色名作为聊天发送。
5. 更新控制方凭据中的角色、模型/variant 和原生 session。若上一轮已发布有效结果，规划转实现等新工作照常创建新 round，旧结果不变。

pure ↔ OMO 是另一种操作：本配方通过关闭已收尾的自有目标并用所需 profile 启动来切换，或另起明确区分的目标；不靠 Tab 切换插件。恢复时使用精确 ses ID，并核对另一 profile 缺失的角色、工具与状态，不能静默改变运行模式来绕过故障。

## 规划角色与结构化回传

Prometheus 的 `prometheus-md-only` hook 对 write/edit 类工具只允许工作区内 `.omo` 路径中的 Markdown；同时给 task/call_omo_agent 注入只读规划要求。普通外部 result.json 不满足该规则。其 permission 中出现 edit/bash allow，并不意味着绕过 hook 后可以随意写 JSON。

当前协议没有通用的“规划角色专用回传适配器”。需要 schema v1 文件时，优先使用能合法写报告的 Sisyphus/Build 承接一个范围明确的分析任务；这与用户明确要求 Prometheus/Plan 不等价，不能静默替换。用户明确指定受限规划角色时，先确认独立的合法报告通道；无法写入则保留计划及原生输出，明确报告协议未闭合，不反复要求重写、不改扩展名伪装、不用 bash/其他角色绕过限制。控制方自己的检查记录也不能冒充目标发布的 result.json。

常见工作流是 Prometheus 访谈/写计划 → 用户或已有授权确认执行 → `/start-work <准确计划名>` 由 Atlas 接手。保存计划路径、boulder 归属、cwd/worktree 和实际角色。仅要求出计划时止于计划阶段；普通有限任务无需启动 Goal 或整套计划执行流程。

## OMO 工作流不是主角色列表

| 入口 | 当前实现 | 前置条件与边界 |
|---|---|---|
| 普通提示 | 当前角色按正常方式处理。 | 不默认加工作流关键词；默认配置也可能注入扩展。 |
| `ulw` / `ultrawork` | 普通消息关键词触发执行扩展。 | 不是 `/ulw` 命令保证；当前代码保留运行时 variant，不意味着自动设为 max。 |
| `team mode` / `team-mode` / `team_mode` / `teammode` | 当前 team 关键词正则。 | 单独 `team` 不匹配这个检测器；团队工具仍依赖 `team_mode.enabled`，schema 默认 false。 |
| `hyperplan` / `hpp` | 对抗规划关键词。 | 与 `/hyperplan` 命令分开；完整工作流依赖 team 能力。 |
| `hpp ulw` 等组合 | 相邻的 hyperplan/hpp 与 ultrawork/ulw，顺序可反转。 | 组合扩展会抑制独立的 hyperplan/ultrawork 扩展；仍受功能开关和深度限制。 |
| `/goal ...` | 设置/查看/暂停/恢复/清除持续目标。 | `goal.enabled` 默认 false；不因目标字样自动启用续跑。 |
| `/start-work ...` | 接手具体计划执行。 | 不等于“进入 Plan”；参数、发布副作用见命令表。 |

本地 schema 的 `default_mode` 只有 `ultrawork` 和 `goal` 两个布尔项，默认 false；它不是当前角色名。`goal.enabled`、`team_mode.enabled` 是各自能力开关，不能互相替代。OMO CLI 的 `default_run_agent` 属于其 run 入口，不能当作原生 `opencode` TUI 的通用角色切换命令。

本次读取的用户级 `[opencode]` 平台段只有 schema、14 个 agent 模型覆盖和 8 个 category 覆盖，没有显式设置上述工作流开关。实际运行还可能合并项目/祖先配置；不能仅凭角色名字或命令存在就宣称 Goal/team 已启用，也不为补全字典自动开启它们。

关键词 hook 会跳过斜杠命令、后台子会话和非 OMO 角色；规划角色会过滤 ultrawork/hyperplan 类关键词。代码块和行内代码也被该检测器排除。这些过滤不等价于所有 hook 或默认系统提示都关闭；`default_mode.ultrawork` 还有单独的系统提示注入路径。旧 README 的 search/analyze 等宣传不代表本版检测器注册了相应模式。

## 模型、变体和显示

- `/models` 或 `Ctrl+X` 后按 `m` 选择模型；F2 / Shift+F2 默认切换最近使用的模型。
- `Ctrl+T` 默认循环当前模型可用 variant，例如某些模型的 high/max；名字和语义取决于 provider/model。没有变体时不虚构可选项。
- `/thinking` 只控制思考内容显示；不等价于 Ctrl+T，也不改变 agent 权限。
- `opencode run --variant <name>` 属于非交互 run 的参数；本机根 TUI `--help` 没有列出 `--variant`，不把 run 参数直接搬到 TUI 启动。
- OMO 角色/category/命令可能有各自模型或 variant；切换之后记录实际生效值，不承诺所有子任务共享父模型。

## 自定义命令与额外派生

原生命令可指定 `agent`、`model`、`subtask`。指向 subagent 时默认触发子任务；`subtask: true` 也能让 primary 以子任务执行。它们会影响实际角色和嵌套深度，不能因为入口是 `/某命令` 就绕过最深层规则。

命令可能来自配置、用户/项目 `.opencode/commands`、插件和发现的 Skill；同名覆盖会改变行为。正式调用前核对实际命令元数据，特别是 `agent`/`subtask`、文件与 shell 展开，而不是只看命令名称。项目不承诺把所有用户自定义命令静态列全。

## 来源与验证范围

- 官方：[Agents](https://opencode.ai/docs/agents/)、[Keybinds](https://opencode.ai/docs/keybinds/)、[Commands](https://opencode.ai/docs/commands/)，本次已读取。
- 本机实际角色清单、CLI help；安装包 `dist/index.js` 中的 `agent-config-assembly`、`agent-display-names`、`hephaestus/agent`、`prometheus-md-only`、`keyword-detector`、`system-transform`，以及 `oh-my-opencode.schema.json`。
- 既有模型实测覆盖默认 Sisyphus 与 pure Build 的协作，未证明 Plan/Prometheus 的 JSON 回传、全部角色切换、team/Goal 或全部模型变体可用。本次补充进行了真实角色/权限查询及文档/运行代码核对，没有发送新的模型任务。
