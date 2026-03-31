import os
import re

FILE = "/opt/rag-kb/main.py"
with open(FILE, "r") as f:
    content = f.read()

# 1. 插入 JavaScript 到 article_view
JS_CODE = """
<script>
async function shareArticle(id) {
    const pwd = prompt("設定分享密碼 (留空則不加密):");
    if (pwd === null) return;
    const res = await fetch(`/articles/${id}/share`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({password: pwd})
    });
    const data = await res.json();
    if (data.url) {
        const fullUrl = window.location.origin + data.url;
        prompt("分享網址已產生！", fullUrl);
    } else {
        alert("分享失敗");
    }
}
</script>
"""

# 我們把 JS 放進 article_view 的 body 中
# 先找 article_view 的結尾 HTMLResponse(_base_html(body, am['title']))
if 'async function shareArticle(id)' not in content.split('@app.get("/articles/{article_id}", response_class=HTMLResponse)')[1]:
    content = content.replace("    return HTMLResponse(_base_html(body, f\\\"{am['title']} — 文章庫\\\"))",
                              "    body += \\\"\"\"" + JS_CODE + "\\\"\"\"\n    return HTMLResponse(_base_html(body, f\"{am['title']} — 文章庫\"))")

# 2. 插入按鈕到文章檢視區的頂部按鈕列
btn_html = """        <button onclick="shareArticle('{article_id}')" class="btn btn-sm btn-ghost">🔗 分享</button>
        <a href="/articles/{article_id}/download" """

if 'onclick="shareArticle(\'{article_id}\')"' not in content:
    content = content.replace('        <a href="/articles/{article_id}/download" ', btn_html)

with open(FILE, "w") as f:
    f.write(content)

print("Added share button and JS to article view.")
