---
name: line-bridge-shrimp
description: 專業的 LINE 橋接器。支援身份識別、多群組回覆，並將機敏資訊外部化儲存。
---

# LINE Bridge (by 蝦蝦)

這是一個將 LINE Messaging API 與 Clawdbot 大腦串接的技能。

## 特點
- **身份識別**：大腦能辨識說話者的 LINE 顯示名稱。
- **門禁系統**：僅限授權的群組或用戶對話。
- **安全設計**：所有機敏 ID 與 Token 均儲存於外部隱藏檔。

## 安裝與設定
本技能預設會從您的家目錄 (`~`) 讀取設定檔。您也可以透過環境變數 `LINE_BRIDGE_CONFIG_DIR` 自定義存放路徑。

請在指定目錄下建立以下檔案：
1. `.line_token`: 填入您的 LINE Channel Access Token。
2. `.line_secret`: 填入您的 LINE Channel Secret。
3. `.line_allowed_sources`: 每行填入一個授權的 User ID 或 Group ID。

**進階設定**：
如果您想自定義目錄，啟動時請執行：
`LINE_BRIDGE_CONFIG_DIR=/your/path node index.js`

## 執行與維運

### 初始化設定
首次使用前，請複製設定檔範本並填入您的資訊：
```bash
cp .env.example .env
nano .env  # 填入您的 Tunnel 名稱與網址
```

### 一鍵啟動 (推薦)
本技能包含一個自動化腳本，會同時檢查並啟動 Node.js 服務與 Cloudflare Tunnel。
若是機器重開機，請執行此指令即可恢復服務：

```bash
./skills/line-channel/start.sh
```

### 手動執行
1. **啟動應用程式**：
   ```bash
   node index.js
   ```
   預設 Port: `8081`

2. **啟動 Tunnel**：
   ```bash
   ./cloudflared tunnel run <TUNNEL_NAME>
   ```
   
## 檔案結構
- `index.js`: 主程式
- `start.sh`: 自動啟動腳本
- `.env`: 環境變數 (需自行建立)
- `cloudflared`: Tunnel 執行檔 (由 start.sh 自動下載)
