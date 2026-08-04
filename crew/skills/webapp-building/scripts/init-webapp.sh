#!/usr/bin/env bash
# init-webapp.sh — local (macOS) edition
#
# Based on webapp-building-v5/scripts/init-webapp.sh (online VM version),
# adapted for running on a local Mac:
#   - project directory is an explicit argument
#     (the VM version hardcodes /mnt/agents/output/app)
#   - dependencies are installed from the public npm registry
#     (the VM version copies a pre-baked node_modules from the image)
#   - no portal registration
#     (http://localhost:8080/api/v1/apps exists only inside the VM runtime)
#   - <title> replacement uses python3
#     (BSD sed -i on macOS is incompatible with the GNU sed syntax)
#   - no automatic git init/commit
#     (local projects may live inside an existing repo; commit manually if wanted)
#   - adds dist/ to .gitignore alongside node_modules
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# 这是最早上线的默认模板，用户如果没指定模板，则使用这个模板
ORIGIN_TEMPLATE_NAME="0-origin"

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"
TEMPLATES_DIR="$REPO_ROOT/templates"

usage() {
  cat <<'EOF'
Usage:
  init-webapp.sh --list
  init-webapp.sh <project-dir> <project-title> [template-name] [--no-install]

Examples:
  init-webapp.sh --list
  init-webapp.sh ./app "Market Dashboard"
  init-webapp.sh ./app "Gallery" airlens-style --no-install
EOF
}

list_templates() {
  find "$TEMPLATES_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort
}

# ─────────────────────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--list" ]]; then
  list_templates
  exit 0
fi

if [[ $# -lt 2 || $# -gt 4 ]]; then
  usage >&2
  exit 2
fi

PROJECT_DIR="$1"
PROJECT_TITLE="$2"
TEMPLATE_NAME="${3:-$ORIGIN_TEMPLATE_NAME}"
INSTALL_DEPS=1

if [[ "${4:-}" == "--no-install" ]]; then
  INSTALL_DEPS=0
elif [[ $# -eq 4 ]]; then
  echo "Unknown option: $4" >&2
  usage >&2
  exit 2
fi

# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

! command -v npm &>/dev/null && { echo "Error: npm not found (install Node.js 20+ first)"; exit 1; }

TEMPLATE_PATH="$TEMPLATES_DIR/$TEMPLATE_NAME"
TEMPLATE_ZIP="$TEMPLATE_PATH/$TEMPLATE_NAME.zip"
INFO_MD="$TEMPLATE_PATH/info.md"

if [[ ! -d "$TEMPLATE_PATH" || ! -f "$TEMPLATE_ZIP" ]]; then
  echo "Error: Template '$TEMPLATE_NAME' not found at: $TEMPLATE_ZIP" >&2
  echo "Available templates:" >&2
  list_templates >&2
  exit 1
fi

if [[ -e "$PROJECT_DIR" && -n "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]]; then
  echo "Error: Project directory already exists and is not empty: $PROJECT_DIR" >&2
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# Project Creation
# ─────────────────────────────────────────────────────────────────────────────

echo "Creating project: $PROJECT_DIR (template: $TEMPLATE_NAME)"
mkdir -p "$PROJECT_DIR"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

TEMP_PATH="$(mktemp -d)"
trap 'rm -rf "$TEMP_PATH"' EXIT

unzip -q "$TEMPLATE_ZIP" -d "$TEMP_PATH"

# 如果解压的目录带着模板名，则使用带着模板名的目录
SOURCE_DIR="$TEMP_PATH"
if [[ -d "$TEMP_PATH/$TEMPLATE_NAME" ]]; then
  SOURCE_DIR="$TEMP_PATH/$TEMPLATE_NAME"
else
  only_child="$(find "$TEMP_PATH" -mindepth 1 -maxdepth 1 -type d | head -n 1 || true)"
  if [[ -n "$only_child" && "$(find "$TEMP_PATH" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" == "1" ]]; then
    SOURCE_DIR="$only_child"
  fi
fi

# Copy template files (dotglob includes hidden files like .gitignore).
# Template zips contain no symlinks, so macOS cp -R is safe here.
(shopt -s dotglob nullglob; cp -R "$SOURCE_DIR"/* "$PROJECT_DIR"/)

# 模板的说明文件放到项目里，方便后续查阅
if [[ -f "$INFO_MD" ]]; then
  cp "$INFO_MD" "$PROJECT_DIR/template-info.md"
fi

# 替换 index.html 的 <title>（用 python3，避开 macOS BSD sed -i 的兼容问题）
if [[ -f "$PROJECT_DIR/index.html" ]]; then
  python3 - "$PROJECT_DIR/index.html" "$PROJECT_TITLE" <<'PY'
from pathlib import Path
import html
import re
import sys

path = Path(sys.argv[1])
title = html.escape(sys.argv[2], quote=False)
text = path.read_text(encoding="utf-8")
text = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", text, count=1, flags=re.S)
path.write_text(text, encoding="utf-8")
PY
fi

# ─────────────────────────────────────────────────────────────────────────────
# .gitignore
# ─────────────────────────────────────────────────────────────────────────────

touch "$PROJECT_DIR/.gitignore"
grep -qxF "node_modules" "$PROJECT_DIR/.gitignore" || printf '\nnode_modules\n' >> "$PROJECT_DIR/.gitignore"
grep -qxF "dist" "$PROJECT_DIR/.gitignore" || printf 'dist\n' >> "$PROJECT_DIR/.gitignore"

# ─────────────────────────────────────────────────────────────────────────────
# Dependencies
# ─────────────────────────────────────────────────────────────────────────────

if [[ "$INSTALL_DEPS" -eq 1 && -f "$PROJECT_DIR/package.json" ]]; then
  cd "$PROJECT_DIR"
  echo "Installing dependencies in: $PROJECT_DIR"
  npm install --no-audit --no-fund
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done: print template info for the AI agent to read
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "✓ Template '$TEMPLATE_NAME' initialized at: $PROJECT_DIR"
echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "TEMPLATE INFO"
echo "═══════════════════════════════════════════════════════════════════════════"
echo ""
if [[ -f "$INFO_MD" ]]; then
  cat "$INFO_MD"
else
  echo "⚠ No info.md found for this template."
fi
echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
