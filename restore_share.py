import os
import json
import uuid

FILES = ["/opt/rag-kb/main.py", "/opt/rag-kb/rag-knowledge-base/server/main.py"]

SHARE_HTML_FUNC = """
def _share_html(body: str, title: str) -> str:
    return f\"\"\"<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}}
  .btn{{display:inline-block;padding:8px 16px;border-radius:6px;font-size:0.9rem;cursor:pointer;text-align:center;border:none;color:#fff;text-decoration:none;transition:background .2s}}
  .btn-primary{{background:#4f46e5;color:#fff}}
  .btn-primary:hover{{background:#4338ca}}
  #rendered{{line-height:1.8;color:#cbd5e1;font-size:.95rem;word-break:break-word;overflow-wrap:break-word;padding:12px 0;}}
  #rendered h1,#rendered h2,#rendered h3{{color:#a78bfa;margin:1.2em 0 .5em;word-break:break-word}}
  #rendered p{{margin-bottom:.9em}}
  #rendered code{{background:#0f1117;padding:2px 6px;border-radius:4px;font-size:.85em;color:#86efac;word-break:break-all}}
  #rendered pre{{background:#0f1117;border:1px solid #2d3154;border-radius:8px;padding:14px;overflow-x:auto;margin-bottom:1em;max-width:100%}}
</style>
</head>
<body>
<div style="padding: 24px; max-width: 800px; margin: 0 auto;">
{body}
</div>
</body>
</html>\"\"\"
"""

SHARE_ROUTES = """
# ── 分享功能 ───────────────────────────────────────────────────────────────
@app.post("/articles/{article_id}/share")
async def share_article_api(article_id: str, request: Request, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        raise HTTPException(401, "Unauthorized")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404, "Article not found")
    try:
        body = await request.json()
        pwd = body.get("password", "").strip()
    except:
        pwd = ""
    
    am["share_pwd"] = pwd
    am["share_enabled"] = True
    _save_articlemeta()
    return JSONResponse({"status": "ok", "url": f"/share/{article_id}"})

@app.get("/share/{article_id}", response_class=HTMLResponse)
def share_view_get(article_id: str, request: Request, v: Optional[str] = None):
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am or not am.get("share_enabled"):
        return HTMLResponse(_share_html("<div style='text-align:center;padding:50px;'>此文章未開放分享。</div>", "文章未分享"))
    
    auth_cookie = request.cookies.get(f"share_auth_{article_id}")
    if am.get("share_pwd") and auth_cookie != am.get("share_pwd"):
        body = f'''
        <div style="max-width:400px;margin:100px auto;padding:32px;background:#1a1d2e;border-radius:12px;border:1px solid #2d3154;text-align:center;">
          <h2 style="color:#e2e8f0;margin-top:0">🔒 密碼保護文章</h2>
          <p style="color:#94a3b8;margin-bottom:24px;">請輸入密碼以閱讀 《{am['title']}》</p>
          <form method="POST" action="/share/{article_id}">
            <input type="password" name="pwd" placeholder="請輸入密碼" required style="width:100%;padding:12px;border-radius:6px;border:1px solid #2d3154;background:#0f1117;color:#e2e8f0;margin-bottom:20px;box-sizing:border-box;">
            <button type="submit" class="btn btn-primary" style="width:100%;">解鎖文章</button>
          </form>
        </div>
        '''
        return HTMLResponse(_share_html(body, "解鎖文章"))

    path = Path(am["path"])
    content_text = path.read_text(encoding="utf-8") if path.exists() else "（檔案遺失）"
    content_escaped = json.dumps(content_text)
    
    body = f'''
    <div style="margin-bottom:24px;border-bottom:1px solid #2d3154;padding-bottom:16px;">
        <h1 style="color:#e2e8f0;margin:0 0 8px 0;">{am['title']}</h1>
        <div style="color:#94a3b8;font-size:0.9rem;">作者: {am.get('author','—')} | 時間: {am.get('uploaded_at','')}</div>
    </div>
    <div id="rendered"></div>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script>
        document.getElementById('rendered').innerHTML = marked.parse({content_escaped});
    </script>
    '''
    return HTMLResponse(_share_html(body, am['title']))

@app.post("/share/{article_id}", response_class=HTMLResponse)
def share_view_post(article_id: str, pwd: str = Form(...)):
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am or not am.get("share_enabled"):
        return HTMLResponse(_share_html("文章未分享", "Error"))
    
    if pwd != am.get("share_pwd"):
        return HTMLResponse(_share_html("<div style='text-align:center;padding:50px;'>密碼錯誤。<br><a href='/share/"+article_id+"' class='btn btn-primary'>重試</a></div>", "密碼錯誤"))
    
    resp = RedirectResponse(f"/share/{article_id}", status_code=303)
    resp.set_cookie(key=f"share_auth_{article_id}", value=pwd, max_age=86400*30)
    return resp
"""

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

for FILE in FILES:
    if not os.path.exists(FILE): continue
    content = open(FILE, "r").read()
    if "def _share_html" not in content:
        content = content.replace("def _base_html", SHARE_HTML_FUNC + "\ndef _base_html")
    if '@app.post("/articles/{article_id}/share")' not in content:
        content = content.replace("def article_view", SHARE_ROUTES + "\ndef article_view")
    if "function shareArticle" not in content:
        content = content.replace('return HTMLResponse(_base_html(body, "文章庫 — RAG KB"))', 
                                  'body += """' + JS_CODE + '"""\n    return HTMLResponse(_base_html(body, "文章庫 — RAG KB"))')
    if 'onclick="shareArticle' not in content:
        content = content.replace('<a href="/articles/{a[\'id\']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>',
                                  '<button onclick="shareArticle(\'{a[\'id\']}\')" class="btn btn-sm btn-ghost">🔗 分享</button> <a href="/articles/{a[\'id\']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>')
    with open(FILE, "w") as f:
        f.write(content)
    print(f"Patched {FILE}")
