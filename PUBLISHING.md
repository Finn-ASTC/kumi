# 发布与维护

本源码包使用独立 Git 历史，包含六个完整 skills、安装脚本、MIT 许可证、通用文档、可复用测试和 CI。私人实验记录、原生会话、凭据及本体补丁不属于发布内容。

## 第一次推送到 GitHub

在 GitHub 创建一个空仓库，不勾选自动生成 README、LICENSE 或 .gitignore，本地已有这些文件。从**这个公开副本的根目录**执行以下步骤。

1. 将 `INTEGRATION.md` 获取源码示例中的 `OWNER/REPOSITORY` 替换为真实地址，并提交该修改。
2. 检查 `git status`、`git log --oneline` 和 `git ls-files`，确认提交及文件范围。
3. 将下面的明确占位地址替换为新仓库地址，然后添加远端并推送：

```bash
ORCH_REMOTE_URL='https://github.com/OWNER/REPOSITORY.git'
git remote add origin "$ORCH_REMOTE_URL"
git push -u origin main
```

仅在尚无 origin 时使用 `remote add`；已有远端先核对 `git remote -v`。上述命令是发布步骤说明，安装脚本不会执行它们。推送后检查 GitHub Actions 实际结果。

## 后续维护

推荐直接在公开仓库进行后续开发。若还保留私有实验仓库，仅迁移已审阅的代码、测试或文档修改，不把私有分支及其历史直接 merge/push 到公开仓库；检查普通 diff、补丁上下文、提交说明和作者信息是否含私人内容。运行包更新时保持六个 skills 为同一版本。

`.gitignore` 防止未跟踪的本地状态和 `.env` 等文件被通常的 `git add` 收入；它不能排除已跟踪内容或删除历史。新增日志、截图、数据库、压缩包和录屏时应单独审阅；测试中的合成凭据标记需与真实认证信息区分。

发布前从根目录执行：

```bash
bash -n install.sh
python3 tests/check_skill_package.py
ORCH_RUN_TMUX_TESTS=1 python3 -m unittest discover -s tests -v
git diff --check
```

这些检查不调用模型，不替代内容脱敏或真实宿主版本兼容性验收。小项目演练见[测试文档](tests/E2E.md)。分发时保留 [LICENSE](LICENSE)，涉及第三方材料时保留[对应声明](THIRD_PARTY_NOTICES.md)。
