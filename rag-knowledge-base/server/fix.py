import os

FILE = "/home/millalex921/.openclaw/workspace-engineer/rag-knowledge-base/server/main.py"
content = open(FILE, "r").read()

# 修正 2474 等行的雙重複按鈕和引號錯誤
import re
# 找到 f"""<tr>...</tr>"""
# 或者是直接找這段字串並替換
bad_pattern = re.compile(r"<button onclick=\"shareArticle\('\{a\[.*?\]\}'\)\" class=\"btn btn-sm btn-ghost\">🔗 分享</button>\s*<button onclick=\"shareArticle\('\{a\[.*?\]\}'\)\" class=\"btn btn-sm btn-ghost\">🔗 分享</button>")
content = bad_pattern.sub(r"<button onclick=\"shareArticle('{a['id']}')\" class=\"btn btn-sm btn-ghost\">🔗 分享</button>", content)

# 或者是更暴力的，找出整個 table row 跟 card 並替換
table_row = """            rows += f\"\"\"<tr>
  <td><a href="/articles/{a['id']}" style="color:#a78bfa;text-decoration:none">{a['title']}</a></td>
  <td style="color:#94a3b8">{a.get('author','—')}</td>
  <td style="color:#64748b;font-size:.8rem">{a.get('uploaded_at','')}</td>
  <td style="color:#64748b;font-size:.8rem">{size_kb} KB</td>
  <td style="white-space:nowrap">
    <button onclick="shareArticle('{a['id']}')" class="btn btn-sm btn-ghost">🔗 分享</button>
    <a href="/articles/{a['id']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>
    &nbsp;
    <form method="post" action="/articles/{a['id']}/delete" style="display:inline"
          onsubmit="return confirm('確定刪除？')">
      <button class="btn btn-sm btn-danger">🗑</button>
    </form>
  </td>
</tr>\"\"\""""

# 但這可能因為我們已經改爛了導致找不到，直接用行號區間重寫
lines = content.split('\n')
start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if "rows += f\"\"\"<tr>" in line:
        start_idx = i
    if "</tr>\"\"\"" in line and start_idx != -1:
        end_idx = i
        break

if start_idx != -1 and end_idx != -1:
    lines[start_idx:end_idx+1] = table_row.split('\n')

card_row = """            cards += f\"\"\"<div class="article-card">
  <a href="/articles/{a['id']}" class="article-card-title">{a['title']}</a>
  <div class="article-card-meta">✍️ {a.get('author','—')} &nbsp;·&nbsp; {a.get('uploaded_at','')} &nbsp;·&nbsp; {size_kb} KB</div>
  <div class="article-card-actions">
    <button onclick="shareArticle('{a['id']}')" class="btn btn-sm btn-ghost">🔗 分享</button>
    <a href="/articles/{a['id']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>
    <form method="post" action="/articles/{a['id']}/delete"
          onsubmit="return confirm('確定刪除？')">
      <button class="btn btn-sm btn-danger">🗑</button>
    </form>
  </div>
</div>\"\"\""""

start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if "cards += f\"\"\"<div class=\"article-card\">" in line:
        start_idx = i
    if "</div>\"\"\"" in line and start_idx != -1 and "article-card-actions" in lines[i-6]:
        # just a rough check
        end_idx = i
        break

if start_idx != -1 and end_idx != -1:
    lines[start_idx:end_idx+1] = card_row.split('\n')

with open(FILE, "w") as f:
    f.write('\n'.join(lines))
print("fixed f-string")
