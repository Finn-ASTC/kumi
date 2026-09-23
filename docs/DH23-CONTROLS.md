# DH-02/03 第一批：事件证据与控制回执

日期：2026-09-23。基线：`aba86e5`。本批完成基础协议与无模型终端验证，
没有实现自动审批服务，不关闭 DH-02/03 的全部待办。

## 本批交付

1. `watch.py detail` 读取原事件的完整已捕获画面、资源、review 与证据指纹。
   新事件绑定 evidence SHA-256；被替换的证据拒绝使用。旧事件标记
   `legacy_unpinned`，仍可查看，但不能用来创建新的精确拒绝意图。
2. `controls.py inspect` 固定 job/round、请求、资源、原生会话、launch、提交 attempt
   与 watch/seq/issue，现场重读目标。capture 明确记录本地截断及完整性未知。
3. `jobs.py control-begin/control-receipt` 使用现有租约与 revision CAS，先保存
   `uncertain` 再由控制器执行动作。动作确认分开记录，恢复建议 `reconcile_control`；
   租约转移不会清除不确定性，也不允许新提交绕过它。
4. 拒绝后用 `controls.py review` 把已确认动作绑定回原事件。它不能处理下一弹窗，
   也不会批量关闭同 job 的事件。决策、动作意图、确认与 review 分别可追溯。
5. 已分配/启动的 job 取消收口要求新鲜的停止确认，覆盖前台、后台、原生子任务、
   交互状态与副作用。支持无结果取消、停止证据刷新、失联后的人工核对。
   不伪造子 agent 的 error/success；新提交不能复用旧停止确认。
6. 未确认控制或已确认但未关闭的取消，令监督检查点返回零独立工作预算。
   同索引内另一个未关闭 job 不能对相同终端创建控制意图。

运行配方、JSON 形状与 CLI 示例见[控制回执](../skills/agent-orchestrator/references/controls.md)。
`controls.py` 是运行包模块；新增回归和确定性 terminal peer 保留在 `tests/`，不安装给 agent。

## 验证

```bash
ORCH_RUN_TMUX_TESTS=1 python3 -m unittest discover -s tests
python3 tests/check_skill_package.py
```

Linux、Python 3.14.7：**570 项全部通过，含 14 项真实 tmux 检查，零跳过/失败**。
本批新增 30 项控制回归、1 项监督预算检查、1 项真实 tmux 流程；没有模型调用。
变更 Python 的 Ruff 基础检查与 `git diff --check` 通过。

覆盖旧轮/换 watch/同屏重新出现、证据修改、过期检查、错误控制 ID、后台检查缺失、
新控制器接管、未知动作不重发、not_sent 后重新检查、失联人工核对、取消无结果、
停止确认过期与刷新、重复 review、新提交使旧停止失效和共享终端冲突。

真实 tmux 测试使用私有 socket、确定性目标和旁边的 sentinel 会话。通过实际 CLI
创建控制意图/回执：目标出现权限画面→只拒绝本次模拟操作→确认原事件→目标 idle 后
退出→核对无子任务/结果→确认停止→取消 job。sentinel 保留，submission 不确定性仍
忠实保留，没有借取消改写历史提交结论。`DENY` 仅为测试目标协议，不是任何原生宿主键。

旧测试中有五处直接取消已分配任务，本批改为明确的合成停止确认；没有删除或绕过
旧验收、关闭后审计、跨 watch 待办和租约保护测试。

## 兼容与边界

- request/result v1 不变，既有 job 历史和旧 watch 可读，不重写历史取消记录。
- 新 `close cancelled` 对已经分配资源的任务更严格；必须先确认停止。仅无资源、
  无 launch、无 attempt 的任务可记 `not_started`。failed 记账本身不证明写入者停止。
- review 证据使用既有有界常规文件读取，最大 16 MiB，拒绝最终 symlink/FIFO；大日志
  应外置并用简短证据文件引用，避免审批路径被无界读取拖住。
- 控制器填写的方法和检查结果仍是声明，helper 核对身份、时序、指纹和结构，不能
  自动证明系统里所有写入者都已停止。它不发送按键、不杀进程、不授予任何新权限。
- 读屏/发送不是原子操作；相同弹窗在两次观察之间消失重现，或外部直接发键，仍可能
  绕过本地合作协议。新事件不是原生 operation ID。纯 TUI 的严格 exactly-once 未实现。
- 30 秒新鲜度窗口是本批控制检查约束，不是审批响应 SLA。场景可能需要重新观察；
  CLI 慢读期间租约或检查过期会拒绝提交意图。
- 检查详情返回捕获画面，不自动提取/证明完整命令、选项或授权范围。旧事件有疑问时
  保留旧审查记录，使用新的现场观察；不能改写旧 hash 伪造已固定证据。

## 下一步

第 4 批继续：按宿主固定精确拒绝/中断能力与证据格式，尤其 OMO 后台停止、Hermes、
失联与旧控制器续租场景；再抽取 DH-04 参数化执行与 DH-07 实验资源占用。持续监督
时效目标仍未关闭。AP-01/02 的统一决策契约与离线规则/小模型评测依赖这些入口，
本批尚未接入 Jev/Laya、人类计时窗口或自动动作执行器。
