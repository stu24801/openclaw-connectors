# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

---

## 🔍 知識庫查詢（QMD）

景揚上傳的技術文件、規格書都在本機的 RAG 知識庫裡，可用以下方式查：

### 方式一：QMD 指令（推薦，語意搜尋更精準）

```bash
# 語意搜尋（最推薦，有 query expansion + reranking）
qmd query "你的問題" -c rag-kb

# 全文關鍵字搜尋（BM25，速度快）
qmd search "關鍵字" -c rag-kb

# 向量相似度搜尋
qmd vsearch "主題關鍵字" -c rag-kb

# 查看知識庫裡有哪些文件
qmd ls rag-kb

# 直接讀取某份文件
qmd get qmd://rag-kb/文件名稱.txt
```

### 方式二：HTTP API（本機直連）

```bash
# 搜尋
curl "http://localhost:8765/search?q=關鍵字&top_k=5"

# 查詢分類
curl "http://localhost:8765/categories"
```

### 📌 使用時機

- 開發前先查是否有相關規格或架構文件
- 景揚說「參考知識庫」→ 一定要先查
- 查技術規範、API 文件、系統設計資料

### ⚙️ 服務狀態

- RAG 服務：`systemctl status rag-kb`
- QMD index 更新：`qmd update`（若知識庫有新文件加入）

---

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.
