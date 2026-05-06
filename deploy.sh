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

# ---------- 3. 重启后端 ----------
if systemctl is-active --quiet trading-bot 2>/dev/null; then
    log "通过 systemd 重启后端..."
    systemctl restart trading-bot
    log "后端已重启 ✓"

elif [ -f "/etc/systemd/system/trading-bot.service" ]; then
    log "启动 systemd 服务..."
    systemctl daemon-reload
    systemctl start trading-bot
    log "后端已启动 ✓"

else
    log "未检测到 systemd 服务，手动管理进程..."
    pkill -f "uvicorn app.main:app" 2>/dev/null || true
    sleep 1
    nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 \
        > logs/server.log 2>&1 &
    log "后端已启动 (PID: $!) ✓"
fi

# ---------- 4. 前端 ----------
log "===== 更新前端 ====="
cd "$FRONTEND_DIR"

npm install --silent

# 生产构建
npm run build

# 重启前端 vite preview
pkill -f "vite preview" 2>/dev/null || true
sleep 1
nohup npx vite preview --host 0.0.0.0 --port 5173 \
    > "$BACKEND_DIR/logs/frontend.log" 2>&1 &
log "前端已启动 (PID: $!) ✓"

# ---------- 5. 完成 ----------
log "========================================"
log "  部署完成！"
log "  前端: http://$(hostname -I | awk '{print $1}'):5173"
log "  后端: http://$(hostname -I | awk '{print $1}'):8000"
log "========================================"
