---
name: github-grader
description: >
  自動評量使用者提供的 GitHub repo 特定分支，對照 RAG 知識庫中的「題目_md」
  與「評分方式.md」進行評分並輸出結構化評分報告。
  當使用者說「幫我評分」、「批改」、「看看我的 GitHub」、「評量我的作業」、
  「check my repo」時觸發此 Skill。
  需要使用者提供：GitHub repo URL（含分支名稱）。
---

# GitHub 作業自動評分 Skill

## 整體流程

```
Step 1: 從 RAG KB 取出所有題目 + 評分方式全文
Step 2: 從 GitHub API 取得 repo 的檔案結構
Step 3: 讀取各主要程式碼檔案內容
Step 4: 分析每個檔案對應哪一道題目
Step 5: 逐題按評分方式評分
Step 6: 輸出完整評分報告
```

---

## Step 1 — 取得題目與評分方式

### 1a. 列出知識庫中的所有題目

```http
GET http://localhost:8765/sources
```

回傳所有文件清單，找出 `category` 包含 `題目_md` 的所有文件。

範例回傳：
```json
{
  "sources": [
    {"name": "社群媒體系統.md", "filename": "...", "category": "新人面試題庫/題目_md"},
    {"name": "金融商品喜好紀錄系統.md", "category": "新人面試題庫/題目_md"},
    ...
  ]
}
```

### 1b. 取得評分方式全文

```http
GET http://localhost:8765/doc_by_source?source_name=評分方式
```

回傳完整評分方式 markdown 內容。

### 1c. 取得每道題目全文

對每個題目文件，使用其 `id` 取得完整內容：
```http
GET http://localhost:8765/doc/{file_id}
```

或批次用：
```http
GET http://localhost:8765/doc_by_source?source_name=社群媒體
```

---

## Step 2 — 讀取 GitHub Repo 結構

使用者提供的 repo URL 格式：
- `https://github.com/{owner}/{repo}` （使用預設分支）
- `https://github.com/{owner}/{repo}/tree/{branch}`

### 取得檔案樹

```http
GET https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1
```

範例：
```http
GET https://api.github.com/repos/stu24801/my-project/git/trees/main?recursive=1
```

注意事項：
- 若 repo 為 private，需要使用者提供 GitHub Token（加 `Authorization: Bearer {token}` header）
- 找出所有 `.java`、`.kt`、`.py`、`.ts`、`*.md`、`*.sql` 等程式碼檔案
- 重點關注：`src/`, `main/`, `controller/`, `service/`, `entity/`, `repository/` 等目錄

---

## Step 3 — 讀取程式碼內容

```http
GET https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}
```

範例：
```http
GET https://raw.githubusercontent.com/stu24801/my-project/main/src/main/java/com/example/UserController.java
```

建議優先讀取：
1. `README.md` — 了解整體架構
2. Controller / Router 層
3. Service 層
4. Entity / Model 定義
5. Repository / DAO 層
6. SQL 腳本 (`*.sql`, `schema.sql`, `init.sql`)
7. `pom.xml` / `build.gradle` / `package.json` — 確認技術選型

---

## Step 4 — 對照題目

將 repo 的功能模組與題目清單對照：

| 題目 | 關鍵識別特徵 |
|------|------------|
| 社群媒體系統 | User, Post, Like, Follow, Feed |
| 金融商品喜好紀錄 | Fund, Product, Portfolio, Transaction |
| 員工座位系統 | Employee, Seat, Office, Booking |
| 電商購物中心 | Product, Order, Cart, Payment |
| 圖書借閱系統 | Book, Borrow, Return, Member |
| 線上投票系統 | Vote, Poll, Option, Result |

若無法從 README 判斷，分析 Entity class 名稱來確認。

---

## Step 5 — 評分

根據「評分方式.md」的評分項目逐一評分：

評分輸出格式（每個項目）：
```
✅ [項目名稱] — 已完成
   說明：在 XxxController.java 第 42 行實作了 POST /api/users，符合要求。

⚠️ [項目名稱] — 部分完成（扣 X 分）
   說明：UserService.java 缺少密碼加鹽處理，直接存明碼。

❌ [項目名稱] — 未完成
   說明：未找到 JWT 相關實作。
```

---

## Step 6 — 輸出評分報告

報告結構：

```markdown
# 評分報告

## 基本資訊
- GitHub Repo: {url}
- 分支: {branch}
- 對應題目: {題目名稱}
- 評分時間: {timestamp}

## 題目判定
根據 [來源：題目名稱] 的規格...（說明為何判定為此題）

## 評分結果

### {評分大項一}（滿分 X 分）
| 評分細項 | 狀態 | 說明 |
|---------|------|------|
| 項目A | ✅ | ... |
| 項目B | ⚠️ | ... |

### {評分大項二}（滿分 X 分）
...

## 總分
| 大項 | 得分 | 滿分 |
|------|------|------|
| ... | ... | ... |
| **總計** | **XX** | **100** |

## 改進建議
1. ...
2. ...

---
*評分依據：RAG知識庫「評分方式.md」（來源：新人面試題庫/評分方式_md）*
*題目來源：RAG知識庫「{題目名稱}」（來源：新人面試題庫/題目_md）*
```

---

## 重要注意事項

1. **公開 repo**：直接用 raw.githubusercontent.com 讀取，不需 token
2. **私有 repo**：請求使用者提供 `ghp_xxxx` 格式的 GitHub Personal Access Token
3. **大型 repo**：優先讀取關鍵目錄，不需要讀取所有檔案
4. **多題情況**：若 repo 包含多道題目，分別評分或請使用者確認要評哪一題
5. **技術限制**：評分以程式碼邏輯為主，不實際執行程式
