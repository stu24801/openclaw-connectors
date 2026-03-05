# RAG Knowledge Base

## Description

A self-hosted HTTP server that lets you upload documents (`.txt`, `.md`, `.pdf`) and query them via semantic search.  
The AI uses this as a **Retrieval-Augmented Generation (RAG)** knowledge base: before answering knowledge-heavy questions, it searches the base and incorporates the top results into its response.

---

## Setup (one-time)

### Option A — Docker Compose (recommended)

```bash
cd rag-knowledge-base/server
docker compose up -d
```

Server starts on **`http://localhost:8765`** (or the host/port you expose).

### Option B — Direct Python

```bash
cd rag-knowledge-base/server
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8765
```

> First startup downloads the embedding model (~120 MB). Subsequent starts are instant.

---

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `RAG_DATA_DIR` | `./data` | Where FAISS index + metadata are stored |
| `RAG_CHUNK_SIZE` | `500` | Characters per chunk (overlap = 100 chars) |
| `RAG_TOP_K` | `5` | Default number of results returned by `/search` |
| `RAG_EMBED_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Sentence-transformers model (supports Chinese + English) |

---

## API Reference

### `GET /health`
Check server status.
```json
{ "status": "ok", "ready": true, "doc_count": 42, "sources": ["manual.md", "faq.txt"] }
```

### `POST /upload` — multipart file upload
```bash
curl -X POST http://localhost:8765/upload \
  -F "file=@/path/to/document.md" \
  -F "source_name=my-doc"
```
Supports: `.txt`, `.md`, `.pdf` (pdfplumber must be installed for PDF).

### `POST /upload_text` — plain JSON upload
```bash
curl -X POST http://localhost:8765/upload_text \
  -H "Content-Type: application/json" \
  -d '{"text": "Your document text here...", "source": "inline-note"}'
```

### `GET /search?q=<query>&top_k=5&source=<optional>`
```bash
curl "http://localhost:8765/search?q=如何設定環境變數&top_k=3"
```
Returns ranked chunks with score, source, and text.

### `GET /sources`
List all uploaded sources and their chunk counts.

### `DELETE /source/{source_name}`
Remove a source and rebuild the index.

---

## AI Operating Instructions

> **When to use this skill:**  
> When the user asks a question that likely requires domain-specific knowledge (e.g., product manuals, internal docs, past conversations), first call `/search` to retrieve relevant context, then answer using that context.

### Step-by-step workflow

1. **Check health first** — `GET /health`  
   - If `ready: false`, inform the user the server is not running.
   - If `doc_count: 0`, inform the user the knowledge base is empty.

2. **Search** — `GET /search?q=<user question>&top_k=5`  
   - Use the user's question (or a cleaned-up version) as `q`.
   - If the user specifies a document/source, add `&source=<name>`.

3. **Incorporate results** — Treat the returned `text` fields as grounding context.  
   - Cite the source name when relevant: `（來源：{source}）`
   - If no relevant chunks are found (`results: []`), say so and answer from general knowledge.

4. **Upload when asked** — If the user wants to add a document:
   - Small text snippets → `POST /upload_text`
   - Files → `POST /upload` with multipart

### Example search call (pseudo-code)
```
GET http://localhost:8765/search?q={user_question}&top_k=5
→ use result[0..4].text as context
→ answer the user
```

---

## Notes

- The default embedding model supports **Traditional Chinese, Simplified Chinese, English, and Japanese**.
- All data is stored **locally** in `./data/`. No data leaves your machine.
- To reset everything: `rm -rf ./data/`
- For PDF support: `pip install pdfplumber`
