#!/usr/bin/env bash
# 把 agent-orchestrator 的 skills 以 symlink 安装到各 agent 的 skills 目录。
# 用 symlink（而非 cp -r）避免副本漂移：改仓库即生效。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS="$REPO/skills"

# 各 agent 的 skills 目录（可用环境变量覆盖）
OMP_SKILLS="${OMP_SKILLS_DIR:-$HOME/.omp/agent/skills}"
HERMES_SKILLS="${HERMES_SKILLS_DIR:-$HOME/.hermes/skills/autonomous-ai-agents}"
OPENCODE_SKILLS="${OPENCODE_SKILLS_DIR:-${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}/skills}"
CODEX_SKILLS="${CODEX_SKILLS_DIR:-$HOME/.agents/skills}"

usage() {
  echo "Usage: bash install.sh [--target default|omp|hermes|opencode|codex|all]"
  echo "Default installs into omp and Hermes. OpenCode and Codex are opt-in."
  echo "Override destinations with OMP_SKILLS_DIR, HERMES_SKILLS_DIR, OPENCODE_SKILLS_DIR, CODEX_SKILLS_DIR."
}

INSTALL_TARGET=default
while [ "$#" -gt 0 ]; do
  case "$1" in
    --target)
      if [ "$#" -lt 2 ]; then usage >&2; exit 2; fi
      INSTALL_TARGET="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
case "$INSTALL_TARGET" in
  default|omp|hermes|opencode|codex|all) ;;
  *) echo "Unknown target: $INSTALL_TARGET" >&2; usage >&2; exit 2 ;;
esac

NAMES=(agent-orchestrator agent-controlled agent-omp agent-hermes agent-opencode agent-codex)

# 在修改安装目录前检查源文件，避免残缺 checkout 产生失效链接。
for s in "${NAMES[@]}"; do
  if [ ! -f "$SKILLS/$s/SKILL.md" ]; then
    echo "缺少 skill: $SKILLS/$s/SKILL.md" >&2
    exit 1
  fi
done

link_dir() {
  local dst="$1"
  mkdir -p -- "$dst"
  for s in "${NAMES[@]}"; do
    local target="$dst/$s"
    if [ -L "$target" ] || [ ! -e "$target" ]; then
      if [ -L "$target" ]; then
        unlink -- "$target"
      fi
      ln -s -- "$SKILLS/$s" "$target"
      echo "✓ $s -> $target"
    else
      echo "⚠ skip $target (已存在且非 symlink；如要换成链接请手动删除)"
    fi
  done
}

case "$INSTALL_TARGET" in
  default) link_dir "$OMP_SKILLS"; link_dir "$HERMES_SKILLS" ;;
  omp) link_dir "$OMP_SKILLS" ;;
  hermes) link_dir "$HERMES_SKILLS" ;;
  opencode) link_dir "$OPENCODE_SKILLS" ;;
  codex) link_dir "$CODEX_SKILLS" ;;
  all) link_dir "$OMP_SKILLS"; link_dir "$HERMES_SKILLS"; link_dir "$OPENCODE_SKILLS"; link_dir "$CODEX_SKILLS" ;;
esac
