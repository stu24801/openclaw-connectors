# RAG Knowledge Base

## Description

A self-hosted HTTP server with **web UI + password auth** for uploading documents and using them as a RAG knowledge base.  
Supports `.txt`, `.md`, `.pdf` uploads, semantic search via sentence-transformers + FAISS, and a dark-mode dashboard.

---

## Setup

### Option A — Docker Compose (recommended)

```bash
cd rag-knowledge-base/server

# 1. Set your password in docker-compose.yml → RAG_PASSWORD
# 2. Launch
docker compose up -d
```

Open **`http://localhost:8765`** → enter password → start uploading.

### Option B — Direct Python

```bash
cd rag-knowledge-base/server
pip install -r requirements.txt
RAG_PASSWORD=yourpassword uvicorn main:app --host 0.0.0.0 --port 8765
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `RAG_PASSWORD` | `changeme` | Web UI login password |
| `RAG_DATA_DIR` | `./data` | Storage path for files + FAISS index |
| `RAG_CHUNK_SIZE` | `500` | Characters per chunk |
| `RAG_TOP_K` | `5` | Default search results count |
| `RAG_EMBED_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Multilingual embedding model |

---

## Web UI Pages

| Path | Description |
|---|---|
| `/` | Redirects to login or dashboard |
| `/login` | Password login page |
| `/dashboard` | Upload files, view file list with download/delete |
| `/search_ui` | Browser-based semantic search |
| `/logout` | Clear session |

---

## JSON API (for AI use — no auth required for read)

### `GET /health`
```json
{ "status": "ok", "ready": true, "doc_count": 42, "sources": ["manual.md"] }
```

### `GET /search?q=<query>&top_k=5`
Returns top ranked chunks. The AI uses this for RAG.
```bash
curl "http://localhost:8765/search?q=如何設定環境變數&top_k=3"
```

### `POST /upload_text`
```json
{ "text": "...", "source": "note-1" }
```

### `GET /sources`
List all indexed sources.

---

## AI Operating Instructions

> Use this skill when the user asks questions that require domain knowledge stored in the knowledge base.

### Workflow

1. **Check server** — `GET /health`  
   - `ready: false` → server not running, ask user to start it.  
   - `doc_count: 0` → knowledge base is empty.

2. **Search** — `GET /search?q=<user question>&top_k=5`  
   - Use the user's question as `q`.  
   - `results[].text` → use as grounding context.  
   - Cite source: `（來源：{source}）`

3. **No results** → answer from general knowledge, note that the KB had no matching context.

4. **Upload via API** (when user pastes text) → `POST /upload_text`  
   For files, direct user to the web UI (`/dashboard`).
