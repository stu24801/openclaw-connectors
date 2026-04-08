import os
import re

FILE = "/home/millalex921/.openclaw/workspace-engineer/rag-knowledge-base/server/main.py"
content = open(FILE, "r").read()

# 1. Add /share endpoint
SHARE_API = """
@app.post("/articles/{article_id}/share")
def share_article(article_id: str, request: Request, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        raise HTTPException(401, "Unauthorized")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404, "Article not found")
        
    data = {"password": ""}
    # try to read json
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        body = loop.run_until_complete(request.json())
        data["password"] = body.get("password", "")
    except Exception:
        pass

    if not am.get("share_token"):
        am["share_token"] = str(uuid.uuid4())
    
    am["share_password"] = data["password"]
    _save_articlemeta()
    
    url = f"{request.base_url.scheme}://{request.base_url.netloc}/shared/{am['share_token']}"
    return JSONResponse({"url": url, "token": am["share_token"]})

@app.get("/shared/{share_token}", response_class=HTMLResponse)
def shared_article_get(share_token: str, request: Request):
    am = next((a for a in _articlemeta if a.get("share_token") == share_token), None)
    if not am:
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
        
    return HTMLResponse(_base_html(f\"\"\"
<div style="max-width:400px;margin:100px auto;text-align:center;padding:24px;background:#1e2235;border-radius:12px;border:1px solid #2d3154;">
    <h2 style="color:#a78bfa;margin-bottom:16px;">《{am['title']}》</h2>
    <p style="color:#94a3b8;margin-bottom:24px;">這是一篇加密分享的文章，請輸入密碼以繼續閱讀。</p>
    <form method="post" action="/shared/{share_token}">
        <input type="password" name="password" placeholder="請輸入密碼" required
               style="width:100%;padding:10px;margin-bottom:16px;background:#0f1117;border:1px solid #3b1f6e;color:#e2e8f0;border-radius:6px;">
        <button type="submit" class="btn btn-primary" style="width:100%;padding:10px;">解鎖閱讀</button>
    </form>
</div>
\"\"\", "文章密碼保護"))

@app.post("/shared/{share_token}", response_class=HTMLResponse)
async def shared_article_post(share_token: str, request: Request):
    am = next((a for a in _articlemeta if a.get("share_token") == share_token), None)
    if not am:
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
        
    form = await request.form()
    password = form.get("password", "")
    
    if am.get("share_password") and password != am.get("share_password"):
        return HTMLResponse(_base_html(f\"\"\"
<div style="max-width:400px;margin:100px auto;text-align:center;padding:24px;background:#1e2235;border-radius:12px;border:1px solid #2d3154;">
    <h2 style="color:#f87171;margin-bottom:16px;">密碼錯誤</h2>
    <p style="color:#94a3b8;margin-bottom:24px;">您輸入的密碼不正確，請重新嘗試。</p>
    <a href="/shared/{share_token}" class="btn btn-primary">返回重試</a>
</div>
\"\"\", "密碼錯誤"))

    # Render article
    path = Path(am["path"])
    content = path.read_text(encoding="utf-8") if path.exists() else "（檔案遺失）"
    content_escaped = json.dumps(content)
    
    body = f\"\"\"
<style>
  .container{{max-width:800px!important;padding:24px!important;margin:0 auto!important}}
  #rendered{{max-width:100%;overflow-x:hidden;word-break:break-word;overflow-wrap:break-word}}
  #rendered h1,#rendered h2,#rendered h3{{color:#a78bfa;margin:1.2em 0 .5em;word-break:break-word}}
  #rendered p{{margin-bottom:.9em}}
  #rendered code{{background:#0f1117;padding:2px 6px;border-radius:4px;font-size:.85em;color:#86efac;word-break:break-all}}
  #rendered pre{{background:#0f1117;border:1px solid #2d3154;border-radius:8px;padding:14px;overflow-x:auto;margin-bottom:1em;max-width:100%}}
</style>
<div style="margin-bottom:24px;border-bottom:1px solid #2d3154;padding-bottom:16px;">
    <h1 style="color:#e2e8f0;margin:0 0 8px 0;">{am['title']}</h1>
    <div style="color:#94a3b8;font-size:0.9rem;">
        作者: {am.get('author','—')} | 發布時間: {am.get('uploaded_at','')}
    </div>
</div>
<div id="rendered"></div>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
  document.getElementById('rendered').innerHTML = marked.parse({{content_escaped}});
</script>
\"\"\"
    return HTMLResponse(_base_html(body, am['title']))
"""

# Inject before @app.get("/articles", ...)
if "@app.post(\"/articles/{article_id}/share\")" not in content:
    content = content.replace(
        '@app.get("/articles", response_class=HTMLResponse)',
        SHARE_API + '\n@app.get("/articles", response_class=HTMLResponse)'
    )

# 2. Add JS function to list page
JS_INJECT = """
<script>
async function shareArticle(id) {
    const pwd = prompt("請設定此文章的分享密碼 (留空代表無密碼):");
    if (pwd === null) return; // cancelled
    
    try {
        const res = await fetch(`/articles/${id}/share`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({password: pwd})
        });
        const data = await res.json();
        if (data.url) {
            prompt("文章已成功建立分享連結！請複製以下網址交給其他人:", data.url);
        } else {
            alert("分享失敗");
        }
    } catch (e) {
        alert("錯誤: " + e);
    }
}
</script>
"""

# Inject JS before </body> or inside _base_html if we can, but let's just inject into articles_list return body.
if "shareArticle(" not in content:
    # Desktop row
    content = content.replace(
        '<a href="/articles/{a[\'id\']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>',
        '<button onclick="shareArticle(\'{a[\\\'id\\\']}\')" class="btn btn-sm btn-ghost">🔗 分享</button>\n    <a href="/articles/{a[\'id\']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>'
    )
    # Mobile row
    content = content.replace(
        '<a href="/articles/{a[\'id\']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>',
        '<button onclick="shareArticle(\'{a[\\\'id\\\']}\')" class="btn btn-sm btn-ghost">🔗 分享</button>\n    <a href="/articles/{a[\'id\']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>'
    )
    # Append JS to articles list HTML
    content = content.replace(
        'return HTMLResponse(_base_html(body, "文章庫 — RAG KB"))',
        'body += """' + JS_INJECT + '"""\n    return HTMLResponse(_base_html(body, "文章庫 — RAG KB"))'
    )

with open(FILE, "w") as f:
    f.write(content)
print("Patch applied successfully.")
