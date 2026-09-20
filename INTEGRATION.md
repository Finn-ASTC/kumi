# kumi 部署与接入说明

这是一套安装到现有终端 agent 的 skills 和本地工具。**直接使用本仓库即可，无需额外的管理脚本或另一份管理仓库。** 基本使用见 [README](README.md)，任务操作见[使用指南](USAGE.md)。

## 环境与宿主准备

| 依赖 | 要求 |
|---|---|
| 操作系统 | Linux / 类 Unix；目前实测以 Linux 为主，其他系统需核对终端和文件锁行为 |
| Bash | 运行安装脚本和终端配方 |
| Python | 3.10+；正式编排工具只使用标准库，无需 pip 安装本项目依赖 |
| 终端传输 | herdr 或 tmux，且允许本地 socket 和受管后台进程 |
| 调度宿主 | 能发现 skills、读写任务状态、调用命令并维持监督责任 |
| 目标 agent | 所选 CLI 可执行，已完成自己的认证、模型和权限配置 |
| 文件系统 | 调度方、目标 agent 能读取同一任务目录和结果路径 |

先按所选宿主和终端工具的安装说明安装它们，并在工作目录手动启动一次目标确认可用。只用 tmux 时无需 herdr。安装脚本不会安装这些软件、复制凭据、修改模型/审批设置或启动模型。

OpenCode 是否加载 OMO 取决于自己的安装和配置，不由本项目安装。`--pure`、插件命令等应先核对当前版本的帮助与实际行为；不能把历史实测机器的配置当成其他机器的默认值。宿主差异见各自的 `SKILL.md`。

## 获取源码

从仓库页面 **Code → Clone** 获取实际地址，或下载 ZIP 解压：

```bash
ORCH_REPO_URL='https://github.com/Finn-ASTC/kumi.git'
git clone "$ORCH_REPO_URL" kumi
cd kumi
```

软链接安装需要保留这份目录；建议放在固定位置。ZIP 和复制分发同样支持安装，不要求有 `.git/`。

仓库名为 `kumi`，宿主发现的六个 skill 名称保持不变；主 skill 仍为 `agent-orchestrator`，任务状态仍使用原有 `agent-orchestrator` 路径。

## 选择安装目标

在仓库根目录运行，例如：

```bash
bash install.sh --target omp
```

`--target` 可选 `default|omp|hermes|opencode|codex|all`。无参数或 `default` 只安装到 omp 和 Hermes；`all` 安装到四类宿主。**每个选中的宿主都获得六个 skills**，并不要求启动所有 agent。调度方需要加载主流程；目标也安装六件套可直接发现受控协议和宿主字典。生成的任务提示词包含核心契约，但未安装 skill 的目标不能假定已经具备全部配方。

| 宿主 | 默认安装目录 | 覆盖变量 |
|---|---|---|
| omp | `~/.omp/agent/skills/` | `OMP_SKILLS_DIR` |
| Hermes | `~/.hermes/skills/autonomous-ai-agents/` | `HERMES_SKILLS_DIR` |
| OpenCode | `~/.config/opencode/skills/` | `OPENCODE_SKILLS_DIR` |
| Codex | `~/.agents/skills/` | `CODEX_SKILLS_DIR` |

OpenCode 未设专用变量时，依次使用 `OPENCODE_CONFIG_DIR/skills` 或 `${XDG_CONFIG_HOME:-~/.config}/opencode/skills`。覆盖目录必须是宿主实际扫描的位置；设置安装变量不会改变宿主的发现规则。例如给独立 Hermes profile 安装：

```bash
HERMES_SKILLS_DIR=/absolute/profile/skills/autonomous-ai-agents \
bash install.sh --target hermes
```

脚本先检查六份 `SKILL.md`，再创建目标目录及链接。行为如下：

- 已有同名软链接会被重建，**包括原本指向其他来源的链接**；执行前核对已有安装。
- 已有实目录或普通文件保留并提示 `skip`，不自动更新；退出成功不代表这些副本已更新。
- 不提供 `--force`。需要从旧副本迁移时，先比较和备份独立修改，再处理具体的冲突目录。
- 安装后新开宿主会话，检查是否发现 `agent-orchestrator`；已运行会话可能仍保留旧的 skill 内容。

## 复制安装与最小分发包

无需链接时，把 `skills/` 下六个目录完整复制到宿主发现目录，排除 `__pycache__/` 和 `*.pyc`。它们必须同级：

```text
<宿主 skills 目录>/
├── agent-orchestrator/    # SKILL.md、references/、scripts/、assets/
├── agent-controlled/
├── agent-omp/
├── agent-hermes/          # 包含可选 plugins/orch-usage/
├── agent-opencode/
└── agent-codex/
```

不要只复制 `SKILL.md`，也不要把整个仓库嵌套成一个 skill。包内相对引用和 Python 模块导入要求保留完整结构。复制到另一机器后，避免软链接仍指向源机器的目录。

用于安装的最小分发包是同级的 `install.sh` 和完整 `skills/`；分发时还应附 [MIT 许可证](LICENSE)，一并分发第三方材料时保留[对应声明](THIRD_PARTY_NOTICES.md)。用户阅读文档可以随包提供，但运行工具不依赖项目根目录的文档或 tests。

| 路径 | 是否接入宿主 skill 目录 |
|---|---|
| 六个 `skills/agent-*` 的全部正式内容 | **是**，同一版本完整接入 |
| `install.sh` | 否，仅安装时使用 |
| `README.md`、`INTEGRATION.md`、`USAGE.md`、`docs/` | 否，供人查阅 |
| `tests/`，包括 E2E runner、演练和夹具 | 否，仅开发验收 |
| `.github/`、`.git/` | 否，分别属于 CI、版本管理 |
| run/job/round、原生日志、实验产物 | 否，独立保存的任务状态 |

正式运行工具位于 `agent-orchestrator/scripts/`：`protocol.py` 负责契约和结果，`jobs.py`/`runs.py` 负责提交与恢复，`watch.py`/`supervision.py` 负责巡视与工作预算，`delivery.py`/`completion.py` 负责独立交付验证，`usage.py` 及其配套模块负责用量与来源审计。这些模块要一起复制；**不需要启动 E2E runner 才能使用它们**。

`wait_output.py` 也是运行包的一部分：任务提供可读、只追加日志时，用它等待检查点，避免原生工具一直等到业务进程结束。它不负责启动业务、审批或自动唤醒，使用边界见[检查点等待](skills/agent-orchestrator/references/supervision.md#waiting-for-native-checkpoints)。

## 离线检查安装

从仓库根目录执行；复制安装时，把 `ORCH_SCRIPTS` 改为所安装 `agent-orchestrator/scripts` 的绝对路径。

```bash
ORCH_SCRIPTS="$PWD/skills/agent-orchestrator/scripts"
python3 "$ORCH_SCRIPTS/protocol.py" prepare \
  --cwd "$PWD" --parent-depth 0 --task-file - --brief <<'TASK'
只读检查项目结构，列出三个改进点，不修改项目文件。
TASK
```

它会创建持久化任务目录，返回 `run_path`、`request_path`、`prompt_path`、`result_path`、`index_path` 等；**不会启动 agent、发送任务或调用模型**。读取 `prompt_path` 对应文件可查看契约，结果文件尚不存在是正常的。要保留独立测试位置，可先 `runs.py init --root /absolute/existing/test-parent`，再给 prepare 传返回的 `--run`。

在同一 shell 中，把以下占位值换成刚才返回的绝对路径：

```bash
RUN_FILE=/absolute/returned/run.json
REQUEST_FILE=/absolute/returned/request.json
INDEX=/absolute/returned/jobs
python3 "$ORCH_SCRIPTS/jobs.py" register --index "$INDEX" --request "$REQUEST_FILE"
python3 "$ORCH_SCRIPTS/runs.py" recover --run "$RUN_FILE"
```

这里仅登记刚生成、确定未发送的请求，恢复结果应保持 prepared。没有 watcher、原生会话或验收证据是预期状态；不要把旧的可能已发送任务重新登记为新任务。这项检查证明准备/登记/恢复路径能执行，不证明宿主发现、终端或模型链路可用。

完整开发仓库还可运行分发包引用检查：

```bash
python3 tests/check_skill_package.py
# 检查独立复制的六个同级 skill；检查器仍从开发仓库运行
python3 tests/check_skill_package.py --root /absolute/distribution/skills
```

检查器验证已识别的本地链接和散文引用模式，零发现不是所有运行路径自包含的证明；还需最小包执行和人工复核。详细开发测试见[测试索引](tests/README.md)。

## 更新和卸载

链接安装会读取当前仓库的文件。先处理自己的修改，再更新至选定的版本；不要在未核对兼容性时替换活动任务正在使用的工具。更新后新会话重新加载；仓库移动后，在新位置重新运行选定目标的安装命令。

复制安装应比较并保留独立修改，再用同一版本更新完整六件套。仅更新 README、tests 或项目报告，不需要重新安装运行包。对外部管理工具没有要求；自行管理副本时，只需维护完整性、版本和实际宿主发现路径。

本项目没有卸载命令。确认任务已交接或结束后，在选中的宿主发现目录检查六个条目；软链接确认指向本项目，再逐个 `unlink /absolute/skills/agent-name`；实目录先备份并核对后处理。不要删除整个宿主 skills 目录。卸载可选 Hermes 插件时，先用对应 profile 的 `plugins disable orch-usage`，再处理该插件目录。

卸载 skills 不会结束已启动的会话，也不会删除任务状态或原生日志。

## 可选 Hermes 用量插件

默认保持宿主源码原样，普通派单不要求用量插件。`agent-hermes/plugins/orch-usage/` 随运行包分发，但 **install.sh 不启用它**。

需要未来主循环调用的回执时，按[插件配方](skills/agent-orchestrator/references/hermes-hooks.md)确认实际 profile、安装插件、显式启用并在新进程验证；每个独立 profile 分别接入。前置检查使用[分层支持配方](skills/agent-orchestrator/references/hermes-support.md)，其中宿主探针需要目标 Hermes 自己的解释器和依赖。

原生数据库提供累计只读信息，不能重建过去的逐调用/逐轮计数。缺少原生辅助 hook 时精确辅助计量保持未知；不将累计差值分摊到某轮或与主循环回执相加。公开包不包含 Hermes 本体实验补丁；Relay 仍属独立研究，不是部署步骤。安装不修改 Hermes 本体、运行时函数或 provider。

## 任务状态放在哪里

默认位于 `$XDG_STATE_HOME/agent-orchestrator/runs/`；未设置有效绝对路径时使用 `~/.local/state/agent-orchestrator/runs/`。显式 `--root` 或 `--temporary` 使用各自的位置；正式长任务宜保留持久 run。

状态目录保存请求、结果、待办、身份和证据引用，不是可再生成的安装缓存。任务包含绝对路径，搬动目录不能自动迁移恢复。原生宿主日志另外保存在宿主自己的存储中。公开仓库无需携带这些记录；需要分享问题时，选取必要且已脱敏的证据。
