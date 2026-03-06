---
name: rag-kb
description: Query the local RAG Knowledge Base for domain-specific documents uploaded by the user. Use this when the user asks questions that may be answered by their personal knowledge base, uploaded documents, manuals, notes, or any custom content stored in the RAG system. The KB is hosted at http://localhost:8765.
---

# RAG Knowledge Base

Self-hosted knowledge base with web UI. Upload documents → semantic vector search powered by QMD (sqlite-vec + GGUF embeddings).

## Production URL

**`https://rag.alex-stu24801.com`** (nginx + Let's Encrypt TLS)

---

## Setup

```bash
cd rag-knowledge-base/server
pip install -r requirements.txt
# Set env vars in .env:
#   RAG_PASSWORD=yourpassword
#   QMD_BIN=/path/to/qmd   (e.g. /home/user/.npm-global/bin/qmd)
RAG_PASSWORD=yourpassword uvicorn main:app --host 0.0.0.0 --port 8765
```

### systemd .env variables

| Variable | Default | Description |
|---|---|---|
| `RAG_PASSWORD` | `changeme` | Web UI login password |
| `RAG_DATA_DIR` | `./data` | Storage path |
| `QMD_BIN` | `qmd` | Full path to qmd binary |
| `RAG_TOP_K` | `5` | Default search results |

---

## Architecture

Documents are written as markdown chunks into `$RAG_DATA_DIR/kb_docs/` which is a QMD collection (`rag-kb`). Embedding and retrieval are handled by QMD — the same SQLite index used by OpenClaw memory.

```
~/.cache/qmd/index.sqlite
  ├── openclaw-engram/   ← OpenClaw memory
  └── rag-kb/            ← uploaded documents
```

---

## Web UI

| Path | Description |
|---|---|
| `/` | Login or dashboard |
| `/dashboard` | Upload files (.txt/.md/.pdf), manage documents |
| `/search_ui` | Browser-based semantic search |

---

## JSON API (no auth required for read)

### `GET /health`
```json
{ "status": "ok", "backend": "qmd+sqlite-vec", "doc_count": 3 }
```

### `GET /search?q=<query>&top_k=5`
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

1. **Search first** — `GET /search?q=<user question>` before answering from general knowledge
2. **Cite source** — use `（來源：{title}）` from results
3. **No results** → answer from general knowledge, note KB had no match
4. **New uploads** → embedding runs async, wait ~10s before searching
