# LLM Proxy

OpenAI-compatible LLM proxy，透過 OpenClaw 已配置的 **GitHub Copilot** 憑證轉發文字請求，圖片生成走 **Google Gemini API**，無需多個客戶端設定。

## 架構

```
任何 OpenAI-compatible 客戶端 (banana-slides / curl / etc.)
  → LLM Proxy (port 9000, FastAPI)
    → 驗證 internal PROXY_TOKEN
    ├─ chat/completions (無 image modality)
    │    → 讀取 OpenClaw GitHub Copilot token
    │    → 轉發至 api.githubcopilot.com/chat/completions
    │    → 可選：RAG 知識庫自動注入 (設定 RAG_ENDPOINT)
    └─ chat/completions (modalities=["image"]) 或 images/generations
         → 呼叫 Google Gemini API (GOOGLE_API_KEY)
         → 回傳圖片 base64 (multi_mod_content 格式，banana-slides 相容)
    → 記錄每日 JSONL 日誌
```

## 支援模型

### 文字（GitHub Copilot）

| Model ID | 說明 |
|---|---|
| `gemini-3.1-pro-preview` | Google Gemini 3.1 Pro（預設） |
| `gemini-2.5-pro` | Google Gemini 2.5 Pro |
| `gemini-3-flash-preview` | Google Gemini 3 Flash |
| `gpt-5` | OpenAI GPT-5 |
| `claude-sonnet-4-5` | Anthropic Claude Sonnet |

### 圖片（Google Gemini API 直連）

| Model ID | 說明 |
|---|---|
| `gemini-3-pro-image-preview` | Gemini 3 Pro Image（預設圖片模型） |
| `gemini-2.0-flash-exp-image-generation` | Gemini 2.0 Flash Image |

## 快速部署

```bash
# 1. 安裝依賴
pip install fastapi uvicorn httpx pillow

# 2. 設定環境變數
cp .env.example .env
# 編輯 .env，填入 PROXY_TOKEN、GOOGLE_API_KEY 和路徑

# 3. 安裝 systemd service
sudo cp llm-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llm-proxy

# 4. 測試
curl http://127.0.0.1:9000/health
```

## 整合 banana-slides

在 banana-slides 的設定（`PUT /api/settings` 或 `.env`）設定：

```env
AI_PROVIDER_FORMAT=openai
OPENAI_API_KEY=<your-PROXY_TOKEN>
OPENAI_API_BASE=http://172.18.0.1:9090/v1   # 透過 docker-portforward
TEXT_MODEL=gemini-3.1-pro-preview
IMAGE_MODEL=gemini-3-pro-image-preview
```

搭配 `docker-portforward` 模組，banana-slides container 透過 `172.18.0.1:9090` 存取主機的 llm-proxy。

## 開啟 RAG 知識庫注入

設定 `.env` 中的 `RAG_ENDPOINT`：

```env
RAG_ENDPOINT=http://127.0.0.1:8765/search
```

## API

### POST /v1/chat/completions

OpenAI-compatible endpoint。加入 `"modalities": ["text", "image"]` 即自動走 Gemini 圖片生成。

```bash
# 文字
curl -X POST http://127.0.0.1:9000/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.1-pro-preview","messages":[{"role":"user","content":"你好"}]}'

# 圖片
curl -X POST http://127.0.0.1:9000/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3-pro-image-preview","modalities":["text","image"],"messages":[{"role":"user","content":"blue sky"}]}'
```

### POST /v1/images/generations

OpenAI-compatible 圖片生成 endpoint（直接走 Gemini API）。

### GET /v1/models

列出可用模型。

### GET /health

健康檢查，回傳 token 有效狀態。

## 日誌

每日寫入 `LOG_DIR/<YYYY-MM-DD>.jsonl`，格式：

```json
{"id":"a1b2c3d4","ts":"2026-03-05T05:30:00Z","model":"gemini-3.1-pro-preview","elapsed_s":2.3,"in_chars":120,"out_chars":85}
```
