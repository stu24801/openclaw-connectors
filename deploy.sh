#!/bin/bash
# deploy-rag-kb.sh — 從 GitHub 拉最新版 main.py 並重啟 rag-kb
set -e

REPO_DIR="/opt/rag-kb"
SERVER_SRC="$REPO_DIR/rag-knowledge-base/server/main.py"
DEPLOY_TARGET="$REPO_DIR/main.py"

echo "📦 git pull..."
cd "$REPO_DIR"
git pull origin main

echo "📋 複製 main.py..."
cp "$SERVER_SRC" "$DEPLOY_TARGET"

echo "🔄 重啟 rag-kb..."
kill $(pgrep -f "uvicorn main:app.*8765") 2>/dev/null || true
echo "✅ systemd 會自動重啟，等待 10 秒..."
sleep 10

echo "🩺 健康檢查..."
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
