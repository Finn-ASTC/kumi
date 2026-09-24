# UX-02a 会话预览故障验证

日期：2026-09-24；修复前基线：`710f3b3`。

## 方法与范围

使用 `tests/session_preview_demo.py` 在指定父目录下建立独立项目和 run，跨进程调用真实
`runs.py → protocol.py → jobs.py → registry.py → capabilities.py → sessions.py` CLI。
每次执行的退出码、stdout、stderr 和总报告保存在新目录，失败也保留。模拟的会话和能力
证据都明确标记为 fixture；没有真实宿主进程、模型请求、终端发键或私人历史变动。
这是运行包的文件/CLI 集成验证，**不是实际多 agent 开发项目或原生恢复/归档验收**。

```bash
python3 tests/session_preview_demo.py --root /absolute/path/to/sandbox
```

也可将六个 skills 单独复制后，用 `--scripts /copied/skills/agent-orchestrator/scripts`
指向复制包。demo 留在开发侧，不需要作为运行依赖安装。

## 修复前后对照

| 检查 | 修复前 | 修复后 |
|---|---|---|
| verified 指向不存在的证据 | 被接受 | 拒绝；未写入登记 |
| 证据内容固定 | 没有摘要 | v2 固定 SHA-256 |
| 宿主状态/输入权未知的恢复预览 | eligible=true | eligible=false，列出具体缺口 |
| 证据内容改动后的读回 | 仍显示有效 verified | 原始声明保留，有效状态 unknown |
| 同一 job/round 多条登记 | 根据 ID 排序覆盖选择 | ambiguous_registration，不猜绑定 |
| 任务/证明正文进入目录摘要 | 没有泄漏 | 没有泄漏，并进一步去掉备注与关闭证据 |

保留的本机实验目录名（位于用户指定 sandbox，不作为可分发依赖）：

- 修复前：`kumi-session-preview-qkiwswtk`，6 项中 5 项失败。
- 修复后：`kumi-session-preview-sqzag28h`，6 项全部通过。
- 独立复制包：`kumi-session-package-fnv5ntsa/kumi-session-preview-y4alc314`，6 项全部通过。

## 增补回归

`test_capabilities.py` 覆盖不存在/相对/目录/symlink/FIFO 证据、版本与时间约束、改动/删除、
旧 v1 降级、registry 重定向、错误文件名/其他 run、锁忙、发布失败/丢回执、独立进程
读回与两个进程登记同一身份，以及四宿主五配置使用相同验证契约。

`test_sessions.py` 增补多登记歧义、profile 冲突、uncertain 提交、租约不等于输入权、
新鲜/过期/变动的 host 证据、关闭不等于停止、只读摘要、external coordinator、关闭后
幂等重试、父项丢失/关系循环、路径重定向和 CLI demo。宿主检查提炼到
`completion.host_summary()`，避免目录为了判断状态加载完整 delivery tree；原 completion
验收路径仍使用同一检查。没有扫描用户 HOME 或 native store。

本轮本机 Python 3.14.7 验证：

- 全仓库 unittest：共 709 项，694 项通过、15 项跳过，无失败。
- skills 分发检查：6 个 skills、33 个 Markdown、158 个本地链接，无 findings。
- 改动脚本 py_compile 与 `git diff --check` 通过。
- 未运行原生 opt-in tmux/模型场景，也未据此声称所有故障窗口已覆盖。

## 未完成边界

- `verified` 是调用方报告且证据 bytes 完整，不能证明任意文本声明为真。
- 当前版本/store/config 适用性、原生 adapter、输入权及消息/交接责任仍未接通；
  archive/resume 预览不会因此变为可执行。该批没有认证任何真实宿主能力。
- session 角色/关系登记不授予权限；同轮多实例的显式选择/撤销机制仍需设计。
- capability 的同 run 版本化晋级/撤销未实现；旧记录不可覆盖。旧 v1 只降级展示，不自动迁移。
- job 分页限制不涵盖整份 registration/capability 元数据；10/100/1000 项规模与所有崩溃
  边界尚未全面验收。本地锁不是针对任意外部进程或恶意改动的隔离保证。
- 原生历史隔离、picker 可见性、实际 resume/archive、通知和 token/工程质量对照仍需
  后续原生项目实测；本次不报告 token 节省比例。
