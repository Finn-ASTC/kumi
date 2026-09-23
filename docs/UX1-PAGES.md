# UX-01：默认同 space 新 tab 与 agent 标题

日期：2026-09-23。基线：`1e5bd93`。运行包、runner 与限定桌面验证已完成；
未同步个人安装、未推送远端，不涉及审批自动放行。

## 行为

- 已确认父 pane/tab 时默认同 workspace 新建 tab，使用 `--no-focus`。
- 明确 `--layout workspace` 可新建 space；分屏仍只在显式请求时使用配方。
- 无父身份时在明确选择的隔离 session 新建 workspace；不推断 focused/default。
  部分父身份、跨 workspace 或现场 pane/tab 不匹配直接失败。
- 标题为“任务目的 · agent 类型”，包含 Codex、omp、Hermes、OpenCode (OMO/pure)；
  与既有标题重复时加 ` · 2` 等短编号。内部 agent 名和资源 ID 独立。
- 相关续轮复用原 tab、资源记录和标题。明确改任务目的时只重命名核验过的自有页面、
  更新显示回执；不可变 `resources.json` 不随显示标题修改。更换宿主需要新启动身份。

运行包新增只读 [pages.py](../skills/agent-orchestrator/scripts/pages.py)：
现场检查父归属、枚举既有标题并输出创建 argv/ownership，不执行分配、聚焦或模型启动。
runner 使用同一规划器，并在启动 agent 前再次核验新 pane 的真实 tab 归属。
分配结果先持久化，再验证/启动；异常保留 `start_uncertain`，禁止盲目重新创建。

场景 `label` 保持任务目的；runner 的 `page_label`、`page_plan` 保存实际显示标题与计划。
新 tab 只拥有 tab，不能因父 workspace 相同而关闭整个 space。清理仍先要求正常退出
agent，再检查子资源身份/内容并关闭该 tab。

## 验证

Linux、Python 3.14.7、herdr 0.9.1、tmux 3.7c：

```bash
ORCH_RUN_TMUX_TESTS=1 python3 -m unittest discover -s tests
python3 tests/check_skill_package.py
python3 tests/herdr_pages_probe.py --root "$ORCH_TEST_ROOT"
# 另开自有 kitty 窗口，需 niri/kitty 和桌面权限：
python3 tests/herdr_pages_probe.py --root "$ORCH_TEST_ROOT" --viewer
```

- **538 项全部通过，含 13 项真实 tmux 检查，零跳过/失败**；没有新模型调用。
- 离线覆盖默认/显式布局、父身份不完整/不匹配、标题控制字符、shell 字符按数据传递、
  OMO/pure、重名、创建前显示回执持久化、续轮复用和精确 tab 清理。
- 真实 herdr 独立会话：同 space 三个子 tab、重复 Codex 标题、OpenCode pure 标题、
  显式新 space、错误同 space 父 tab 拒绝、创建后父焦点、只清理子页面后父页保留通过。
- 桌面 kitty/niri：通过仅限测试窗口的屏幕文本读取确认实际渲染；后台创建后桌面焦点
  保持，主动选中自有测试 tab 后 OpenCode (pure) 标识可见。测试窗口随后关闭。
- 分发链接检查和变更 Python 的 Ruff 基础检查通过。

私有实验目录名为 `kumi-pages-qlsvnl74`（服务器）和 `kumi-pages-5ucd7ow4`（viewer），
位于用户授权的 sandbox 父目录。原始路径、终端内容与命令回执不进入公开运行包。
可复用探针留在 `tests/`，不需要安装给 agent；`pages.py` 属于运行包。

## 限制和后续

窄窗口 tab 栏会截断后面的长标题，实际桌面初次检查因此未满足“全部类型同时可见”；
选中目标 tab 后类型可见。当前保持任务优先顺序，没有修改 herdr 本体渲染。
这不是跨桌面/字体/尺寸兼容性验证，也没有自动模拟鼠标交互或四宿主长期运行。

标题查重基于实时快照，不提供跨控制器的原子名称预留；同一调度方串行分配即可处理
常见重复目的，独立控制器同时创建仍可能同名，身份与清理从不依赖标题。
runner 的 follow 不自动推断新目的并改标题；明确变化按运行包 rename 配方处理。
页面分开不等于文件系统隔离。

本批只验证 transport/viewer 与已有续轮协议，未调用真实模型验证 skill 遵循率。
审批与巡视保持焦点的配方已明确；审批完整处置和弹窗时效仍属后续 DH-03/DH-02/AP。
综合计划继续进入审批事件证据与输入身份的基础批；不宣称 Jev/Laya 已接入。
