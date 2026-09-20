# Codex 原生接口

## herdr / tmux

按[公共传输配方](../../agent-orchestrator/references/transports.md)创建并记录精确 pane。交互示例：

```bash
herdr --session "$SESSION" agent start "$AGENT" --kind codex --pane "$PANE" -- \
  --no-alt-screen --sandbox workspace-write --add-dir "$ROUND_ROOT"
```

这是允许业务编辑和报告写入的示例；权限范围必须来自用户任务。tmux 同样把 `codex`、每个选项、路径作为独立 argv 传递。首次目录信任、登录、升级提示或权限对话框可能阻止提交；检查实际界面后操作，不能把提示文字发进 shell。

herdr 本机支持 kind codex，但检测成功不代表收到某一轮。记录 CLI 接受、屏幕/进程、原生会话及当前结果。交互键入长文本后可能先出现粘贴占位；验证已经提交且输入框清空，再等待模型。

## 非交互执行与恢复

在需要事件流的脚本里用 `codex exec`。该进程是独立运行资源，不伪造 herdr pane，也不把 `exec` 填入协议脚本当前仅支持的 transport mode。另存控制方进程凭据（PID、启动身份、argv、事件日志、退出码、session ID），轮次仍使用相同 request/result 契约。

```bash
codex exec --sandbox workspace-write --add-dir "$ROUND_ROOT" \
  --cd "$PROJECT_CWD" --json --output-last-message "$FINAL_MESSAGE" - \
  < "$PROMPT_FILE" > "$EVENT_LOG" 2> "$STDERR_LOG"
```

使用 Git 工作区；明确属于本次的非 Git 测试目录可加 `--skip-git-repo-check`。不向提示或日志输出鉴权信息。继承已登录的 CLI 身份与配置，不自行复制 auth.json。

- `--json` 是 **JSONL 事件流**，包括 thread/turn/item；不是 schema version 1 的回传文件。
- `--output-last-message` 是最终消息副本，不直接指向协议 `result_path`，否则可能破坏原子发布与不可覆盖约束。
- `--output-schema` 只约束最终消息形状，不能证明任务成功或实现原子发布。正常让目标用协议 helper 发布；若采用控制方转换候选结果，必须先校验身份和字段再 write-result。
- 保存 `thread.started` 给出的精确 ID。多轮使用 `codex exec resume <SESSION_ID> <PROMPT>`；不在并发环境用 `--last`，不同时对一个会话续跑。
- 新轮明确重传原始 cwd/权限和新 request。0.154.0 实测仅 `codex exec resume ID` 没有保留首轮回传目录的写入权限：业务已修改，报告写入报 Errno 30。把初始 exec 选项放在 resume **之前**：`codex exec --sandbox workspace-write --add-dir "$ROUND_ROOT" --cd "$PROJECT_CWD" resume "$SESSION_ID" --json -`。已发生的报告失败只修报告，不重复业务；本地真实同轮修复验证了计数不增加、旧结果不变。
- 需要持久恢复时不加 `--ephemeral`。exec 无法依赖交互审批；工具被拒绝时保留错误与部分结果，必要时转交可审批的交互会话，不能扩大权限自动重跑。
- 超时只是控制方停止等待。取消前确认 PID 启动身份及自有进程组，发出中断后等待真实命令退出、检查迟到写入。中断/非零退出可能没有结果，不能伪造 success。

## 当前版本差异

本地 0.154.0：`--ask-for-approval` 为 `on-request|never`；新脚本用明确的 `--sandbox workspace-write`，不依赖旧 `--full-auto`。权限菜单推荐 `/permissions`；旧版本菜单/命令可能不同。`codex queue` 存在不表示可以绕过一个目标只执行一轮的要求。

官方来源（2026-09-17 实际读取）：

- [非交互运行](https://developers.openai.com/codex/noninteractive)
- [Skill 发现与软链接](https://developers.openai.com/codex/skills)
- [TUI 指令](https://developers.openai.com/codex/cli/slash-commands)

文档与本机帮助冲突时记录版本差异，测试本机行为；不把静态核对称为真实模型通过。
