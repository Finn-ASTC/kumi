# DH-01：按轮验收与切轮检查

日期：2026-09-23。实现前基线：`dabecb8`。

## 已交付行为

原来 `accept` 只能作用于活动轮；切轮清空当前 completion，已记录的旧验收只能翻阅
revision，漏记后没有入口补记。success 也能在没有验收时激活下一轮。

本批增加：

- `jobs.py round-status --round-id ID`：只读查询已登记轮次，区分当前证据是否有效
  与当时切轮记录。省略 ID 查询活动轮，不查询终端或推断宿主已停止。
- `jobs.py accept --round-id ID`：用当前 revision 与有效租约给指定旧轮补记或更新
  验收。结果/请求/交付 attempt 仍精确绑定旧轮；当前轮、原生身份、观察绑定与
  completion 不变。每次新建 revision，原记录不覆盖。
- success 切轮前要求有效的 accepted 或 rejected；后者支持正常返工。缺失、证据
  变化或跨轮 attempt 不能过关。blocked/error 保留无 success 验收的合法续轮。
- 切轮记录保留结果 hash、当时的验收状态与时间。后来的补记标明 `backfilled` 和
  实际 `recorded_at`，不回写过去。旧版缺少切轮检查记录时显示 `unknown_legacy`。
- 已关闭 job 可通过 `claim --acceptance-only` 获得仅验收租约，仅能 accept/renew/
  release。不能重新打开、派单、切轮或记录宿主活动；原 `closed` 和其 decision-time
  completion 原样保留。后来的负面复核可以与历史关闭结论不同。

操作配方见[按轮验收](../skills/agent-orchestrator/references/completion.md#per-round-review-and-follow-up)。
运行包、用户指南及 runner 说明已同步；不修改个人安装或现场 DataHive 记录。

## 兼容与边界

新字段是 job schema v1 的附加记录，旧历史无需迁移。普通 recovery 和 mutation 回复
保留活动轮视图及补记数量；按轮查询才扫描历史 revision。多次验收的原始版本仍在
revision 中。扫描历史和交付证据回读有本地 I/O 成本，不是常数时间查询。

本批有意收紧 success 的 `activate`：旧调用方若直接 success→activate，现在需要
先记录验收。已有 runner 的 verify 会记录 accepted/rejected，正常返工流程继续成立。
若 follow 已留下 candidate 但激活失败，补齐前轮验收后用 reconcile-round 接续，
不要重复准备或发送。`prepare --previous` 自身不代表提交，不受此门槛替代。

历史结果已有切轮/验收 hash 时，补记拒绝绑定被替换的结果。旧版未验收且没有保留
结果 hash 的轮次，只能按当前留存证据补记，不能证明它在原交付时内容相同。
证据 hash 验证留存内容和身份，不替代验收人的判断，也不自动证明检查覆盖充分。

验收与原生收尾仍分开：有效拒收允许继续修复，不代表可以直接向未就绪终端发新任务。
completed 继续要求独立接受和新鲜的宿主收尾证据。仅验收租约限制本工具入口，不能
拦截绕过工具的直接终端输入。DH-02/03 的停止接管与审批闭环继续跟进。

## 验证

新增 19 项确定性回归，覆盖：

- success 无验收、有效拒收、证据变化、blocked/error 续轮。
- 多轮独立查询、旧版漏记、补记时间、切轮记录不变、当前轮不继承旧验收。
- 旧请求/结果变化、跨轮 delivery attempt、交付证据变化。
- revision/租约冲突、未激活/外来轮次、CLI 只读查询与显式选轮。
- 关闭后的仅验收租约、活租约排他、权限受限以及原关闭记录保持。

独立 sandbox 中通过原有真实 tmux 无模型金额项目演练：作者三轮
（blocked→有缺陷交付→修复交付），验证者一轮，失败独立验收触发返工，最后两个 job
关闭且没有重复提交。随后通过 CLI 按轮读出 not_applicable/rejected/accepted，对已
关闭作者的中间轮追加拒收记录，核对原切轮记录、最终轮和 completed 证据均未改变。
演练未启动模型，证据保存在私有测试目录，原始路径和回执不收入公开包。

初次真实 tmux 运行被沙箱 socket 限制阻止；获准后重跑。三个既有 success 续轮夹具
补齐明确验收，保留原恢复/监督验证目标。最终 **530 项回归全部通过，含 13 项真实
tmux 测试，无跳过**；分发包 150 个本地链接、Ruff 本批代码检查及差异检查通过。
新增文件经 Ruff 格式化；环境仍没有 mypy/black 模块，不声称通过它们的检查。

本批关闭 DH-01 的工具实现与确定性验收；真实多宿主长期组合仍归 F10。
下一项为 UX-01：默认同 space 新 tab，标题含任务目的与 agent 类型。
