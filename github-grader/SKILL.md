---
name: github-grader
description: >
  從 GitHub repo 取得原始碼並對照 RAG 知識庫中的題目與評分方式進行評分。
  觸發詞：「幫我評分」、「批改」、「看看我的 GitHub」、「評量我的作業」、
  「check my repo」、「評分這個 repo」。
  需要使用者提供：GitHub repo URL（支援公開 repo，私有 repo 需 token）。
---

# GitHub 作業自動評分 Skill

## 概覽

此 Skill 分為兩個子任務：

1. **`fetch-repo`** — 從 GitHub API 取得 repo 原始碼（可獨立呼叫）
2. **`grade`** — 結合題目 + 評分方式 + 原始碼 → 輸出評分報告

兩者都可透過 **RAG KB 的 `/grade_stream` SSE API** 一次完成，也可單獨呼叫 GitHub API。

---

## 子技能一：fetch-repo（取得 GitHub 原始碼）

### 用途
當使用者說「看一下這個 repo」、「幫我讀取這個專案」、「這個 GitHub 有什麼」時，
只執行 fetch-repo，不進行評分。

### 流程

```
Step 1: 解析 GitHub URL → owner / repo / branch
Step 2: 取得檔案樹（GitHub Trees API）
Step 3: 篩選並讀取主要程式碼檔案
Step 4: 整理成結構化摘要回傳
```

### Step 1 — 解析 URL

支援格式：
- `https://github.com/{owner}/{repo}` → branch 預設 `main`
- `https://github.com/{owner}/{repo}/tree/{branch}`

### Step 2 — 取得檔案樹

```http
GET https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1
Authorization: Bearer {token}   # 私有 repo 才需要
User-Agent: openclaw-grader
```

回傳的 `tree[]` 陣列中，type=blob 的是檔案。

### Step 3 — 讀取程式碼

只讀副檔名為 `.java .kt .py .ts .js .go .cs .sql .md .xml .gradle` 的檔案。
跳過路徑含 `test` `target` `build` `node_modules` `__pycache__` `.git` 的項目。
最多讀 30 個檔案，每個最多讀 3000 字元。

```http
GET https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}
Authorization: Bearer {token}   # 私有 repo 才需要
```

優先讀取順序：
1. `README.md`
2. `*Controller*`, `*Router*`
3. `*Service*`
4. `*Entity*`, `*Model*`
5. `*Repository*`, `*DAO*`
6. `*.sql`, `schema.sql`
7. `pom.xml`, `build.gradle`, `package.json`

### Step 4 — 回傳摘要

```
Repo: {owner}/{repo} @{branch}
共 {N} 個檔案，讀取 {M} 個程式碼檔案

📄 讀取的檔案：
- src/main/java/.../UserController.java  (1234 bytes)
- src/main/java/.../UserService.java     (2891 bytes)
...

📦 技術棧（依 pom.xml / build.gradle 判斷）：
- Spring Boot 3.x, JPA, MySQL

🔑 主要 Entity：User, Post, Follow, Like
```

---

## 子技能二：grade（完整評分）

### 方法 A：使用 RAG KB /grade_stream API（推薦）

直接呼叫 RAG KB 的 SSE 端點，一次取得題目 + 評分方式 + 程式碼：

```
GET http://localhost:8765/grade_stream?repo_url={url}&token={token}
```

SSE 事件：
- `event: progress` — 進度訊息
- `event: done` — 完成，data 為 JSON：
  ```json
  {
    "owner": "stu24801",
    "repo": "my-project",
    "branch": "main",
    "file_count": 45,
    "code_files_read": 12,
    "questions_available": ["社群媒體系統.md", ...],
    "grading_prompt": "你是一位嚴格但公正的後端工程師面試評審..."
  }
  ```
- `event: error` — 錯誤訊息

收到 `done` 事件後，將 `grading_prompt` 交給 AI 推論（即本對話）進行評分。

### 方法 B：手動步驟（/grade_stream 不可用時）

#### Step 1 — 從 RAG KB 取題目

```http
GET http://localhost:8765/sources
```
找 `category` 含 `題目_md` 的文件，取其 `id`：
```http
GET http://localhost:8765/doc/{file_id}
```

取評分方式：
```http
GET http://localhost:8765/doc_by_source?source_name=評分方式
```

#### Step 2 — fetch-repo（見上方子技能一）

#### Step 3 — 對照題目

| 題目 | 關鍵 Entity / 關鍵詞 |
|------|---------------------|
| 社群媒體系統 | User, Post, Like, Follow, Feed |
| 金融商品喜好紀錄 | Fund, Product, Portfolio, Transaction |
| 員工座位系統 | Employee, Seat, Office, Booking |
| 電商購物中心 | Product, Order, Cart, Payment |
| 圖書借閱系統 | Book, Borrow, Return, Member |
| 線上投票系統 | Vote, Poll, Option, Result |

#### Step 4 — 逐項評分

依 `評分方式.md` 的每個項目評定：

```
✅ [項目名稱] — 已完成
   說明：在 XxxController.java 第 42 行實作 POST /api/users。

⚠️ [項目名稱] — 部分完成（扣 X 分）
   說明：缺少密碼加鹽處理。

❌ [項目名稱] — 未完成
   說明：未找到 JWT 相關實作。
```

#### Step 5 — 輸出報告

```markdown
# 📋 評分報告

## 基本資訊
- GitHub Repo: {url}
- 分支: {branch}
- 對應題目: {題目名稱}
- 評分時間: {timestamp}

## 🎯 題目判定
（說明判斷依據，引用 Entity 名稱或 README）

## 📊 評分結果

### {大項一}（滿分 X 分）
| 評分細項 | 狀態 | 說明 |
|---------|------|------|
| 項目A   | ✅   | ...  |

### {大項二}（滿分 X 分）
...

## 💯 總分
| 大項 | 得分 | 滿分 |
|------|------|------|
| ... | ... | ... |
| **總計** | **XX** | **100** |

## 💡 改進建議
1. ...
2. ...

---
*評分依據：RAG 知識庫「評分方式.md」*
*題目來源：RAG 知識庫「{題目名稱}」*
```

---

## 注意事項

| 情況 | 處理方式 |
|------|---------|
| 公開 repo | 直接讀取，不需 token |
| 私有 repo | 請使用者提供 `ghp_xxxx` token |
| 大型 repo（>200 檔） | 優先讀關鍵目錄，不需全讀 |
| 無法判斷題目 | 請使用者確認，或列出最可能的 2 個 |
| 多道題目在同一 repo | 分別評分或請使用者指定 |
| GitHub API rate limit | 提示使用者提供 token（即使公開 repo token 也可提升限制） |

---

## 相關 URL

- **RAG KB 評分 UI**：`https://rag.alex-stu24801.com/grade_ui`
- **RAG KB SSE API**：`http://localhost:8765/grade_stream?repo_url=...`
- **RAG KB 文件 API**：`http://localhost:8765/sources`、`/doc/{id}`、`/doc_by_source`
