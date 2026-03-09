---
name: rag-kb
description: >
  Query the local RAG Knowledge Base for domain-specific documents uploaded
  by the user. Use when the user asks questions that may be answered by their
  personal knowledge base, internal documents, or asks you to "look up",
  "find in the knowledge base", or "check the KB".
  Supports category filtering: use ?category=分類名稱 to narrow results.
  Also supports uploading text/markdown content and generating a public download link.
endpoints:
  base: http://localhost:8765
  health: GET /health
  search: GET /search?q={query}&top_k=5[&category={category}]
  categories: GET /categories
  sources: GET /sources
  upload_text: POST /upload_text  (body: {text, source, category})
  download: GET /download/{file_id}
  filemeta: /opt/rag-kb/data/filemeta.json  (local JSON, contains file_id mapping)
public_base_url: https://rag.alex-stu24801.com
---

# RAG Knowledge Base Skill

## How to use

1. Call `GET /health` to confirm the service is online.
2. (Optional) Call `GET /categories` to see available categories.
3. Call `GET /search?q={user_question}&top_k=5` to retrieve relevant passages.
   - Add `&category={category}` to filter by a specific category or subtree.
   - Category uses prefix matching: `技術文件` matches `技術文件/Java`, `技術文件/Python`, etc.
4. Include `results[].body` as context in your answer.
5. Always cite the source: `（來源：{title}，分類：{category}）`

## Response format

```json
{
  "query": "your query",
  "category_filter": null,
  "results": [
    {
      "score": 0.63,
      "title": "Document Title",
      "category": "技術文件/Java",
      "file": "uuid.txt",
      "body": "relevant passage text..."
    }
  ]
}
```

## Category system

Documents are organized in hierarchical categories using `/` as separator:
- `技術文件/Java`
- `人事/規範`
- `產品/規格書`

Use `GET /categories` to list all categories with document counts.

# RAG Knowledge Base Skill

## How to use

1. Call `GET /health` to confirm the service is online.
2. (Optional) Call `GET /categories` to see available categories.
3. Call `GET /search?q={user_question}&top_k=5` to retrieve relevant passages.
   - Add `&category={category}` to filter by a specific category or subtree.
   - Category uses prefix matching: `技術文件` matches `技術文件/Java`, `技術文件/Python`, etc.
4. Include `results[].body` as context in your answer.
5. Always cite the source: `（來源：{title}，分類：{category}）`

## Response format

```json
{
  "query": "your query",
  "category_filter": null,
  "results": [
    {
      "score": 0.63,
      "title": "Document Title",
      "category": "技術文件/Java",
      "file": "uuid.txt",
      "body": "relevant passage text..."
    }
  ]
}
```

## Category system

Documents are organized in hierarchical categories using `/` as separator:
- `技術文件/Java`
- `人事/規範`
- `產品/規格書`

Use `GET /categories` to list all categories with document counts.

---

## Upload & Public Download Link

Use this flow when the user asks you to generate a document (e.g. proposal, report, summary) and make it downloadable via a public URL.

### Steps

**Step 1 — Upload text content**

```
POST http://localhost:8765/upload_text
Content-Type: application/json

{
  "text": "<full markdown or plain text content>",
  "source": "<slug-style-name>",       // e.g. "openclaw-proposal-v1"
  "category": "<category>"             // e.g. "提案文件"
}
```

Response:
```json
{"source": "openclaw-proposal-v1", "category": "提案文件", "total_docs": 9}
```

**Step 2 — Find the file_id**

Read `/opt/rag-kb/data/filemeta.json` (local file) and find the entry where `source_name` matches the `source` value used in Step 1.

```bash
python3 -c "
import json
with open('/opt/rag-kb/data/filemeta.json') as f:
    data = json.load(f)
for item in data:
    if item.get('source_name') == 'YOUR_SOURCE_NAME':
        print(item['id'])
"
```

**Step 3 — Generate public download URL**

```
https://rag.alex-stu24801.com/download/{file_id}
```

Present the link to the user. The file is immediately downloadable without authentication.

### Notes
- Files are stored as `.txt` on disk but can contain markdown content — the download link will serve them as-is.
- The `source` field becomes the filename (`source.txt`), so use a meaningful slug.
- Files are also indexed into the RAG search index automatically after upload.
- To update a document, upload again with the same `source` name and find the new `file_id` from `filemeta.json`.
