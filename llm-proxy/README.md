# LLM Proxy

OpenAI-compatible LLM proxy，透過 OpenClaw 已配置的 **GitHub Copilot** 憑證轉發請求，**無需額外 API Key**。

## 架構

```
任何 OpenAI-compatible 客戶端 (banana-slides / curl / etc.)
  → LLM Proxy (port 9000, FastAPI)
    → 驗證 internal PROXY_TOKEN
    → 讀取 OpenClaw GitHub Copilot token (~/.openclaw/credentials/github-copilot.token.json)
    → 轉發至 api.githubcopilot.com/chat/completions
    → 可選：RAG 知識庫自動注入 (設定 RAG_ENDPOINT)
    → 記錄每日 JSONL 日誌
```

## 支援模型（GitHub Copilot 已開通）

| Model ID | 說明 |
|---|---|
| `gemini-3.1-pro-preview` | Google Gemini 3.1 Pro（預設） |
| `gemini-2.5-pro` | Google Gemini 2.5 Pro |
| `gemini-3-flash-preview` | Google Gemini 3 Flash |
| `gpt-5` | OpenAI GPT-5 |
| `claude-sonnet-4-5` | Anthropic Claude Sonnet |

## 快速部署

```bash
# 1. 安裝依賴
pip install fastapi uvicorn httpx

# 2. 設定環境變數
cp .env.example .env
# 編輯 .env，填入 PROXY_TOKEN 和路徑

# 3. 安裝 systemd service
sudo cp llm-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llm-proxy

# 4. 測試
curl http://127.0.0.1:9000/health
```

## 整合 banana-slides

在 `/opt/banana-slides/.env` 設定：

```env
AI_PROVIDER_FORMAT=openai
OPENAI_API_KEY=<your-PROXY_TOKEN>
OPENAI_API_BASE=http://127.0.0.1:9000/v1
TEXT_MODEL=gemini-3.1-pro-preview
IMAGE_CAPTION_MODEL=gemini-3.1-pro-preview
```

## 開啟 RAG 知識庫注入

設定 `.env` 中的 `RAG_ENDPOINT`：

```env
RAG_ENDPOINT=http://127.0.0.1:8765/search
```

啟用後，每個 prompt 會自動搜尋知識庫，並將相關段落注入 system message。

## API

### POST /v1/chat/completions

OpenAI-compatible endpoint。

```bash
curl -X POST http://127.0.0.1:9000/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.1-pro-preview","messages":[{"role":"user","content":"你好"}]}'
```

### GET /v1/models

列出可用模型。

### GET /health

健康檢查，回傳 token 有效狀態。

## 日誌

每日寫入 `LOG_DIR/<YYYY-MM-DD>.jsonl`，格式：

```json
{"id":"a1b2c3d4","ts":"2026-03-05T05:30:00Z","model":"gemini-3.1-pro-preview","elapsed_s":2.3,"in_chars":120,"out_chars":85}
```
