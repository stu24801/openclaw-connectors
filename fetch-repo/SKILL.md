---
name: fetch-repo
description: >
  從 GitHub repo 取得所有原始碼並整理成結構化摘要。
  觸發詞：「看看這個 repo」、「幫我讀取這個 GitHub」、「這個專案有什麼」、
  「fetch repo」、「讀取原始碼」、「分析這個 GitHub 連結」。
  需要：GitHub repo URL（公開 repo 不需 token）。
---

# fetch-repo Skill — 讀取 GitHub 原始碼

## 目的

不進行評分，單純讀取並整理 GitHub repo 的原始碼結構與內容，
以便後續對話中分析、提問、或傳入其他 Skill（如 github-grader）。

---

## 執行步驟

### Step 1 — 解析 GitHub URL

從使用者輸入的 URL 解析：

| URL 格式 | 解析結果 |
|---------|---------|
| `https://github.com/owner/repo` | branch = `main` |
| `https://github.com/owner/repo/tree/develop` | branch = `develop` |
| `git@github.com:owner/repo.git` | 轉換為 HTTPS 格式 |

若無法解析，請使用者確認格式。

### Step 2 — 取得檔案樹

```http
GET https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1
Accept: application/vnd.github.v3+json
User-Agent: openclaw-fetch-repo
Authorization: Bearer {token}   # 私有 repo 或需提升 rate limit 時使用
```

從回傳的 `tree[]` 中過濾 `type == "blob"` 的檔案。

若回傳 `truncated: true`，表示 repo 過大（>100,000 個物件），只讀關鍵目錄。

### Step 3 — 篩選要讀的檔案

**允許的副檔名：**
```
.java .kt .scala  → JVM 語言
.py               → Python
.ts .js .mjs      → JavaScript/TypeScript
.go               → Go
.cs               → C#
.rs               → Rust
.sql              → 資料庫腳本
.md               → 說明文件
.xml .gradle .toml .yaml .yml  → 設定檔
```

**跳過的路徑（含以下字串）：**
```
/test/ /tests/ /__tests__/   → 測試程式碼（可選讀）
/target/ /build/ /dist/      → 編譯產物
/node_modules/ /.git/        → 依賴與版本控制
/__pycache__/ /.gradle/      → 快取
```

**優先讀取順序：**
1. `README.md` / `readme.md`
2. `*Controller*` `*Router*` `*Handler*`
3. `*Service*` `*UseCase*`
4. `*Entity*` `*Model*` `*Domain*`
5. `*Repository*` `*DAO*` `*Store*`
6. `*.sql` `schema.*` `migration*`
7. `pom.xml` `build.gradle` `package.json` `go.mod`
8. `application.yml` `application.properties`

**限制：**
- 最多讀 **30 個檔案**
- 每個檔案最多讀 **3000 字元**（超過截斷並加 `[...截斷]`）

### Step 4 — 讀取檔案內容

```http
GET https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}
Authorization: Bearer {token}   # 私有 repo
```

### Step 5 — 整理輸出

```markdown
## 📦 Repo 資訊
- **Repo**: {owner}/{repo}
- **分支**: {branch}
- **總檔案數**: {N} 個
- **已讀取**: {M} 個程式碼檔案

## 🛠 技術棧
（依 pom.xml / build.gradle / package.json 分析）
- 語言: Java 17
- 框架: Spring Boot 3.2
- 資料庫: MySQL (JPA/Hibernate)
- 認證: Spring Security + JWT

## 🏗 架構概覽
（依 README 或目錄結構分析）
- 分層: Controller → Service → Repository
- 主要 Package: com.example.xxx

## 🔑 主要 Entity / 資料模型
| Entity | 主要欄位 |
|--------|---------|
| User   | id, username, email, password |
| Post   | id, content, authorId, createdAt |

## 📁 已讀取的檔案
| 檔案路徑 | 說明 |
|---------|------|
| src/main/java/.../UserController.java | User CRUD API |
| ... | ... |

## 📄 各檔案內容
（逐一列出，含截斷標記）
```

---

## 注意事項

- **Rate Limit**：未授權的 GitHub API 每小時限 60 次請求；建議使用者提供 token（每小時 5000 次）
- **私有 repo**：需要有 `repo` scope 的 Personal Access Token (`ghp_xxxx`)
- **讀取失敗**：若單一檔案 404，跳過並記錄，繼續讀下一個
- **結果快取**：讀取結果保留在對話 context 中，後續「幫我評分」可直接使用，不需重新讀取

---

## 與其他 Skill 的整合

讀取完成後，context 中已有原始碼摘要，可直接：
- 說「幫我評分」→ 觸發 `github-grader`，使用已讀取的原始碼（不重新 fetch）
- 說「這個用什麼框架」→ 直接從 context 回答
- 說「有沒有實作分頁」→ 搜尋已讀取的內容回答
