# 可复用 E2E runner

**这是项目开发/验收工具，不属于 agent 接入包。** 日常安装只同步 `skills/` 六个完整目录，详见[接入清单](../INTEGRATION.md)；本目录分类见[测试索引](README.md)。

从仓库根目录执行 `python3 tests/e2e_runner.py --help`。runner 将现有 protocol/jobs/runs/watch/delivery/completion 串成可恢复的显式步骤，不是常驻调度器。每条命令输出一份 JSON；退出码 0 表示步骤成功，`verify` 的 1 表示验收拒收，2 表示操作失败。传输返回成功仍不证明任务已被接收。

## 无模型的小项目演练

```bash
ORCH_TEST_ROOT="$(mktemp -d /tmp/orch-tests.XXXXXX)"
python3 tests/e2e_demo.py --root "$ORCH_TEST_ROOT"
```

需要 Python 3.10+、tmux 和本地 socket 权限；不需要模型、登录信息或宿主安装。每次创建新的 `runner-demo-*/`，保留 scenario、CLI 输入/输出、两份金额项目、run/index、快照和独立验收目录。失败记录为 `failure.json`，成功记录为 `report.json`。它会清理本次自有的 tmux 会话，保留磁盘证据。

演练包含两个目标、四轮提交：作者提问 → 先报告错误实现成功 → 独立拒收 → 修复通过；验证目标经过**合成权限画面**。控制器在目标尚未完成时，实际运行针对错误种子的负对照；每次 runner 调用都是新进程，并从共享 run 恢复旧 waiting_user。检查每轮只收到一次输入、原始种子不变、任务窗口标题和旁侧 sentinel 存活。最后记录合成原生身份、观察真实夹具进程退出，通过完成门禁。

fixture 只能在 `init --fixture` 时选择，只支持私有 tmux；普通 runner 没有自动批准、任意按键或自动退出宿主的接口。demo 中 `ALLOW` 只发给它自己启动的确定性程序。这里验证真实终端传输和控制流程，**不证明真实模型能力、原生审批识别率、长期响应时效或 token 节省**。

```bash
# 全部回归，包括 13 项真实 tmux 测试；普通 CI 也使用此入口
ORCH_RUN_TMUX_TESTS=1 python3 -m unittest discover -s tests -v
# 只运行新端到端演练
ORCH_RUN_TMUX_TESTS=1 python3 -m unittest discover -s tests -p test_e2e_integration.py -v
```

## 准备真实宿主场景

真实宿主启动是单独的显式操作。先按对应宿主 skill 核对已安装版本、模型、审批设置及运行依赖。runner 不安装扫描器、不复制凭据、不创建 profile alias、不修改用户模型或审批配置；预检中的人工结论必须有实际证据。

准备绝对路径的只读种子目录和已有测试父目录，后者不能位于种子内部。下面保存为 `scenario.json`，替换种子及观察路径；`test_money.py` 应是种子中独立编写的 oracle。复制目标列表即可添加互补任务，每个目标都会获得自己的完整副本。

```json
{
  "version": 1,
  "observe_paths": ["/absolute/seed-money"],
  "targets": [{
    "name": "author",
    "label": "修复十进制金额换算",
    "kind": "omp",
    "seed": "/absolute/seed-money",
    "task_packet": {
      "objective": "修复金额转换的浮点截断问题",
      "scope": "仅修改 money.py；test_money.py 由独立验证方维护",
      "acceptance": ["十进制金额 0.29、-0.29、1.10 分别得到 29、-29、110 分"]
    },
    "scope": {"include": ["."], "exclude": ["__pycache__"]},
    "verification_plan": {
      "commands": [{"argv": ["python3", "-B", "test_money.py"], "timeout_seconds": 10}],
      "artifacts": [], "dependencies": {}, "environment": {}
    }
  }]
}
```

`kind` 支持 omp/codex/hermes/opencode。OpenCode 保持当前 OMO 默认；只有显式 `"opencode_mode":"pure"` 才加 `--pure`。标签可使用中文任务目的，内部 agent ID 独立生成。种子不支持软链接；初始化在启动宿主前捕获每个目标的编辑前基线。详细契约出错时可能保留部分 lab 和 `initialization-failure.json`，此时没有启动宿主。

```bash
ORCH_TEST_ROOT="$(mktemp -d /tmp/orch-native.XXXXXX)"
python3 tests/e2e_runner.py init --root "$ORCH_TEST_ROOT" --input scenario.json
```

后续用返回的 `runner_path`；下面用 shell 变量 `RUNNER` 指代它。lab/run 路径固定，运行中不要移动证据目录。`observe_paths` 只比较显式声明路径的元数据和内容 hash，不保存外部文件正文、不限制宿主写入，也不能归因是谁修改了路径。不要把全部 home 放入观察范围。

预检 JSON 示例（`evidence_path` 必须指向已经存在的本次证据）：

```json
{
  "host_version": "实际查到的宿主版本",
  "evidence_path": "/absolute/preflight-evidence.json",
  "note": "已核对版本、运行依赖与既有模型/审批配置",
  "checks": {"model_and_approvals_preserved": true, "runtime_dependencies_verified": true}
}
```

Hermes 还需先按[私有存储配方](../skills/agent-hermes/references/isolation.md)准备 `<lab>/profiles/<name>`，在预检 JSON 添加 `hermes_home`、`scanner_sha256`，以及 checks 中的 `private_home_verified`、`automatic_maintenance_disabled`。runner 核对私有 config、扫描器可执行性/hash 和 `hermes config path`；将 `HERMES_HOME` 通过新页面的 `--env` / tmux `-e` 传入。布尔值本身不是自动核验全部配置的证明。

## 启动、输入与监督

herdr 使用**已存在且显式选定**的 session，runner 不负责启停服务。默认新建任务命名的 workspace/初始 tab，并传 `--no-focus`。在现有 herdr 内操作时提供精确父 ID，禁止 `focused` 等别名：

```bash
python3 tests/e2e_runner.py start --runner "$RUNNER" --target author --input preflight.json \
  --session existing-session --parent-pane w1:p1 --parent-tab w1:t1
# 若选择新 tab：在上式增加 --layout tab，父 pane/tab 均必填
# tmux：改用 --transport tmux，省略 herdr 的 session/parent 参数
```

tmux 使用 `<lab>/tmux.sock`，每目标独立会话。socket 路径过长时会在分配前拒绝，应换较短的测试父目录。四宿主启动参数按当前配方组合；当前公开回归的范围见[状态与路线](../docs/STATUS.md)；持续响应时效与 start_uncertain 的全宿主故障恢复仍未整体验收。

```bash
python3 tests/e2e_runner.py inspect --runner "$RUNNER" --target author > inspection.json
```

检查 `observation`、精确资源身份和 UI 当前是否可输入，再编辑这份 JSON 的 `ready:true` 与说明；**不要直接自动翻转该字段**。`inspect` 默认 false。`submit --input inspection.json` 要求检查时间在 30 秒内、资源/request 完全一致，并再次读取 live UI；已知工作中、权限框、blocked 或 dead 时不发输入。未知 TUI 必须有非空的实际 UI，依赖控制器的新鲜判断；此例外只允许该次输入，不解除 checkpoint 对 unknown 的零工作预算。owner/实际观察器/身份/寿命/成功读取仍必须有效。重读、监督检查与发送之间不是原子操作，进程可在之后退出，不能宣称未来覆盖保证。

```bash
python3 tests/e2e_runner.py watch-init --runner "$RUNNER"
```

**先建立监督，再提交业务。** 以受管句柄启动返回的 `observer_argv`，确认各目标首次成功检查，并按下方 `monitor` 登记真实负责人/有效期。之后再取得新的 `inspect`（旧检查可能已超过 30 秒）、人工确认可输入，再 `submit --target author --input inspection.json`。投递门槛会检查当前轮的 owner、watch、近期成功检查、活动观察器及实际剩余寿命；缺失/到期/即将到期时，在 `begin` 和发送输入之前拒绝，任务仍为 prepared。

提交前先持久记录 uncertain，客户端超时不自动重发。看到同轮有效结果后可提交 receipt：

```json
{"status":"accepted","kind":"matching_result","evidence_path":"/absolute/observed-result.json","note":"已核对当前轮结果"}
```

使用 `receipt --target author --input receipt.json` 记账。其他接收/明确未发送的证据类型见[提交与恢复](../skills/agent-orchestrator/references/submission.md)；超时、没有输出均不能作为 rejected/not_sent。

`watch-init` **只建 watch**，返回 `observer_argv`，不会启动后台观察器。控制器应以受管句柄启动该命令（默认 15 秒间隔、300 秒时长）；新 runner 的投递门槛要求实际活动的受管观察器。手工 `poll` 用于观察器停机的接续检查，不能独自满足该投递门槛，也不能与同 watch 的活动观察器争锁。观察器到期、换轮、更换 watch 后，应重新核对覆盖范围并接续监督。

用 `monitor --target author --input monitor.json` 记录每目标负责人、watch、有效期和受管句柄/轮询责任证据：

```json
{
  "monitor": {"owner":"controller-handle","watch_path":"/absolute/watch.json","expires_at":1890000000},
  "evidence_path":"/absolute/observer-handle.json",
  "note":"已检查受管观察句柄或控制器轮询责任"
}
```

替换时间为真实到期 Unix 秒值。此回执不会启动进程，也不证明监督者一直存活；应通过 run 恢复中的健康字段核对最近成功观察。续轮会清除旧 monitor，需要重新登记。控制器投递后继续自己的独立工作，消费权限/提问事件，只有实际依赖时才有界等待。

每段独立工作之前执行 `checkpoint --runner "$RUNNER"`。它复用正式 `scripts/supervision.py`，返回是否可工作、最长工作片段和下次检查时刻；有待处理事件、过期检查、失活观察器或默认 60 秒内需要续接时，返回零预算/退出码 1。coverage 的 `renew_by` 给出续接边界，实际启动/检查/绑定耗时较长时应增大正式 helper 的 `--renew-before`。已询问的 waiting_user 保留，不阻止兄弟任务推进。该命令没有终端输入能力，也不会自动唤醒控制器或续约。完整接续顺序见[监督检查点](../skills/agent-orchestrator/references/supervision.md#checkpoint-before-work-and-input)。

## 恢复、返工与完成

| 操作 | 行为与证据要求 |
|---|---|
| `recover` | 只读共享 run 的任务、未解决事件、waiting_user 和声明路径变化；不会补发输入 |
| `checkpoint` | 读取待办和当前监督覆盖，给下一段独立工作预算；未知/失效/即将到期时要求先处理 |
| `bind --target … --input …` | 仅处理 start_uncertain；输入含 `resources`、`evidence_path`、`note`。核对原启动 session/父资源/归属、原资源文件（若有）和 live 身份后恢复 started，不分配或重启 |
| `claim --target … --input …` | 确认前控制器已停后，用 evidence_path/note 显式接管过期租约；保留 uncertain，不表示允许重发 |
| `follow --target … --input packet.json --note …` | 同 job 和资源的新轮，要求作者明确交接；新建编辑前基线并保留旧结果 |
| `reconcile-round --target …` | 从已保存 candidate_round 恢复激活中断；不再次 prepare 或发送，只接续尚未提交的候选轮 |
| `verify --target … --note …` | 同轮有效 success 后捕获固定源码副本，在独立目录执行 scenario 中的 plan，记录 accepted/rejected。重跑新增 attempt，旧证据不改写 |
| `native --target … --input …` | 显式绑定查证后的 native session_id/turn_id，附 evidence_path/note；不按时间猜归属 |
| `host --target … --input …` | 保存外部声明路径的后快照，并按 completion schema 登记真实宿主收尾事实 |
| `close --target … --evidence … --note …` | 只有当前轮 success、独立验收、有效宿主收尾均满足时 completed |
| `cleanup-plan --target …` | 仅输出归属范围内的清理建议；不执行 |

所有操作都带 `--runner "$RUNNER"`。`native` 输入形如 `{"native":{"session_id":"精确原生ID","turn_id":null},"evidence_path":"/absolute/native-proof.json","note":"查证过程"}`。`host` 的检查项、有效期和子会话谱系见[完成检查](../skills/agent-orchestrator/references/completion.md)，不可用虚构 clear 替代未知。

若 start 的分配回执丢失，先从命令证据与原 session 检查实际目标，再构造 bind。若资源文件已存在，使用其**完整原记录**，不能替换；若只建了页面、没有可确认的运行宿主，不会通过 live 绑定，应保留失败证据并按已知归属人工收尾。

`test_startup_recovery.py` 在真实私有 tmux 创建完成后直接退出测试控制器，分别覆盖消费回执前、保存 resources 后的中断；恢复时断言仅一次创建、同一 pane/PID、旧资源字节不变、submission 仍 prepared、没有业务输入，旁侧 sentinel 保留。这些确定性检查不代表所有宿主和任意故障点都已覆盖；长时原生监督仍属[待办](../docs/STATUS.md)。

权限处置仍使用原[持久待办流程](../skills/agent-orchestrator/references/supervision.md)，逐条记录 waiting_user/handled 及版本，换 watch 不丢弃旧待办。runner 不代为批准，也不会把旧批准用于新弹窗。完成后依宿主字典执行 scoped 取消/退出，观察前台、后台、原生子任务和副作用后再登记 host；结果发布本身不等于宿主已停。

现场确认一次权限处置的结果后，按[立即记账顺序](../skills/agent-orchestrator/references/supervision.md#record-each-confirmed-outcome-before-the-next-item)写入该 watch/seq 的证据与回执，再扫描全量恢复、处理下一弹窗或做独立工作。若只成功发送按键而无法确认，保持 open。正文提到“批准/拒绝”本身不再触发 attention；缺少提示也不代表可以输入，仍须执行现场检查和监督门槛。[权限识别回归](test_permission_detection.py)使用脱敏画面，[终端回归](test_tmux_integration.py)覆盖连续合成权限；这些检查不是长期原生时效测试。

runner 的 flock 和 jobs 租约只约束合作控制器，不阻止绕过协议的外部终端输入。默认租约 3600 秒；长任务到期前通过 jobs 的 `renew` 接口续期（当前 revision/token，payload 为 `{"lease_seconds":3600}`）。只有过期且前任已停时才 claim。

步骤不是跨文件/终端的事务：candidate 保存前崩溃仍可能留下未激活目录，可从 run inventory 找到；验证完成但记账中断也应检查独立 attempt 后协调恢复。不要因一条命令失败就从 start/submit 重跑整条流程。此入口先固化可审查步骤，自动调度、原生身份发现、用量导入和长时四宿主验证留给后续工作。
