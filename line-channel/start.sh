#!/bin/bash

# 切換到腳本所在目錄
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 載入環境變數
if [ -f .env ]; then
    export $(cat .env | grep -v '#' | xargs)
fi

# 設定預設值
TUNNEL_NAME=${TUNNEL_NAME:-claw-webhook}
TUNNEL_URL=${TUNNEL_URL:-"(未設定網址)"}
CLOUDFLARED_CONFIG=${CLOUDFLARED_CONFIG:-"$HOME/.cloudflared/config.yml"}
TUNNEL_LOG=${TUNNEL_LOG:-"$HOME/clawd/tunnel.log"}

echo "=== LINE Bridge & Tunnel 啟動檢查 ==="
echo "設定: Tunnel名稱=$TUNNEL_NAME | URL=$TUNNEL_URL"
echo "Cloudflared config: $CLOUDFLARED_CONFIG"

# 1. 檢查 Cloudflared 執行檔
if [ ! -f "./cloudflared" ]; then
    echo "⚠️  找不到 cloudflared，正在下載..."
    curl -L --output cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
    chmod +x cloudflared
fi

# 2. 啟動 Node.js LINE Bridge
if pgrep -f "node index.js" > /dev/null; then
    echo "✅ LINE Bridge (App) 正在執行中"
else
    echo "🚀 啟動 LINE Bridge..."
    nohup node index.js > line_bridge.log 2>&1 &
    sleep 2
    if pgrep -f "node index.js" > /dev/null; then
        echo "   -> 啟動成功 (Port ${PORT:-8081})"
    else
        echo "   -> ❌ 啟動失敗，請檢查 line_bridge.log"
        exit 1
    fi
fi

# 3. 啟動 Cloudflare Tunnel
# 使用 --config 指定設定檔路徑，確保正確載入憑證
if pgrep -f "cloudflared tunnel" > /dev/null; then
    echo "✅ Cloudflare Tunnel ($TUNNEL_NAME) 正在執行中"
else
    echo "🚀 啟動 Cloudflare Tunnel: $TUNNEL_NAME ..."
    if [ -f "$CLOUDFLARED_CONFIG" ]; then
        nohup ./cloudflared tunnel --config "$CLOUDFLARED_CONFIG" run "$TUNNEL_NAME" > "$TUNNEL_LOG" 2>&1 &
    else
        echo "   ⚠️  找不到 config.yml ($CLOUDFLARED_CONFIG)，使用預設憑證..."
        nohup ./cloudflared tunnel run "$TUNNEL_NAME" > "$TUNNEL_LOG" 2>&1 &
    fi
    sleep 4
    if pgrep -f "cloudflared tunnel" > /dev/null; then
        echo "   -> ✅ Tunnel 已建立 ($TUNNEL_URL)"
    else
        echo "   -> ❌ Tunnel 啟動失敗，請檢查 $TUNNEL_LOG"
        echo "      (提示: 請確認 ~/.cloudflared/config.yml 與憑證檔存在)"
        exit 1
    fi
fi

echo "=== 啟動完畢 ==="
echo "Webhook URL: $TUNNEL_URL/line/webhook"
