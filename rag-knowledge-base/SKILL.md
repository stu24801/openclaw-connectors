---
name: rag-kb
description: >
  Query the local RAG Knowledge Base for domain-specific documents uploaded
  by the user. Use when the user asks questions that may be answered by their
  personal knowledge base, internal documents, or asks you to "look up",
  "find in the knowledge base", or "check the KB".
  Supports category filtering: use ?category=分類名稱 to narrow results.
endpoints:
  base: http://localhost:8765
  health: GET /health
  search: GET /search?q={query}&top_k=5[&category={category}]
  categories: GET /categories
  sources: GET /sources
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
