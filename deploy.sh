#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
VENV_DIR="$BACKEND_DIR/.venv"

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# ---------- 0. 检查项目目录 ----------
if [ ! -d "$BACKEND_DIR" ]; then
    log "错误: 找不到 backend 目录，请将此脚本放在项目根目录"
    exit 1
fi

cd "$PROJECT_DIR"

# ---------- 1. 拉取最新代码 ----------
log "拉取最新代码..."
git pull origin main

# ---------- 2. 后端 ----------
log "===== 更新后端 ====="
cd "$BACKEND_DIR"

if [ ! -d "$VENV_DIR" ]; then
    log "创建 Python 虚拟环境..."
    python3 -m venv .venv
fi

source "$VENV_DIR/bin/activate"
pip install -r requirements.txt -q

log "执行数据库迁移..."
mkdir -p data logs
alembic upgrade head
log "后端代码/依赖/迁移已更新 ✓（不启动进程）"

# ---------- 3. 前端 ----------
log "===== 更新前端 ====="
cd "$FRONTEND_DIR"

npm install --silent

# 生产构建（FastAPI 托管 frontend/dist/）
npm run build
log "前端已构建 ✓（不启动进程）"

# ---------- 4. 完成 ----------
log "========================================"
log "  部署完成（未启动/重启任何进程）"
log "  请在进程守护中自行重启后端"
log "  前端产物: $FRONTEND_DIR/dist"
log "========================================"
