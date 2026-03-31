import os

FILE = "/opt/rag-kb/main.py"
with open(FILE, "r") as f:
    content = f.read()

# 1. 注入 /share/{article_id}/download 路由
DOWNLOAD_ROUTE = """
@app.get("/share/{article_id}/download")
def share_download(article_id: str, request: Request):
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am or not am.get("share_enabled"):
        raise HTTPException(404, "Article not found or not shared")
    
    auth_cookie = request.cookies.get(f"share_auth_{article_id}")
    if am.get("share_pwd") and auth_cookie != am.get("share_pwd"):
        raise HTTPException(401, "Unauthorized")

    path = Path(am["path"])
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(str(path), filename=am["filename"], media_type="text/markdown")
"""

if '@app.get("/share/{article_id}/download")' not in content:
    content = content.replace('@app.post("/share/{article_id}")', DOWNLOAD_ROUTE + '\n@app.post("/share/{article_id}")')

# 2. 注入下載按鈕 UI
UI_REPLACE_FROM = """<div style="color:#94a3b8;font-size:0.9rem;">✍️ 作者: {am.get('author','—')} &nbsp;·&nbsp; {am.get('uploaded_at','')}</div>"""

UI_REPLACE_TO = """<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <div style="color:#94a3b8;font-size:0.9rem;">✍️ 作者: {am.get('author','—')} &nbsp;·&nbsp; {am.get('uploaded_at','')}</div>
                <a href="/share/{article_id}/download" class="btn btn-sm btn-ghost" style="text-decoration:none;font-size:0.8rem;padding:4px 10px;min-height:auto;">⬇ 下載 Markdown</a>
            </div>"""

if '⬇ 下載 Markdown' not in content:
    content = content.replace(UI_REPLACE_FROM, UI_REPLACE_TO)

with open(FILE, "w") as f:
    f.write(content)

print("Added download functionality to shared article.")
