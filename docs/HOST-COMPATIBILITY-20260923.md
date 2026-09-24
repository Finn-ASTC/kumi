# 2026-09-23 宿主版本与兼容审计

实现基线：`496f530`。本次区分本机实际版本、包管理历史、上游发布及已完成测试。
不自动升级宿主，不修改宿主源码/用户配置，不启用自动审批，也不把帮助检查写成原生
任务通过。原始路径、配置、个人会话和系统日志不进入公开报告。

2026-09-24 会话管理规划补查：本机 `omp --version` 已返回 **18.3.0**；帮助仍有
`--session-dir` 与 `--resume`，尚未重跑新版原生行为。Codex 0.155.1 的 help 有
archive/unarchive，OpenCode 仍为 1.18.29；Hermes/OMO 本次未重新核对。详见
[UX-02 候选路线](SESSION-MANAGEMENT.md)。下方表格保留 9 月 23 日的历史事实，不能
作为所有组件的当前版本清单；本次未升级或变更宿主配置。

## 实际版本与更新来源

| 组件 | 上次相关记录 | 本次实际安装 | 结论 |
|---|---|---|---|
| Codex CLI | 0.154.0 恢复实验；0.155.0 原生监督 | 0.155.1 | 包日志确认 9 月 22 日 05:22（UTC+8）从 0.155.0 更新 |
| omp | 18.2.6 原生监督；18.2.3 中断 | 18.2.11 编译可执行文件 | 相对已测版本有变化；不以旧 npm 17.3.5 源树推断当前实现，安装时间未单独确认 |
| Hermes | 0.21.3、`64ea66b03d`，9 月 20 日已核对 | 同版本、同提交；私有 Python 3.11.16 | 未观察到本轮更新；早期 0.20.5 中断测试不能代替当前验证 |
| OpenCode | 1.18.29 | 1.18.29 | 9 月 18 日先升到 2.0.5，同日 19:07 又降回；以实际 binary＋包数据库为准 |
| OMO | oh-my-openagent 4.19.4 | 指定安装包元数据 4.19.4 | 未观察到变化；不声称已核对每个活动会话加载的插件 |
| herdr | 0.9.1 | 0.9.1 | 9 月 18 日更新已包含在 UX-01 验证基线 |
| tmux | 3.7c | 3.7c | 与已测传输基线一致 |

系统 Python 3.14.7、Bun 1.4.2、Node.js 26.9.0。系统滚动更新不覆盖所有独立安装，
CLI 文件更新也不证明共享 daemon 或存量进程已经换版。Hermes `--version` 的落后提交数
仅为宿主提示，本审计不把它当作精确在线升级结论。

## 上游变化与 kumi 跟进

检索日期为 2026-09-23；上游说明不是本机集成证据。

### Codex

[官方更新记录](https://learn.chatgpt.com/docs/changelog)中，0.155.1（9 月 18 日发布）
修复新本地 TUI 默认 reasoning summary 导致部分 provider 拒绝请求的问题。本机安装日
是 9 月 22 日，不混淆发布日期。当前 kumi 使用的 sandbox/add-dir/cd、exec JSONL、
resume 等参数仍被本机帮助接受，未发现必须改启动 argv 的证据。

上游 9 月 23 日已发布 0.156.1，0.156.0 的可选 `/tui` 全屏、`/usage`、`/daemon`
以及 tmux/粘贴修复值得跟进。升级后要重新捕获实际界面，验证审批识别、输入提交、
拒绝/中断、后台工具和会话恢复；不自动切换 UI，也不重启用户共享 daemon。
用量面板不是按轮账本来源。本轮没有安装或运行 0.156.x。

### omp

[18.2.10](https://github.com/can1357/oh-my-pi/releases/tag/v18.2.10)改变 `hub jobs`
为不消费自动交付的精简摘要，并修复流式子任务失败重试；
[18.2.11](https://github.com/can1357/oh-my-pi/releases/tag/v18.2.11)修复后台完成事件
打断前台 Bash/eval。实际 CLI 帮助仍有精确恢复、审批模式和 `ps` 的项目过滤/停止参数。

这影响 DH-02/03/F10：应补有前台工具与后台任务并行时的拒绝、取消、迟到结果与用量
重导，记录每次 provider 重试，不把后台通知出现当成整个任务停止。本轮仅核对接口。

### Hermes：优先原生扩展

上游已发布 [0.21.4 / v2026.9.21](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.21)，
本机仍为 0.21.3。当前上游[插件接口文档](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
列出 `pre_auxiliary_call` / `post_auxiliary_call`：按 provider attempt 观察辅助调用，
包含请求身份、重试、用量及错误；父 turn/session 在部分路径可能为空。
在线文档与某个 tag 是否完全一致需按选定 checkout 再验，不能仅凭发布号宣布支持。

本机没有这两个 hook，也没有实验性 `on_aux_usage`。现有主循环插件重验通过，辅助
逐调用仍不可用。已改进 `hermes_status.py`：分别返回新观察接口的 true/false/null，
有新接口但未实现适配时明确报告 `auxiliary_observer_adapter_not_implemented`，不误报
当前 collector 已能工作。已有 `on_aux_usage` 的兼容路径和原始数据保留。

H4 后续优先固定上游 checkout，适配新的原生观察接口：落盘前只选择必要数值和身份；
核对 retry/streaming/error、缓存及 reasoning 口径、主辅重叠和 MoA 去重、后台丢事件；
缺父身份保持未知。请求、响应正文和原始错误不能因为 hook 可得就写进用量数据库。
本轮没有实现该采集器，也没有恢复本体补丁路线。

本机已经有原生 [approval transport](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#approval-transports)。
它为**人类审批呈现**提供不可变请求、ID/digest、可选范围和超时检查，适合作为 AP-04
人类窗口桥接的候选。它不是策略/自动授权 API，不把模型代审伪装成人类回答；展示文本
可能已脱敏，不能自动认为完整操作可据此判定。Jev/Laya 分流仍需独立决策接口与授权。

### OpenCode / OMO

保持已安装 1.18.29＋4.19.4 组合的现有语义；当前帮助仍有 `--pure`、精确 session 和
JSON 输出。上游 [OpenCode 1.18.32](https://github.com/anomalyco/opencode/releases/tag/v1.18.32)
列出 Together AI 流式用量修复，实际使用此 provider 时需做用量专项复测。
OMO 发布入口当前指向 [5.0.0-beta.86](https://github.com/code-yeongyu/oh-my-openagent/releases/tag/v5.0.0-beta.86)，
涉及 Native engine 和默认路由变化；这是另一条待评估迁移，不能拿 4.x 插件矩阵背书。
包管理器曾出现 2.0.5 不代表当前就是该版本，也不是升级 OMO 的授权。

## 本批实现与验证边界

- `tests/host_compat_probe.py`：固定 argv 查询版本/帮助，可指定 executable 和 OMO
  package.json；记录版本差异、参数缺失、退出状态和输出 hash。缺安装、超时、失败、
  无法解析均显式返回；OpenCode 帮助输出到 stderr 也能识别。禁止把输出正文里的
  参数子串当作选项声明。所有 `native_behavior_verified` 均保持 false。
- `tests/hermes_approval_probe.py`：使用本机原版审批模块做 **14 项**合成检查，覆盖
  deny、允许本次的正对照、async、错误类型、旧 ID/指纹、未提供的 session/永久范围、
  异常、中断、超时后迟到允许、新请求不吃旧回执和请求不可变。零真实审批/命令/模型。
  这不是插件注册到真实 TUI 的端到端验收，也不证明后台已停止。
- Hermes status 探针 **6 个配置场景**通过，profile 内容不变；hook 探针通过实际
  discovery/emitter 注入合成 provider 回执，主循环导入和重复去重通过，未启用 profile
  没有产生存储。两个探针均无新模型调用。
- 新增 **13 项**默认回归：10 项版本/帮助与失败分类、3 项新旧 Hermes hook 的能力
  区分。完整回归 **583 项通过，含 14 项真实 tmux，零跳过/失败**；分发检查 6 个
  skills、32 份 Markdown、156 个本地链接、零发现。变更 Python 的 Ruff 基础检查、
  `git diff --check` 通过，汇总见[状态页](STATUS.md)。

最初受限环境中真实 tmux 的 socket 被拒绝；在允许本地 socket 的测试环境复跑，
没有跳过这部分或把权限失败当成产品缺陷。基线总量和本次新探针结果分别记录。
探针属于开发验收，不安装进 agent；运行包仅包含能力诊断和准确的宿主说明。

## 继续执行的具体事项

以下保留 V 编号和验证范围；实际执行先刷新版本，顺序与检查点见
[综合计划](IMPLEMENTATION-PLAN.md)。9 月 24 日补查已发现 omp 18.3.0，不能按旧版本排实测。

1. **V-01 / DH-02/03/F10**：选定实际版本后验证 Codex/omp 等各宿主的原生拒绝、中断、
   前台与后台共存、取消后静默期、result/receipt 对齐；保留 OMO 双 Esc 及后台收尾专项。
2. **V-02 / H4**：在独立 checkout/profiles 固定并测试上游辅助观察接口；通过后才
   交付新的可选计量适配，继续不修改日常 Hermes 本体。
3. **V-03 / AP-04/05**：人类审批 transport 的真实注册/呈现/超时撤销与 kumi control
   ID 桥接；自动决策层、完整操作证据与人类呈现层分开验收。
4. **V-04 / F10**：Codex 0.156.x、OpenCode/OMO 新组合的独立升级矩阵；选定版本后
   检查实际 UI、身份、原生子来源、用量及后台控制，不修改当前安装来省略迁移验证。

结合综合阶段 4 的 DH-04 参数化执行和 DH-07 资源占用；以上版本项不关闭既有
全矩阵、审批时效、完整计量或三级审批待办。
