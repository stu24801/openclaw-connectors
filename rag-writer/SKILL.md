---
name: rag-writer
description: >
  Submit articles to the RAG Knowledge Base writer portal for the owner to read and download.
  Use when a writer (寫手蝦) wants to upload or publish a Markdown article.
  Supports submitting article content directly via API without needing to use the web UI.
---

# RAG Writer Skill

Writer portal URL: `https://rag.alex-stu24801.com/writer/login`
Submit API endpoint: `POST https://rag.alex-stu24801.com/writer/api/submit`

## Submit an Article via API

```bash
curl -s -X POST https://rag.alex-stu24801.com/writer/api/submit \
  -H "Content-Type: application/json" \
  -H "X-Writer-Token: $WRITER_TOKEN" \
  -d '{
    "title": "文章標題",
    "author": "作者名稱",
    "content": "# 標題\n\n內文...",
    "note": "備註（選填）"
  }'
```

## Web UI Flow

1. 前往 `https://rag.alex-stu24801.com/writer/login`
2. 輸入投稿密碼（由 owner 提供）
3. 填寫標題、作者、備註，上傳 `.md` 檔案
4. 點「送出投稿」

## Notes
- 文章格式請使用 Markdown（`.md`）
- 投稿後 owner 可在文章庫瀏覽、閱讀、下載
- `WRITER_TOKEN` 由 owner 提供，請妥善保存
