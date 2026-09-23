# kumi 文档索引

| 阅读目的 | 入口 |
|---|---|
| 项目介绍和首次派单 | [README](../README.md) |
| 安装、复制分发、更新、卸载 | [部署与接入说明](../INTEGRATION.md) |
| 并行、权限、恢复、取消和交付 | [使用指南](../USAGE.md) |
| 能力边界、既有事项编号和后续优先级 | [状态与路线](STATUS.md) |
| 接下来逐批做什么、消息实验分支及每批完成条件 | [综合实施计划](IMPLEMENTATION-PLAN.md#下一阶段执行队列) |
| 实际宿主版本、接口验证及升级跟进项 | [宿主兼容审计](HOST-COMPATIBILITY-20260923.md) |
| Jev/Laya/Von/Foq 等决策后端的候选与评测门槛 | [审批后端选型](APPROVAL-MODEL-SELECTION.md) |
| 计量计划、阶段采集、缺口与重复导入验证 | [CQ-01 最小计量闭环](CQ1-METERING.md) |
| 按轮查询与补记验收、切轮门槛和历史兼容 | [DH-01 按轮验收](DH1-ROUND-ACCEPTANCE.md) |
| 审批证据、动作回执、停止确认与无结果取消 | [DH-02/03 首批](DH23-CONTROLS.md) |
| 同 space 默认 tab、任务＋agent 标题及桌面验证 | [UX-01 页面体验](UX1-PAGES.md) |
| 回归测试和探针 | [测试索引](../tests/README.md) |
| 恢复摘要的验证与输出规模 | [C1 验证记录](C1-RECOVERY.md) |
| 增量读取、续读与失效验证 | [C2 验证记录](C2-RECOVERY.md) |
| DataHive 实测反馈、缺口归类与默认 tab/标题需求 | [DataHive 跟进清单](DATAHIVE-FOLLOWUP.md) |
| 同伴直接交流、委托验收与网状协作演进（提案） | [网状协作设计](MESH-COLLABORATION.md) |
| DataHive 原生用量、质量证据与调度接班优化 | [成本与质量复核](DATAHIVE-COST-QUALITY.md) |
| 可恢复的小项目验收 | [E2E runner](../tests/E2E.md) |
| 发布与后续维护 | [发布说明](../PUBLISHING.md) |

正式运行参考位于 `skills/*/references/`，与六个 skills 一起分发。这里的项目文档供人阅读，不要求调度 agent 每轮加载。私人实验日志、机器安装记录及原生宿主本体补丁不包含在公开源码包中。
