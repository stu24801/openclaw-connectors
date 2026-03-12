# 長期記憶 (Long-Term Memory)

此檔案為核心知識庫，記錄重要專案架構、設定細節、系統限制與踩坑經驗。

## 專案：DeepWiki 部署架構與設定
- **部署時間**：2026 年 3 月 11 日
- **環境**：GCP 雙核心、3.8GB RAM 虛擬機 (資源吃緊，開機常有高負載影響 SSH 登入)
- **網址**：`https://deepwiki.alex-stu24801.com`
- **網路存取**：透過 Cloudflare Tunnel (`cloudflared-tunnel.service`) 直接將外部流量導向內部服務。
- **密碼保護**：啟用內建驗證 (`DEEPWIKI_AUTH_MODE=true`)，密碼設定為 `qA!`。

### 服務架構 (Systemd)
- **Frontend (Next.js)**：
  - 服務名稱：`deepwiki-web.service`
  - 通訊埠：3002
- **Backend (FastAPI)**：
  - 服務名稱：`deepwiki-api.service`
  - 通訊埠：8001
- **LLM Proxy**：
  - 本地代理伺服器 (Port 9000)，使用 GitHub Copilot 憑證轉發。
- **Embedder**：
  - 考量到本機記憶體不足（不適合跑 Ollama），改用 Google Gemini 的 API (`gemini-embedding-001`)。

### 🚨 踩坑紀錄與重大修正 (Lessons Learned)
1. **Frontend 無限載入 Bug (Auth Loading)**
   - **現象**：開啟密碼保護後，頁面卡死在 `Loading...`。
   - **原因**：Next.js 前端渲染前依賴 `/api/lang/config` 取得語言選單，但後端的全域驗證把這個 API 也鎖死了，導致請求 403 失敗。
   - **解法**：修改 `api/api.py`，將 `/lang/config` 端點獨立排除在 `WIKI_AUTH_MODE` 的驗證之外。
2. **WebSocket 端點重大安全漏洞 (Authentication Bypass)**
   - **現象**：原版程式碼中，負責處理主要 Wiki 生成的 `/ws/chat` WebSocket 端點，**完全沒有實作任何密碼驗證邏輯**。
   - **解法**：
     - 後端：修改 `api/websocket_wiki.py`，在連線時嚴格比對傳入的 `authorization_code` 是否與 `WIKI_AUTH_CODE` 一致，若不符則關閉連線 (code 1008)。
     - 前端：修改 `src/app/[owner]/[repo]/page.tsx` 中的 `addTokensToRequestBody`，將前端輸入的密碼 (`authCode`) 夾帶進 WebSocket 請求中發送給後端。

