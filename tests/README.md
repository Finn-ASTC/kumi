# 项目测试与实验工具

**整个 `tests/` 都是开发/验收内容，不安装到 agent 的 skill 目录。** 正式运行工具位于 [`skills/agent-orchestrator/scripts/`](../skills/agent-orchestrator/scripts/)，接入边界见[接入与同步清单](../INTEGRATION.md)。

| 文件 | 分类 | 如何使用 |
|---|---|---|
| `test_*.py` | 自动回归 | unittest discover；大部分离线，真实 tmux 测试显式启用 |
| `test_runs.py` | 持久恢复回归 | 包含 B1 发现的原生空元数据目录误报；保留非空/符号链接/未登记任务报警，[原生监督报告](../docs/B1-SUPERVISION.md)另列真实模型实验的失败与边界 |
| `test_recovery_summary.py` | C1 恢复摘要回归 | 多规模分页、页外错误与健康、旧问题/新权限身份、只读与时间过期；[验证记录](../docs/C1-RECOVERY.md) |
| `test_recovery_delta.py`、`recovery_benchmark.py` | C2 增量恢复回归与规模测量 | 续读/缓存失效、丢失输出、分页积压与断点；benchmark 在独立父目录保留无模型夹具，支持 `--skills-root`；[验证记录](../docs/C2-RECOVERY.md) |
| `test_usage_sources.py` | 来源绑定审计回归 | run 历史/漏绑、原生父子关系、四宿主证据边界、轮次/账本 scope 与有效计数核对；无模型调用 |
| `test_metering.py` | CQ-01 计量检查点回归 | controller/原生子来源、不可变计划、实际 CLI 重导、缺口/未知、失败续采及四宿主计量边界；无模型调用 |
| `test_round_acceptance.py` | DH-01 按轮验收回归 | 历史查询/补记、切轮时状态、成功验收门槛、blocked/error 续轮、跨轮 attempt 拒绝及关闭后受限验收租约 |
| `test_pages.py`、`herdr_pages_probe.py` | UX-01 页面验证 | 离线规划/命名回归；探针在独立真实 herdr 会话验证 tab、焦点和清理，显式 `--viewer` 另需 kitty/niri 桌面，不调用模型 |
| `test_hermes_hooks.py`、`hermes_hook_probe.py` | 可选 Hermes 用量插件验证 | 回归检查重试身份/缺记录/隐私/绑定；probe 通过已安装 Hermes 的真实加载与 hook 路径注入合成响应，不调用模型 |
| `test_hermes_aux.py`、`hermes_aux_probe.py` | 辅助回执验证 | 去重、缺失计数、未知归属与旧主循环存储；probe 默认合成响应，显式 `--template-home` 才复制既有配置进行标题/审批真实调用；需兼容的原生 hook |
| `test_hermes_status.py`、`hermes_status_probe.py` | 分层前置诊断 | 能力/配置/插件/历史存储独立判断；probe 在真实宿主解释器做 A→B→A 和兼容矩阵，验证 profile 字节不变，不调用模型 |
| `test_startup_recovery.py` | 启动中断回归 | 私有 tmux 真实创建后，在两个持久化边界退出控制器；新控制器恢复原 pane/PID，不重启、不提前提交 |
| `test_permission_detection.py`、`fixtures/permission-screens.json` | 权限误报回归 | 脱敏界面形状、正文/历史误报、部分提示和未知 UI 复核；真实 tmux 连续权限流程另在 `test_tmux_integration.py` |
| `test_reporting_recipe.py`、`test_wait_output.py`、`test_supervision_checkpoint.py` | B2 发布、日志等待与接续回归 | stdin 精确发布、日志游标/文件边界、提前续接预算；[B2 验证记录](../docs/B2-SUPERVISION.md)另列独立应用及短原生实验，30 秒时效仍未通过 |
| `test_verifier_acceptance.py` | 验证轮验收示例回归 | 实际执行运行文档中的计划：拒收永远返回通过的 oracle，拒绝借用作者 attempt；验证者通过后仍须独立宿主收尾 |
| `check_skill_package.py` | 分发包引用检查 | CI 执行；检查本地链接与已知散文引用模式，不调用模型，不安装给 agent |
| `e2e_runner.py` | 当前可复用 E2E 测试入口 | 显式场景与步骤，串联正式工具；native start 可以实际启动模型宿主 |
| `e2e_demo.py` | 无模型的小项目演练 | 调用 runner，使用真实 tmux 和确定性程序，保留实验目录 |
| `delivery_config_demo.py` | 交付配置负对照与返工演练 | 真实 Rust debug/release、产物检查与完成门禁；不启动终端或模型，需 Linux /proc、Cargo/Rust/rustfmt/clippy |
| `E2E.md` | runner 操作说明 | 场景、预检、提交、监督、恢复和验收步骤 |
| `fixtures/` | 确定性模拟目标 | 由回归/演练启动，不是要安装的 agent 或 skill |


本目录保留可复用测试和探针，真实模型实验从 [E2E.md](E2E.md) 开始。私有环境的旧实验脚本及其专用回归未包含在本公开包中。

## 常用命令

在仓库根目录执行：

```bash
# 分发包引用检查：范围和限制见接入说明的“离线检查安装”
python3 tests/check_skill_package.py

# 标准回归，无模型；默认跳过 opt-in tmux 测试
python3 -m unittest discover -s tests -v

# 包含真实终端传输，仍不调用模型；需要 tmux 和本地 socket 权限
ORCH_RUN_TMUX_TESTS=1 python3 -m unittest discover -s tests -v

# 无模型小项目，使用独立测试父目录并保留证据
ORCH_TEST_ROOT="$(mktemp -d /tmp/orch-tests.XXXXXX)"
python3 tests/e2e_demo.py --root "$ORCH_TEST_ROOT"

# 交付配置负对照；离线构建，保留原始失败和修正后的独立验收证据
python3 tests/delivery_config_demo.py --root "$ORCH_TEST_ROOT"
```

`e2e_runner.py` 的确定性模式需在 init 时显式选择 `--fixture`；普通 native 模式会在 start 时启动实际宿主。需要做真实 agent 验收时，应核对场景、宿主配置和作用域后按说明分步执行。

保留模式演练产生的 `runner-demo-*`、`orch-e2e-*`、run/index、项目副本、快照和宿主回执都是某次实验的证据，不同步到 skills，demo 会保留它们；自动回归用例则会清理各自的临时目录。保留的证据与可重新生成的 `__pycache__` 不同。

`delivery_config_demo.py` 显式运行，不加入默认 unittest 或 CI 的 Rust 依赖。
它复制 `fixtures/delivery-config/` 中刻意带有断言副作用的源码到新的
`delivery-config-demo-*`，证明 debug-only 计划能漏掉 release 挂起；再运行
发布版测试和独立产物 oracle、核对超时进程组、拒绝失败验收及 completed，
只修 sandbox 副本并以新轮次/快照复验。修正后运行的是运行包中的
[Rust 执行计划](../skills/agent-orchestrator/assets/verification/rust-cli-plan.json)，
另外检查 fmt/clippy。原夹具必须保持错误，直接对它运行 release 测试会挂起。

演练的作者结果由确定性脚本生成；没有原生宿主观察，因此最终只证明交付验收
通过，`host_settled` 和 `ready_to_complete` 仍为 false。这不是四宿主 E2E 或
计划完整性自动推断。运行包保留配方与执行计划示例；这个演练和错误 Rust
项目都留在 tests，不同步给 agent。
