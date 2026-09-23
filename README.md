# kumi

让一个终端 agent 通过 **herdr / tmux** 调用其他 agent：派发任务、并行推进、处理权限和追问、恢复中断任务，再独立验证交付。

支持 **omp（oh-my-pi）、Hermes、OpenCode / OMO、Codex** 之间的协作。项目由六个 skills 和标准库 Python 辅助工具组成，安装到已有 agent 中使用；无需部署 Web 服务或常驻调度服务，可直接从本仓库安装，无需额外的 skill 管理仓库。

**kumi** 是项目和仓库名称。安装后的主 skill 仍叫 `agent-orchestrator`；六个 skill 名称、命令与任务状态路径保持兼容，下文派单时使用实际 skill 名称。

```text
调度 agent ──准备任务契约──→ herdr / tmux ──→ 子 agent
    │                                          │
    ├──推进自己的独立工作                      ├──执行 / 提问 / 报错
    ├──读取巡视事件、处理权限                  └──发布本轮 JSON 结果
    └──恢复任务、独立验收 ←── 持久化任务状态与交付证据
```

## 快速开始

### 1. 准备环境

- Linux / 类 Unix 本地环境；当前实测以 Linux 为主，调度方与子 agent 共享文件系统。
- Bash、Python **3.10+**。
- **herdr 或 tmux**；需要图形 space/tab 时使用 herdr，只有终端时可先用 tmux。
- 实际要使用的 agent CLI 已安装、完成登录和模型配置，能在目标工作目录独立运行。无需安装全部四种。
- 调度方能读取 skills、执行命令，并有权限启动本地终端进程。安装 skills 不会增加宿主权限。

### 2. 获取并安装

从 GitHub 仓库的 **Code** 菜单复制 clone 地址或下载 ZIP，放到准备长期保留的目录。进入仓库根目录，选择要加载这些 skills 的宿主，例如：

```bash
bash install.sh --target codex
```

支持 `omp`、`hermes`、`opencode`、`codex`、`all`；**无参数仅安装到 omp 和 Hermes**。每个选中的宿主都安装全部六个 skills，以便承担调度或被调度角色。

安装使用软链接，保留仓库目录。已有同名软链接会改为指向本仓库，已有实目录/普通文件会跳过；若已有安装，先核对来源。自定义路径、复制安装、更新和卸载见[部署与接入说明](INTEGRATION.md)。

### 3. 发起第一项任务

在安装后的宿主中新开会话，进入一个测试项目，确认能发现 `agent-orchestrator`，然后直接说：

> 使用 agent-orchestrator，让 omp 只读检查当前项目的测试结构，列出三个改进点，不修改项目文件。使用 tmux。你同时检查 README，最后核对它的结论。

将 `omp` 换成实际已配置的目标。使用 herdr 时可以说：

> 让 Hermes 检查错误处理，在新的 space 打开，命名“检查错误处理”；保留我当前页面的焦点。你继续检查测试，并定期关注它的权限申请。

这一步会实际调用模型，使用已有账号和计费配置。只想离线检查安装，可运行[不启动 agent 的验证示例](INTEGRATION.md#离线检查安装)。

## 日常使用

- **页面组织**：在身份已确认的 herdr 会话中，默认每个子 agent 在调度方同一 workspace（space）中新建 tab，保留当前焦点；可显式指定新 space，需要分屏时明确提出。标题包含任务和 agent 类型，例如“实现上传重试 · Codex”。
- **并行与巡视**：提交后，调度方继续做不依赖子任务的工作，在工作段之间检查监督状态和权限申请。观察器默认约 15 秒检查一次；30 秒注意力响应是目标，长期达标仍待实测。
- **恢复**：每项任务保留 run/job/round 和精确终端身份。可以把 `run.json` 路径交给新会话继续处理；提交是否成功不明时先核查，避免重复执行。
- **交付**：子 agent 的 `success` 之后，还要独立验收并确认原生后台已收尾，才能完成任务。
- **节省上下文**：任务包、事件去重、按需读证据和用量账本用于减少重复上下文；目前没有证明多 agent 一定比单 agent 省 token。

完整示例、查看页面、追问、恢复、取消和计量见[使用指南](USAGE.md)。多个 space/tab 共享文件系统；并行写代码应使用不同工作树或明确文件分工。

## 哪些内容需要安装

**日常只需 `skills/` 下六个完整同级目录。** `references/`、`scripts/`、`assets/` 和随包插件是运行内容，要一起保留。

| 内容 | 用途 |
|---|---|
| `skills/agent-orchestrator/` | 调度流程，以及协议、巡视、恢复、验收、用量工具 |
| `skills/agent-controlled/` | 子 agent 的回传契约 |
| `skills/agent-{omp,hermes,opencode,codex}/` | 四类宿主的操作字典 |
| `install.sh` | 安装工具，只链接六个 skills |
| `tests/` | 开发回归、E2E runner 和演练；日常使用不需要 |
| `docs/`、`.github/` | 项目资料与 CI；不安装给 agent |

Hermes 默认使用原版宿主；普通编排不要求用量插件。可选插件记录主循环调用，辅助调用精确归属和完整计量仍有缺口；安装不会修改 Hermes 本体，也不会启用 Relay。

## 当前状态与开发

[当前状态与路线](docs/STATUS.md)统一记录已实现能力、验证边界和待办。已有真实宿主小项目验收；长时间并行监督、桌面 viewer 验收与单／多 agent 成本对照仍待补齐。观察器不会自动唤醒已结束的调度方会话，本项目尚不提供无人值守调度服务。

开发检查从仓库根目录执行：

```bash
bash -n install.sh
python3 tests/check_skill_package.py
python3 -m unittest discover -s tests -v
# 安装 tmux 并允许本地 socket 后，启用真实终端回归（不调用模型）
ORCH_RUN_TMUX_TESTS=1 python3 -m unittest discover -s tests -v
```

更多内容见[测试说明](tests/README.md)、[E2E runner](tests/E2E.md)、[文档索引](docs/README.md)。本公开源码包保留可复用的工具和测试，不包含本机实验日志或私人管理资料。

本项目采用 [MIT 许可证](LICENSE)，第三方材料保留[上游版权声明](THIRD_PARTY_NOTICES.md)。发布步骤见[发布说明](PUBLISHING.md)。
