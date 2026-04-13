"""
RAG Knowledge Base Server — QMD SQLite direct + embed_server (port 8766)
v2: 支援分層分類 (category) 管理與搜尋過濾
"""

import os
import uuid, json, hashlib, secrets, io, subprocess, shutil, re, sqlite3, struct, urllib.request
import threading, queue, time
from pathlib import Path
from typing import List, Optional, AsyncGenerator
from datetime import datetime, timezone, timedelta

TZ_TAIPEI = timezone(timedelta(hours=8))

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Cookie, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, StreamingResponse

# ── OpenClaw Gateway ─────────────────────────────────────────────────────────
OPENCLAW_GATEWAY_URL   = os.getenv("OPENCLAW_GATEWAY_URL",   "http://127.0.0.1:8080")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "3056ad885dd941a5795fb4ac8dcd10b677ac4d2c5d4f696f")
WRITER_SESSION_KEY     = os.getenv("WRITER_SESSION_KEY",     "agent:main:main")

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR      = Path(os.getenv("RAG_DATA_DIR", "./data"))
FILES_DIR     = DATA_DIR / "files"
FILEMETA_PATH = DATA_DIR / "filemeta.json"
QMD_BIN       = os.getenv("QMD_BIN", "qmd")
QMD_DB_PATH   = os.path.expanduser(os.getenv("QMD_DB", "~/.cache/qmd/index.sqlite"))
VEC0_SO       = os.path.expanduser(os.getenv("VEC0_SO",
    "~/.npm-global/lib/node_modules/@tobilu/qmd/node_modules/sqlite-vec-linux-x64/vec0.so"))
EMBED_URL     = os.getenv("EMBED_URL", "http://127.0.0.1:8766/embed")
QMD_COLL      = "rag-kb"
TOP_K         = int(os.getenv("RAG_TOP_K", "5"))
PASSWORD        = os.getenv("RAG_PASSWORD", "changeme")
WRITER_PASSWORD = os.getenv("WRITER_PASSWORD", "writer123")

# ── ENV file path (for password persistence) ──────────────────────────────────
_ENV_FILE = Path(os.getenv("RAG_ENV_FILE", "/app/.env"))

# ── Articles (writer uploads) ─────────────────────────────────────────────────
ARTICLES_DIR      = DATA_DIR / "articles"
ARTICLEMETA_PATH  = DATA_DIR / "articlemeta.json"

# ── Vault (general file storage) ──────────────────────────────────────────────
VAULT_DIR      = DATA_DIR / "vault"
VAULTMETA_PATH = DATA_DIR / "vaultmeta.json"
_vaultmeta: List[dict] = []

def _load_vaultmeta():
    global _vaultmeta
    _vaultmeta = json.loads(VAULTMETA_PATH.read_text()) if VAULTMETA_PATH.exists() else []

def _save_vaultmeta():
    VAULTMETA_PATH.write_text(json.dumps(_vaultmeta, ensure_ascii=False, indent=2))

# ── In-memory session store ───────────────────────────────────────────────────
_sessions: set = set()

# ── Article messages (feedback) ───────────────────────────────────────────────
ARTICLE_MESSAGES_PATH = DATA_DIR / "article_messages.json"
_article_messages: dict = {}  # article_id → List[{role, from, content, timestamp}]

def _load_article_messages():
    global _article_messages
    _article_messages = json.loads(ARTICLE_MESSAGES_PATH.read_text()) if ARTICLE_MESSAGES_PATH.exists() else {}

def _save_article_messages():
    ARTICLE_MESSAGES_PATH.write_text(json.dumps(_article_messages, ensure_ascii=False, indent=2))

app = FastAPI(title="RAG Knowledge Base")

# ── Filemeta helpers ──────────────────────────────────────────────────────────
_filemeta: List[dict] = []

def _load_filemeta():
    global _filemeta
    _filemeta = json.loads(FILEMETA_PATH.read_text()) if FILEMETA_PATH.exists() else []

def _save_filemeta():
    FILEMETA_PATH.write_text(json.dumps(_filemeta, ensure_ascii=False, indent=2))

# ── Article meta helpers ──────────────────────────────────────────────────────
_articlemeta: List[dict] = []

def _load_articlemeta():
    global _articlemeta
    _articlemeta = json.loads(ARTICLEMETA_PATH.read_text()) if ARTICLEMETA_PATH.exists() else []

def _save_articlemeta():
    ARTICLEMETA_PATH.write_text(json.dumps(_articlemeta, ensure_ascii=False, indent=2))

def _all_categories() -> List[str]:
    """Return sorted unique category paths from filemeta."""
    cats = sorted({f.get("category", "未分類") for f in _filemeta})
    return cats

def _category_tree() -> dict:
    """Build nested dict from category paths for sidebar tree view."""
    tree = {}
    for cat in _all_categories():
        parts = cat.split("/")
        node = tree
        for p in parts:
            node = node.setdefault(p, {})
    return tree

# ── Embedding (via embed_server on port 8766) ─────────────────────────────────

def _qmd_update_embed():
    """Background task to update QMD embeddings after file upload."""
    import subprocess, os
    qmd_bin = os.getenv("QMD_BIN", "qmd")
    try:
        subprocess.run([qmd_bin, "embed", "--dir", DATA_DIR], timeout=120, capture_output=True)
    except Exception:
        pass

def _get_embedding(text: str) -> Optional[bytes]:
    try:
        req = urllib.request.Request(
            EMBED_URL,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        vec = data["embedding"]
        return struct.pack(f"{len(vec)}f", *vec)
    except Exception as e:
        print(f"[embed] error: {e}")
        return None

# ── SQLite vector search ──────────────────────────────────────────────────────

def _sqlite_vsearch(query: str, top_k: int = TOP_K, category: Optional[str] = None) -> List[dict]:
    """Cosine search + optional category filter (prefix match)."""
    vec_bytes = _get_embedding(query)
    if vec_bytes is None:
        return []

    # Build set of file paths that belong to the requested category (prefix)
    if category:
        allowed_files = {
            Path(f["path"]).name
            for f in _filemeta
            if f.get("category", "未分類").startswith(category)
        }
    else:
        allowed_files = None

    try:
        db = sqlite3.connect(QMD_DB_PATH)
        db.enable_load_extension(True)
        db.load_extension(VEC0_SO)
        db.enable_load_extension(False)

        rows = db.execute("""
            SELECT
                d.title,
                d.path,
                cv.hash,
                cv.pos,
                1 - vv.distance AS score
            FROM vectors_vec vv
            JOIN content_vectors cv ON cv.hash || '_' || cv.seq = vv.hash_seq
            JOIN documents d ON d.hash = cv.hash
            WHERE d.collection = ?
              AND vv.embedding MATCH ?
              AND k = ?
            ORDER BY score DESC
        """, (QMD_COLL, vec_bytes, top_k * 5)).fetchall()
        db.close()

        # Build file_id → metadata map for category lookup
        path_to_meta = {Path(f["path"]).name: f for f in _filemeta}

        results = []
        seen_hashes = set()
        for title, fpath, chash, pos, score in rows:
            if chash in seen_hashes:
                continue
            fname = Path(fpath).name
            if allowed_files is not None and fname not in allowed_files:
                continue
            seen_hashes.add(chash)

            db2 = sqlite3.connect(QMD_DB_PATH)
            content_row = db2.execute("SELECT doc FROM content WHERE hash=?", (chash,)).fetchone()
            db2.close()
            body = ""
            if content_row:
                full = content_row[0]
                body = full[pos:pos+400] if pos < len(full) else full[:400]

            fm = path_to_meta.get(fname, {})
            results.append({
                "score": round(score, 4),
                "title": fm.get("source_name", title),
                "category": fm.get("category", "未分類"),
                "file": fpath,
                "body": body,
            })
            if len(results) >= top_k:
                break
        return results
    except Exception as e:
        print(f"[sqlite_vsearch] error: {e}")
        return []

# ── QMD update+embed (background) ────────────────────────────────────────────

def _ask_writer_agent(article_id: str, owner_message: str):
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am: return

    path = Path(am["path"])
    content = path.read_text(encoding="utf-8") if path.exists() else ""

    # get last few messages for context
    msgs = _article_messages.get(article_id, [])
    history_text = "\\n".join([f"[{m['role']}] {m.get('content', '')}" for m in msgs[-5:]])

    prompt = (
        f"🦐 景揚在文章《{am['title']}》的對話通道傳來訊息：\n\n"
        f"「{owner_message}」\n\n"
        f"📄 文章 ID：{article_id}\n"
        f"🔗 對話介面：https://rag.alex-stu24801.com/articles/{article_id}\n\n"
        f"請你立即回覆景揚。步驟：\n"
        f"1. 透過 POST https://rag.alex-stu24801.com/articles/{article_id}/messages 回覆\n"
        f"   （X-Writer-Token header，使用自然對話口吻，簡短即可）\n"
        f"2. 若需要修改文章內容，再透過 POST https://rag.alex-stu24801.com/articles/{article_id}/revise 提交修改版\n"
        f"⚠️ 回覆訊息時，『絕對不要』貼出整篇文章或任何文章片段，只需用簡短自然的對話告訴景揚你的想法或已完成的修改。"
    )

    payload = {
        "tool": "sessions_send",
        "args": {
            "sessionKey": WRITER_SESSION_KEY,
            "message": prompt,
            "timeoutSeconds": 0
        }
    }
    try:
        req = urllib.request.Request(
            f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            pass  # 只需發送通知，寫手蝦會透過 API 自行回覆與修改文章

    except Exception as e:
        print(f"[_ask_writer_agent] error: {e}")
        _article_messages[article_id].append({
            "role": "writer",
            "from": "系統",
            "content": f"⚠️ 連線寫手蝦失敗：{e}",
            "timestamp": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
        })
        _save_article_messages()

def _notify_owner(
article_title: str, article_id: str, writer_name: str, reply_content: str) -> bool:
    """Notify 景揚 when 寫手蝦 replies to feedback."""
    article_url = f"https://rag.alex-stu24801.com/articles/{article_id}"
    payload = {
        "tool": "sessions_send",
        "args": {
            "sessionKey": "agent:main:main",
            "message": (
                f"✍️ **寫手蝦（{writer_name}）回覆了你的回饋**\n\n"
                f"📄 文章：**{article_title}**\n"
                f"💬 回覆：{reply_content}\n\n"
                f"👉 查看文章：{article_url}"
            ),
            "timeoutSeconds": 0
        }
    }
    try:
        req = urllib.request.Request(
            f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        return result.get("ok", False)
    except Exception as e:
        print(f"[notify_owner] error: {e}")
        return False



    try:
        subprocess.run([QMD_BIN, "update"], capture_output=True, timeout=120)
        subprocess.run([QMD_BIN, "embed"], capture_output=True, timeout=600)
    except Exception as e:
        print(f"[qmd embed] warning: {e}")

def _auth(token: Optional[str]) -> bool:
    return token is not None and token in _sessions

@app.on_event("startup")
def startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    _load_filemeta()
    _load_articlemeta()
    _load_article_messages()
    _load_vaultmeta()

# ═══════════════════════════════════════════════════════════════════════════════
#  HTML helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _share_html(body: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}}
  .btn{{display:inline-block;padding:8px 16px;border-radius:6px;font-size:0.9rem;cursor:pointer;text-align:center;border:none;color:#fff;text-decoration:none}}
  .btn-primary{{background:#4f46e5;color:#fff}}
  .btn-primary:hover{{background:#4338ca}}
</style>
</head>
<body>
<div style="padding: 24px;">
{body}
</div>
</body>
</html>"""

def _base_html(body: str, title="RAG Knowledge Base", sidebar_cats: List[str] = None) -> str:
    # Build sidebar category links
    sidebar_html = ""
    if sidebar_cats is not None:
        links = '<a href="/dashboard" style="color:#94a3b8;text-decoration:none;display:block;padding:6px 8px;border-radius:6px;font-size:.82rem;min-height:44px;display:flex;align-items:center">📋 全部文件</a>'
        for cat in sidebar_cats:
            depth = cat.count("/")
            indent = depth * 14
            label = cat.split("/")[-1]
            links += f'<a href="/dashboard?cat={cat}" style="color:#94a3b8;text-decoration:none;display:flex;align-items:center;padding:5px 8px 5px {8+indent}px;border-radius:6px;font-size:.82rem;min-height:40px">{"  " * depth}📁 {label}</a>'
        sidebar_html = f"""
<div class="sidebar-panel">
  <div style="background:#1a1d2e;border:1px solid #2d3154;border-radius:10px;padding:14px">
    <div style="color:#64748b;font-size:.75rem;font-weight:600;margin-bottom:10px;letter-spacing:.05em">分類</div>
    {links}
  </div>
</div>"""
    else:
        sidebar_html = ""

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}}
  .topbar{{background:#1a1d2e;border-bottom:1px solid #2d3154;padding:12px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
  .topbar h1{{font-size:1.1rem;font-weight:600;color:#a78bfa;flex-shrink:0}}
  .topbar-nav{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;flex:1}}
  .topbar a{{color:#94a3b8;font-size:.85rem;text-decoration:none;min-height:44px;display:inline-flex;align-items:center;padding:0 4px}}
  .topbar a:hover{{color:#a78bfa}}
  .topbar a.active{{color:#a78bfa;border-bottom:2px solid #a78bfa;padding-bottom:2px}}
  .topbar-logout{{margin-left:auto}}
  .hamburger{{display:none;background:none;border:none;color:#94a3b8;font-size:1.4rem;cursor:pointer;padding:4px 8px;min-height:44px;min-width:44px;align-items:center;justify-content:center}}
  .sidebar-toggle-btn{{display:none;width:100%;background:#2d3154;border:none;color:#94a3b8;padding:10px;border-radius:8px;cursor:pointer;font-size:.85rem;margin-bottom:10px;min-height:44px}}
  .container{{max-width:1100px;margin:32px auto;padding:0 16px}}
  .card{{background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;padding:24px;margin-bottom:24px}}
  .card h2{{font-size:1rem;font-weight:600;color:#a78bfa;margin-bottom:18px;display:flex;align-items:center;gap:8px}}
  label{{font-size:.9rem;color:#94a3b8;display:block;margin-bottom:6px}}
  input[type=text],input[type=password],input[type=file],select,textarea{{
    width:100%;padding:12px 14px;background:#0f1117;border:1px solid #2d3154;
    border-radius:8px;color:#e2e8f0;font-size:16px;outline:none;transition:.2s
  }}
  select option{{background:#1a1d2e}}
  input:focus,select:focus,textarea:focus{{border-color:#a78bfa}}
  .btn{{display:inline-flex;align-items:center;justify-content:center;padding:12px 22px;background:#7c3aed;color:#fff;
    border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:500;transition:.2s;min-height:44px;min-width:44px}}
  .btn:hover{{background:#6d28d9}}
  .btn-sm{{padding:8px 14px;font-size:.82rem;border-radius:6px;min-height:44px}}
  .btn-danger{{background:#dc2626}}.btn-danger:hover{{background:#b91c1c}}
  .btn-ghost{{background:transparent;border:1px solid #374151;color:#94a3b8}}
  .btn-ghost:hover{{background:#1f2937;color:#e2e8f0}}
  .alert{{padding:12px 16px;border-radius:8px;font-size:.85rem;margin-bottom:16px}}
  .alert-error{{background:#450a0a;border:1px solid #7f1d1d;color:#fca5a5}}
  .alert-success{{background:#052e16;border:1px solid #14532d;color:#86efac}}
  .table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem;min-width:480px}}
  th{{text-align:left;padding:10px 12px;background:#0f1117;color:#64748b;font-weight:500;border-bottom:1px solid #2d3154}}
  td{{padding:10px 12px;border-bottom:1px solid #1e2235;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#1e2235}}
  .badge{{display:inline-block;padding:3px 8px;border-radius:99px;font-size:.75rem;font-weight:500}}
  .badge-purple{{background:#3b0764;color:#c4b5fd}}
  .badge-blue{{background:#1e3a5f;color:#93c5fd}}
  .badge-green{{background:#052e16;color:#86efac}}
  .empty{{color:#4b5563;text-align:center;padding:32px;font-size:.9rem}}
  .form-row{{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}}
  .form-row>*{{flex:1;min-width:140px}}
  .form-row .btn{{flex:0 0 auto}}
  .stat{{display:inline-flex;align-items:center;gap:6px;background:#0f1117;
    border:1px solid #2d3154;border-radius:8px;padding:8px 16px;font-size:.85rem}}
  .stat-val{{font-size:1.2rem;font-weight:700;color:#a78bfa}}
  .stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}}
  .cat-group-header{{background:#13162a;padding:8px 12px;font-size:.78rem;
    color:#a78bfa;font-weight:600;letter-spacing:.04em;border-bottom:1px solid #2d3154}}
  @keyframes spin{{ to{{transform:rotate(360deg)}} }}
  .spinner{{width:40px;height:40px;margin:0 auto;border:3px solid #2d3154;
    border-top-color:#a78bfa;border-radius:50%;animation:spin 0.8s linear infinite}}
  /* Sidebar panel */
  .sidebar-panel{{width:200px;flex-shrink:0}}
  /* Upload grid */
  .upload-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}}
  /* Search grid */
  .search-grid{{display:grid;grid-template-columns:1fr auto auto;gap:12px;margin-bottom:20px;align-items:end}}
  /* Grade grid */
  .grade-grid{{display:grid;grid-template-columns:1fr auto;gap:12px;margin-bottom:14px;align-items:end}}
  /* ── Mobile ── */
  @media (max-width:640px){{
    .topbar{{padding:10px 14px;gap:8px}}
    .topbar h1{{font-size:1rem}}
    .topbar-nav a{{font-size:.8rem;padding:0 2px}}
    .container{{padding:0 12px;margin:16px auto}}
    .card{{padding:16px;border-radius:10px}}
    /* Sidebar becomes collapsible */
    .sidebar-panel{{width:100% !important;margin-bottom:12px}}
    .sidebar-panel>div{{border-radius:8px}}
    /* Layout becomes vertical */
    .layout-flex{{flex-direction:column !important}}
    /* Content area no min-width */
    .content-wrap{{min-width:0 !important}}
    /* Upload form → single column */
    .upload-grid{{grid-template-columns:1fr !important}}
    /* Search inputs → stacked */
    .search-grid{{grid-template-columns:1fr !important}}
    .search-grid select{{width:100% !important}}
    /* Grade inputs → stacked */
    .grade-grid{{grid-template-columns:1fr !important}}
    .grade-grid input{{width:100% !important}}
    /* Tables: horizontal scroll */
    table{{min-width:500px}}
    .table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -4px}}
    /* Stats wrap */
    .stats{{gap:8px}}
    .stat{{font-size:.8rem;padding:6px 10px}}
    /* Buttons in action cells */
    td .btn-sm{{min-height:40px;min-width:40px}}
    /* Article view */
    #rendered{{font-size:.9rem;line-height:1.7}}
    /* Chat input */
    #chat-input{{font-size:16px}}
    /* Prompt box */
    #prompt-box{{font-size:.75rem}}
    /* Progress log */
    #progress-log{{font-size:.78rem}}
    /* Form rows */
    .form-row{{flex-direction:column}}
    .form-row>*{{min-width:unset}}
    /* Topbar logout push */
    .topbar-logout{{margin-left:0}}
  }}
</style>
</head>
<body>
<div class="topbar">
  <h1>🦐 RAG Knowledge Base</h1>
  <div class="topbar-nav">
    <a href="/dashboard">Dashboard</a>
    <a href="/search_ui">搜尋</a>
    <a href="/grade_ui">📝 評分</a>
    <a href="/articles">📰 文章庫</a>
    <a href="/vault">📦 檔案庫</a>
    <a href="/sys_status">🖥️ 系統狀態</a>
    <a href="/logout" class="topbar-logout">登出</a>
  </div>
</div>
<div class="container">
  <div class="layout-flex" style="display:flex;gap:24px;align-items:flex-start">
  {sidebar_html}
  <div class="content-wrap" style="flex:1;min-width:0">
  {body}
  </div>
  </div>
</div>
<script>
// Wrap all tables in scroll containers
document.querySelectorAll('table').forEach(function(t){{
  if(t.parentElement && !t.parentElement.classList.contains('table-wrap')){{
    var w=document.createElement('div');
    w.className='table-wrap';
    t.parentNode.insertBefore(w,t);
    w.appendChild(t);
  }}
}});
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════════════════
#  Auth
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def root(rag_token: Optional[str] = Cookie(None)):
    return RedirectResponse("/dashboard" if _auth(rag_token) else "/login")

@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    err = '<div class="alert alert-error">密碼錯誤，請再試一次。</div>' if error else ""
    body = f"""
<div style="max-width:400px;margin:80px auto">
<div class="card">
  <h2>🔐 登入</h2>{err}
  <form method="post" action="/login">
    <label>存取密碼</label>
    <input type="password" name="password" autofocus style="margin-bottom:16px">
    <button class="btn" type="submit" style="width:100%">確認登入</button>
  </form>
</div></div>"""
    return HTMLResponse(_base_html(body, "登入 — RAG KB"))

@app.post("/login")
def login(response: Response, password: str = Form(...)):
    if not secrets.compare_digest(password, PASSWORD):
        return RedirectResponse("/login?error=1", status_code=303)
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("rag_token", token, httponly=True, samesite="lax", max_age=86400*7)
    return resp

@app.get("/logout")
def logout(rag_token: Optional[str] = Cookie(None)):
    _sessions.discard(rag_token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("rag_token")
    return resp

# ═══════════════════════════════════════════════════════════════════════════════
#  Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(rag_token: Optional[str] = Cookie(None), msg: str = "", cat: str = ""):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    cats = _all_categories()
    ok_html = f'<div class="alert alert-success">✅ {msg}</div>' if msg else ""

    # Filter files by category if requested
    if cat:
        display_files = [f for f in _filemeta if f.get("category", "未分類").startswith(cat)]
        cat_title = f' — 📁 {cat}'
    else:
        display_files = _filemeta
        cat_title = ""

    doc_count = len(display_files)

    # Build existing categories for datalist
    cat_options = "\n".join(f'<option value="{c}">' for c in cats)

    # Group files by category
    from collections import defaultdict
    grouped: dict = defaultdict(list)
    for f in reversed(display_files):
        grouped[f.get("category", "未分類")].append(f)

    if display_files:
        table_parts = []
        for group_cat in sorted(grouped.keys()):
            table_parts.append(f'<tr><td colspan="5" class="cat-group-header">📁 {group_cat}</td></tr>')
            for f in grouped[group_cat]:
                size_kb = f.get("size", 0) // 1024
                table_parts.append(f"""<tr>
  <td style="padding-left:20px">{f['filename']}</td>
  <td><span class="badge badge-purple">{f.get('source_name','—')}</span></td>
  <td><span class="badge badge-blue">{f.get('category','未分類')}</span></td>
  <td style="color:#64748b">{f.get('uploaded_at','')}&nbsp;&nbsp;{size_kb} KB</td>
  <td>
    <a href="/download/{f['id']}" class="btn btn-sm btn-ghost">⬇</a>
    &nbsp;
    <form method="post" action="/delete/{f['id']}" style="display:inline"
          onsubmit="return confirm('確定刪除？')">
      <button class="btn btn-sm btn-danger">🗑</button>
    </form>
  </td>
</tr>""")
        file_table = f"""<table>
<thead><tr><th>檔名</th><th>標籤</th><th>分類</th><th>時間 / 大小</th><th>操作</th></tr></thead>
<tbody>{"".join(table_parts)}</tbody></table>"""
    else:
        file_table = '<div class="empty">此分類下尚無文件</div>'

    body = f"""
{ok_html}
<div class="stats">
  <div class="stat"><span class="stat-val">{doc_count}</span> 份文件{cat_title}</div>
  <div class="stat"><span class="stat-val">{len(cats)}</span> 個分類</div>
</div>

<div class="card">
  <h2>📤 上傳文件</h2>
  <form method="post" action="/upload_form" enctype="multipart/form-data" onsubmit="showUploadLoading(this)">
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px" class="upload-grid">
      <div>
        <label>選擇檔案（.txt / .md / .pdf）</label>
        <input type="file" name="file" accept=".txt,.md,.pdf" required>
      </div>
      <div>
        <label>標籤名稱（選填）</label>
        <input type="text" name="source_name" placeholder="e.g. 技術規格書">
      </div>
      <div>
        <label>分類路徑（可用 / 分層）</label>
        <input type="text" name="category" placeholder="e.g. 技術文件/Java" list="cat-list" value="{cat}">
        <datalist id="cat-list">{cat_options}</datalist>
      </div>
    </div>
    <button class="btn" id="upload-btn" type="submit">上傳並向量化</button>
    <span id="upload-loading" style="display:none;margin-left:14px;color:#94a3b8;font-size:.85rem">
      <span style="display:inline-block;width:14px;height:14px;border:2px solid #4b5563;border-top-color:#a78bfa;border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:middle;margin-right:6px"></span>
      上傳中，請稍候…（向量化約需 15 秒）
    </span>
  </form>
<script>
function showUploadLoading() {{
  document.getElementById('upload-btn').disabled = true;
  document.getElementById('upload-loading').style.display = 'inline';
}}
</script>
</div>

<div class="card">
  <h2>📋 文件列表{cat_title}</h2>
  {file_table}
</div>

<div class="card">
  <h2>🔍 快速搜尋</h2>
  <div class="form-row">
    <input type="text" id="qs-input" placeholder="輸入查詢關鍵字…"
           onkeydown="if(event.key==='Enter')goSearch()">
    <select id="qs-cat">
      <option value="">全部分類</option>
      {"".join(f'<option value="{c}">{c}</option>' for c in cats)}
    </select>
    <button class="btn" onclick="goSearch()">搜尋</button>
  </div>
<script>
function goSearch() {{
  const q = document.getElementById('qs-input').value.trim();
  const c = document.getElementById('qs-cat').value;
  if (!q) return;
  let url = '/search_ui?q=' + encodeURIComponent(q);
  if (c) url += '&category=' + encodeURIComponent(c);
  window.location.href = url;
}}
</script>
</div>
"""
    return HTMLResponse(_base_html(body, "Dashboard — RAG KB", sidebar_cats=cats))

# ═══════════════════════════════════════════════════════════════════════════════
#  Upload
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/upload_form")
async def upload_form(
    rag_token: Optional[str] = Cookie(None),
    file: UploadFile = File(...),
    source_name: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    raw = await file.read()
    name = source_name.strip() if source_name and source_name.strip() else (file.filename or "unnamed")
    cat  = (category.strip().strip("/") if category and category.strip() else "未分類")

    if file.filename and file.filename.lower().endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except ImportError:
            return RedirectResponse("/dashboard?msg=PDF+支援需安裝+pdfplumber", status_code=303)
    else:
        text = raw.decode("utf-8", errors="replace")

    file_id  = str(uuid.uuid4())
    save_ext = Path(file.filename).suffix if file.filename else ".txt"
    if save_ext.lower() not in ('.txt', '.md'):
        save_ext = '.txt'
    save_path = FILES_DIR / f"{file_id}{save_ext}"
    save_path.write_text(text, encoding="utf-8")

    _filemeta.append({
        "id": file_id,
        "filename": file.filename or "unnamed",
        "source_name": name,
        "category": cat,
        "size": len(raw),
        "uploaded_at": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
        "ext": save_ext,
    })
    _save_filemeta()

    import threading
    threading.Thread(target=_qmd_update_embed, daemon=True).start()

    return RedirectResponse(
        f"/dashboard?msg=上傳成功：{file.filename}（分類：{cat}）已排入向量化&cat={cat}",
        status_code=303
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  Download / Delete
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/download/{file_id}")
def download(file_id: str, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")
    fm = next((f for f in _filemeta if f["id"] == file_id), None)
    if not fm:
        raise HTTPException(404)
    path = Path(fm["path"])
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(str(path), filename=fm["filename"])

@app.post("/delete/{file_id}")
def delete_file(file_id: str, rag_token: Optional[str] = Cookie(None)):
    global _filemeta
    if not _auth(rag_token):
        return RedirectResponse("/login")
    fm = next((f for f in _filemeta if f["id"] == file_id), None)
    if not fm:
        raise HTTPException(404)
    p = Path(fm["path"])
    if p.exists():
        p.unlink()
    _filemeta = [f for f in _filemeta if f["id"] != file_id]
    _save_filemeta()
    import threading
    threading.Thread(target=_qmd_update_embed, daemon=True).start()
    return RedirectResponse(f"/dashboard?msg=已刪除：{fm['filename']}", status_code=303)

# ═══════════════════════════════════════════════════════════════════════════════
#  Vault — General File Storage (upload/download any file type)
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n/1024/1024:.1f} MB"
    if n >= 1024:
        return f"{n/1024:.1f} KB"
    return f"{n} B"

@app.get("/vault", response_class=HTMLResponse)
def vault_page(rag_token: Optional[str] = Cookie(None), msg: str = ""):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    ok_html = f'<div class="alert alert-success">{msg}</div>' if msg else ""

    rows = ""
    for f in reversed(_vaultmeta):
        icon = "📄"
        ext = Path(f["filename"]).suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
            icon = "🖼️"
        elif ext in (".zip", ".tar", ".gz", ".7z", ".rar"):
            icon = "📦"
        elif ext in (".pdf",):
            icon = "📕"
        elif ext in (".mp4", ".mov", ".avi", ".mkv"):
            icon = "🎬"
        elif ext in (".mp3", ".wav", ".m4a", ".ogg"):
            icon = "🎵"
        elif ext in (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".sh", ".md"):
            icon = "💾"
        rows += f"""<tr>
  <td style="padding:10px 12px">{icon} <span style="color:#e2e8f0">{f['filename']}</span></td>
  <td style="padding:10px 12px;color:#94a3b8;font-size:.82rem">{_fmt_size(f['size'])}</td>
  <td style="padding:10px 12px;color:#64748b;font-size:.82rem">{f.get('uploaded_at','')}</td>
  <td style="padding:10px 12px;white-space:nowrap">
    <a href="/vault/download/{f['id']}" class="btn btn-sm btn-ghost">⬇ 下載</a>
    <form method="post" action="/vault/delete/{f['id']}" style="display:inline" onsubmit="return confirm('確認刪除？')">
      <button class="btn btn-sm btn-danger" type="submit">🗑</button>
    </form>
  </td>
</tr>"""

    file_table = f"""<div class="table-wrap"><table>
<thead><tr>
  <th>檔案名稱</th><th>大小</th><th>上傳時間</th><th>操作</th>
</tr></thead>
<tbody>{rows}</tbody>
</table></div>""" if _vaultmeta else '<div class="empty" style="padding:24px;color:#64748b;text-align:center">尚無檔案，快來上傳吧！</div>'

    body = f"""{ok_html}
<div class="card">
  <h2>📦 檔案庫 — 上傳任意格式</h2>
  <form method="post" action="/vault/upload" enctype="multipart/form-data" onsubmit="showVaultLoading(this)">
    <div style="display:grid;grid-template-columns:1fr auto;gap:12px;align-items:end">
      <div>
        <label>選擇檔案（任意格式）</label>
        <input type="file" name="file" required>
      </div>
      <div>
        <button class="btn" id="vault-btn" type="submit">上傳</button>
        <span id="vault-loading" style="display:none;margin-left:10px;color:#94a3b8;font-size:.85rem">上傳中…</span>
      </div>
    </div>
  </form>
  <script>
  function showVaultLoading() {{
    document.getElementById('vault-btn').disabled = true;
    document.getElementById('vault-loading').style.display = 'inline';
  }}
  </script>
</div>

<div class="card">
  <h2>📋 已上傳的檔案（共 {len(_vaultmeta)} 個）</h2>
  {file_table}
</div>
"""
    return HTMLResponse(_base_html(body, "檔案庫 — RAG KB"))

@app.post("/vault/upload")
async def vault_upload(
    rag_token: Optional[str] = Cookie(None),
    file: UploadFile = File(...),
):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    raw = await file.read()
    file_id  = str(uuid.uuid4())
    orig_name = file.filename or "unnamed"
    ext = Path(orig_name).suffix or ""
    save_path = VAULT_DIR / f"{file_id}{ext}"
    save_path.write_bytes(raw)

    _vaultmeta.append({
        "id": file_id,
        "filename": orig_name,
        "size": len(raw),
        "uploaded_at": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
    })
    _save_vaultmeta()

    return RedirectResponse(f"/vault?msg=✅+已上傳：{orig_name}", status_code=303)

@app.get("/vault/download/{file_id}")
def vault_download(file_id: str, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")
    fm = next((f for f in _vaultmeta if f["id"] == file_id), None)
    if not fm:
        raise HTTPException(404)
    path = Path(fm["path"])
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(str(path), filename=fm["filename"])

@app.post("/vault/delete/{file_id}")
def vault_delete(file_id: str, rag_token: Optional[str] = Cookie(None)):
    global _vaultmeta
    if not _auth(rag_token):
        return RedirectResponse("/login")
    fm = next((f for f in _vaultmeta if f["id"] == file_id), None)
    if not fm:
        raise HTTPException(404)
    p = Path(fm["path"])
    if p.exists():
        p.unlink()
    _vaultmeta = [f for f in _vaultmeta if f["id"] != file_id]
    _save_vaultmeta()
    return RedirectResponse(f"/vault?msg=已刪除：{fm['filename']}", status_code=303)

# ═══════════════════════════════════════════════════════════════════════════════
#  Search UI
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/search_ui", response_class=HTMLResponse)
def search_ui(q: str = "", category: str = "", rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    cats = _all_categories()
    cat_options = "\n".join(
        f'<option value="{c}" {"selected" if c==category else ""}>{c}</option>'
        for c in cats
    )

    body = f"""
<div class="card">
  <h2>🔍 語意搜尋</h2>
  <div style="display:grid;grid-template-columns:1fr auto auto;gap:12px;margin-bottom:20px;align-items:end" class="search-grid">
    <div>
      <label>查詢關鍵字</label>
      <input type="text" id="search-input" value="{q}" placeholder="輸入查詢關鍵字…" autofocus
             onkeydown="if(event.key==='Enter')doSearch()">
    </div>
    <div>
      <label>分類過濾</label>
      <select id="search-cat" style="width:180px">
        <option value="">全部分類</option>
        {cat_options}
      </select>
    </div>
    <div>
      <button class="btn" id="search-btn" onclick="doSearch()">搜尋</button>
    </div>
  </div>

  <div id="loading" style="display:none;text-align:center;padding:40px">
    <div class="spinner"></div>
    <div style="color:#94a3b8;font-size:.85rem;margin-top:14px">向量搜尋中，請稍候…<br>
      <span style="font-size:.75rem;color:#4b5563">（首次查詢需要 10–30 秒載入模型）</span>
    </div>
  </div>
  <div id="results"></div>
</div>

<script>
const initialQ   = {json.dumps(q)};
const initialCat = {json.dumps(category)};

async function doSearch() {{
  const q   = document.getElementById('search-input').value.trim();
  const cat = document.getElementById('search-cat').value;
  if (!q) return;

  let qs = '?q=' + encodeURIComponent(q);
  if (cat) qs += '&category=' + encodeURIComponent(cat);
  history.replaceState(null, '', '/search_ui' + qs);

  document.getElementById('loading').style.display = 'block';
  document.getElementById('results').innerHTML = '';
  document.getElementById('search-btn').disabled = true;

  try {{
    const res  = await fetch('/search' + qs + '&top_k=5');
    const data = await res.json();

    document.getElementById('loading').style.display = 'none';
    document.getElementById('search-btn').disabled = false;

    if (!data.results || data.results.length === 0) {{
      document.getElementById('results').innerHTML =
        '<div class="empty">沒有找到相關段落。<br><span style="font-size:.8rem;color:#4b5563">請確認文件已上傳並完成向量化（約需 15 秒）</span></div>';
      return;
    }}

    let rows = '';
    for (const r of data.results) {{
      const score  = (r.score || 0).toFixed(3);
      const source = r.title || '—';
      const cat    = r.category || '—';
      const body   = (r.body || '').substring(0, 300)
        .replace(/</g,'&lt;').replace(/>/g,'&gt;');
      rows += `<tr>
        <td style="color:#a78bfa;font-weight:600">${{score}}</td>
        <td><span class="badge badge-purple">${{source}}</span></td>
        <td><span class="badge badge-blue">${{cat}}</span></td>
        <td style="white-space:pre-wrap;font-size:.8rem;color:#cbd5e1">${{body}}…</td>
      </tr>`;
    }}
    document.getElementById('results').innerHTML = `
      <table>
        <thead><tr><th>相關度</th><th>來源</th><th>分類</th><th>內容片段</th></tr></thead>
        <tbody>${{rows}}</tbody>
      </table>`;
  }} catch(e) {{
    document.getElementById('loading').style.display = 'none';
    document.getElementById('search-btn').disabled = false;
    document.getElementById('results').innerHTML =
      '<div class="alert alert-error">搜尋失敗：' + e.message + '</div>';
  }}
}}

if (initialQ) doSearch();
</script>
"""
    return HTMLResponse(_base_html(body, "搜尋 — RAG KB", sidebar_cats=cats))

# ═══════════════════════════════════════════════════════════════════════════════
#  JSON API
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {
        "status": "ok",
        "backend": "qmd+sqlite-vec",
        "doc_count": len(_filemeta),
        "categories": _all_categories(),
    }

@app.get("/categories")
def list_categories():
    """List all categories and document counts per category."""
    from collections import Counter
    counts = Counter(f.get("category", "未分類") for f in _filemeta)
    return {"categories": [{"name": k, "count": v} for k, v in sorted(counts.items())]}

@app.get("/search")
def search_api(q: str, top_k: int = TOP_K, category: Optional[str] = None, source: Optional[str] = None):
    """
    Vector search API.
    ?q=query             — search query (required)
    &category=技術文件    — filter by category prefix (optional)
    &top_k=5             — number of results (optional)
    """
    results = _sqlite_vsearch(q, top_k, category=category)
    if source:
        results = [r for r in results if source.lower() in (r.get("title","") + r.get("file","")).lower()]
    return {"query": q, "category_filter": category, "results": results[:top_k]}

@app.post("/upload_text")
async def upload_text_api(payload: dict):
    text     = payload.get("text", "")
    source   = payload.get("source", "inline-" + str(uuid.uuid4())[:8])
    category = (payload.get("category", "未分類") or "未分類").strip().strip("/")
    if not text.strip():
        raise HTTPException(400, "text is empty")

    file_id   = str(uuid.uuid4())
    save_path = FILES_DIR / f"{file_id}.txt"
    save_path.write_text(text, encoding="utf-8")
    _filemeta.append({
        "id": file_id,
        "filename": f"{source}.txt",
        "source_name": source,
        "category": category,
        "size": len(text.encode()),
        "uploaded_at": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
        "ext": ".txt",
    })
    _save_filemeta()
    import threading
    threading.Thread(target=_qmd_update_embed, daemon=True).start()
    return {"source": source, "category": category, "total_docs": len(_filemeta)}

@app.get("/sources")
def list_sources():
    return {"sources": [
        {"name": f["source_name"], "filename": f["filename"], "category": f.get("category","未分類")}
        for f in _filemeta
    ]}

@app.get("/doc/{file_id}")
def get_doc_content(file_id: str):
    """
    Return full content of a document by file_id.
    Used by AI to retrieve complete 題目.md or 評分方式.md for grading.
    """
    fm = next((f for f in _filemeta if f["id"] == file_id), None)
    if not fm:
        raise HTTPException(404, "Document not found")
    path = Path(fm["path"])
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    content = path.read_text(encoding="utf-8")
    return {
        "id": file_id,
        "filename": fm["filename"],
        "source_name": fm["source_name"],
        "category": fm.get("category", "未分類"),
        "content": content,
    }

@app.get("/doc_by_source")
def get_doc_by_source(source_name: str):
    """
    Return full content by source_name (fuzzy: contains match).
    e.g. GET /doc_by_source?source_name=題目
    """
    matches = [f for f in _filemeta if source_name.lower() in f.get("source_name","").lower()]
    if not matches:
        raise HTTPException(404, f"No document matching source_name='{source_name}'")
    results = []
    for fm in matches:
        path = Path(fm["path"])
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        results.append({
            "id": fm["id"],
            "filename": fm["filename"],
            "source_name": fm["source_name"],
            "category": fm.get("category", "未分類"),
            "content": content,
        })
    return {"results": results}

# ═══════════════════════════════════════════════════════════════════════════════
#  Grade API — fetch GitHub repo + call OpenClaw for grading
# ═══════════════════════════════════════════════════════════════════════════════

def _github_tree(owner: str, repo: str, branch: str, token: Optional[str]) -> List[dict]:
    """Fetch recursive file tree from GitHub API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "rag-kb-grader"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return [item for item in data.get("tree", []) if item.get("type") == "blob"]

def _github_file(owner: str, repo: str, branch: str, path: str, token: Optional[str]) -> str:
    """Fetch raw file content from GitHub."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    headers = {"User-Agent": "rag-kb-grader"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

def _parse_github_url(url: str) -> tuple:
    """Parse GitHub URL → (owner, repo, branch). branch defaults to 'main'."""
    url = url.strip().rstrip("/")
    # https://github.com/owner/repo/tree/branch
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)", url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    # https://github.com/owner/repo
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)", url)
    if m:
        return m.group(1), m.group(2), "main"
    raise ValueError(f"無法解析 GitHub URL: {url}")

PRIORITY_EXTS = {".java", ".kt", ".py", ".ts", ".js", ".go", ".cs", ".sql", ".md", ".xml", ".gradle"}
PRIORITY_DIRS = {"src", "main", "controller", "service", "entity", "repository", "model", "api", "db"}

def _should_read(path: str) -> bool:
    ext = Path(path).suffix.lower()
    if ext not in PRIORITY_EXTS:
        return False
    parts = set(Path(path).parts)
    # Skip test files and build output
    if any(p in parts for p in {"test", "tests", ".git", "target", "build", "node_modules", "__pycache__"}):
        return False
    return True

@app.post("/grade")
async def grade_api(payload: dict):
    """
    Collect GitHub repo code + KB questions/scoring → return grading context.
    Body: { "repo_url": "...", "token": "ghp_xxx (optional)" }
    """
    repo_url = payload.get("repo_url", "").strip()
    token    = (payload.get("token") or "").strip() or None
    if not repo_url:
        raise HTTPException(400, "repo_url is required")

    try:
        owner, repo, branch = _parse_github_url(repo_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    questions = [f for f in _filemeta if "題目" in f.get("category","") or "題目" in f.get("source_name","")]
    scoring   = [f for f in _filemeta if "評分" in f.get("category","") or "評分" in f.get("source_name","")]
    if not questions:
        raise HTTPException(404, "知識庫中找不到題目文件")
    if not scoring:
        raise HTTPException(404, "知識庫中找不到評分方式文件")

    scoring_content = Path(scoring[0]["path"]).read_text(encoding="utf-8")
    try:
        tree = _github_tree(owner, repo, branch, token)
    except Exception as e:
        raise HTTPException(502, f"GitHub API 錯誤：{e}")

    code_files = [item["path"] for item in tree if _should_read(item["path"])][:30]
    code_snippets = []
    for fpath in code_files:
        content = _github_file(owner, repo, branch, fpath, token)
        if content:
            code_snippets.append({"path": fpath, "content": content[:3000]})

    questions_text = ""
    for q in questions:
        qcontent = Path(q["path"]).read_text(encoding="utf-8")
        questions_text += f"\n\n---\n## 題目：{q['source_name']}\n{qcontent}"

    code_summary = "\n\n".join(
        f"### {s['path']}\n```\n{s['content'][:1500]}\n```"
        for s in code_snippets[:15]
    )
    grading_prompt = f"""你是一位嚴格但公正的後端工程師面試評審。

⚠️ 重要規則：評分時必須嚴格遵守「評分方式」文件中的每一條規則。分數只能按照評分標準計算，不得自行加分、寬鬆評分或給予同情分。若程式碼未完整實作某功能，該項目只能得到實際完成比例應得的分數。

請根據以下資訊進行評分：

# 考題內容
{questions_text}

# 評分方式
{scoring_content}

# 應試者的 GitHub Repo
- URL: {repo_url}
- 分支: {branch}
- 共 {len(tree)} 個檔案，已讀取 {len(code_snippets)} 個主要程式碼檔案

# 程式碼內容
{code_summary}

---

請完成以下任務：

1. **判定題目**：分析程式碼，確認這份 repo 對應「考題內容」中的哪一道題目，說明判斷依據。

2. **逐項評分**：按照「評分方式」的每個評分項目，逐一評定是否達標，給出分數。

3. **輸出報告**，格式如下（務必嚴格遵守此格式，不得自行更改）：

## 評分結果

### 是否符合技術要求（共 N 項）：X 項
（逐項列出，格式：✅ 項目名稱 / ❌ 項目名稱，並簡述理由）

### 是否符合架構要求（共 N 項）：X 項
（逐項列出，格式：✅ 項目名稱 / ❌ 項目名稱，並簡述理由）

### 是否符合功能要求（共 N 項）：X 項
（逐項列出，格式：✅ 項目名稱 / ❌ 項目名稱，並簡述理由）

---

## 總評 ({{等級符號}})
（等級符號填入：◎ ○ ▲ △ × 其中之一）

### 未達標部分
1. （具體說明未達標的項目與原因）
2. ...

### 其他評論
- （其他觀察，例如：是否採用 TypeScript、是否有 AI 生成痕跡等）

---

## 結果
- **分數：** {{數字}} ({{等級符號}})
- **達標項目數 / 總項目數：** {{達標數}} / {{總項目數}} = {{比率小數}}

### 評分等級說明
◎（90-100）/ ○（80-89）/ ▲（60-79）/ △（30-59）/ ×（29 以下）
"""
    return {
        "owner": owner, "repo": repo, "branch": branch,
        "file_count": len(tree), "code_files_read": len(code_snippets),
        "questions_available": [q["source_name"] for q in questions],
        "grading_prompt": grading_prompt,
    }


@app.get("/grade_stream")
async def grade_stream(repo_url: str, token: str = ""):
    """
    SSE endpoint: streams grading progress events then final prompt.
    GET /grade_stream?repo_url=...&token=...
    """
    _token = token.strip() or None

    async def event_stream() -> AsyncGenerator[str, None]:
        def sse(event: str, data: str) -> str:
            # Escape newlines in data
            data_lines = "\n".join(f"data: {line}" for line in data.split("\n"))
            return f"event: {event}\n{data_lines}\n\n"

        try:
            yield sse("progress", "🔍 解析 GitHub URL…")
            try:
                owner, repo, branch = _parse_github_url(repo_url)
            except ValueError as e:
                yield sse("error", str(e)); return

            yield sse("progress", f"📚 從知識庫載入題目與評分方式…")
            questions = [f for f in _filemeta if "題目" in f.get("category","") or "題目" in f.get("source_name","")]
            scoring   = [f for f in _filemeta if "評分" in f.get("category","") or "評分" in f.get("source_name","")]
            if not questions:
                yield sse("error", "知識庫中找不到題目文件，請先上傳"); return
            if not scoring:
                yield sse("error", "知識庫中找不到評分方式文件，請先上傳"); return

            scoring_content = Path(scoring[0]["path"]).read_text(encoding="utf-8")
            q_names = [q["source_name"] for q in questions]
            yield sse("progress", f"✅ 已載入 {len(questions)} 道題目：{', '.join(q_names)}")

            yield sse("progress", f"🌐 連接 GitHub API，取得 {owner}/{repo}@{branch} 檔案列表…")
            try:
                tree = _github_tree(owner, repo, branch, _token)
            except Exception as e:
                yield sse("error", f"GitHub API 錯誤：{e}"); return
            yield sse("progress", f"📁 共找到 {len(tree)} 個檔案")

            code_files = [item["path"] for item in tree if _should_read(item["path"])][:30]
            yield sse("progress", f"📖 讀取 {len(code_files)} 個程式碼檔案…")

            code_snippets = []
            for i, fpath in enumerate(code_files):
                content = _github_file(owner, repo, branch, fpath, _token)
                if content:
                    code_snippets.append({"path": fpath, "content": content[:3000]})
                if (i+1) % 5 == 0:
                    yield sse("progress", f"  已讀取 {i+1}/{len(code_files)} 個檔案…")

            yield sse("progress", f"✅ 成功讀取 {len(code_snippets)} 個程式碼檔案")

            # Build questions text
            questions_text = ""
            for q in questions:
                qcontent = Path(q["path"]).read_text(encoding="utf-8")
                questions_text += f"\n\n---\n## 題目：{q['source_name']}\n{qcontent}"

            code_summary = "\n\n".join(
                f"### {s['path']}\n```\n{s['content'][:1500]}\n```"
                for s in code_snippets[:15]
            )

            grading_prompt = f"""你是一位嚴格但公正的後端工程師面試評審。

⚠️ 重要規則：評分時必須嚴格遵守「評分方式」文件中的每一條規則。分數只能按照評分標準計算，不得自行加分、寬鬆評分或給予同情分。若程式碼未完整實作某功能，該項目只能得到實際完成比例應得的分數。

請根據以下資訊進行評分：

# 考題內容
{questions_text}

# 評分方式
{scoring_content}

# 應試者的 GitHub Repo
- URL: {repo_url}
- 分支: {branch}
- 共 {len(tree)} 個檔案，已讀取 {len(code_snippets)} 個主要程式碼檔案

# 程式碼內容
{code_summary}

---

請完成以下任務：

1. **判定題目**：分析程式碼，確認這份 repo 對應「考題內容」中的哪一道題目，說明判斷依據。

2. **逐項評分**：按照「評分方式」的每個評分項目，逐一評定是否達標，給出分數。

3. **輸出報告**，格式如下（務必嚴格遵守此格式，不得自行更改）：

## 評分結果

### 是否符合技術要求（共 N 項）：X 項
（逐項列出，格式：✅ 項目名稱 / ❌ 項目名稱，並簡述理由）

### 是否符合架構要求（共 N 項）：X 項
（逐項列出，格式：✅ 項目名稱 / ❌ 項目名稱，並簡述理由）

### 是否符合功能要求（共 N 項）：X 項
（逐項列出，格式：✅ 項目名稱 / ❌ 項目名稱，並簡述理由）

---

## 總評 ({{等級符號}})
（等級符號填入：◎ ○ ▲ △ × 其中之一）

### 未達標部分
1. （具體說明未達標的項目與原因）
2. ...

### 其他評論
- （其他觀察，例如：是否採用 TypeScript、是否有 AI 生成痕跡等）

---

## 結果
- **分數：** {{數字}} ({{等級符號}})
- **達標項目數 / 總項目數：** {{達標數}} / {{總項目數}} = {{比率小數}}

### 評分等級說明
◎（90-100）/ ○（80-89）/ ▲（60-79）/ △（30-59）/ ×（29 以下）
"""
            yield sse("progress", "✅ 評分 Prompt 已建立完成！")
            # Send the full prompt as a done event
            yield sse("done", json.dumps({
                "owner": owner, "repo": repo, "branch": branch,
                "file_count": len(tree), "code_files_read": len(code_snippets),
                "questions_available": q_names,
                "grading_prompt": grading_prompt,
            }))

        except Exception as e:
            yield sse("error", f"未預期的錯誤：{e}")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        }
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  Grade UI  (SSE version)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/grade_ui", response_class=HTMLResponse)
def grade_ui(rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    question_count = len([f for f in _filemeta if "題目" in f.get("category","") or "題目" in f.get("source_name","")])
    scoring_count  = len([f for f in _filemeta if "評分" in f.get("category","") or "評分" in f.get("source_name","")])
    q_list = [f["source_name"] for f in _filemeta if "題目" in f.get("category","") or "題目" in f.get("source_name","")]

    status_color = "#86efac" if question_count > 0 and scoring_count > 0 else "#fca5a5"
    status_icon  = "✅" if question_count > 0 and scoring_count > 0 else "⚠️"
    status_msg   = f"{question_count} 道題目、{scoring_count} 份評分方式已載入" if question_count > 0 else "尚未上傳題目或評分方式，請先至 Dashboard 上傳"
    q_badges = "".join(f'<span class="badge badge-blue" style="margin:2px">{q}</span>' for q in q_list)

    body = f"""
<div class="card">
  <h2>📝 GitHub 作業自動評分</h2>

  <!-- KB 狀態列 -->
  <div style="background:#0f1117;border:1px solid #2d3154;border-radius:8px;padding:14px;margin-bottom:20px;display:flex;align-items:center;gap:12px">
    <span style="font-size:1.4rem">{status_icon}</span>
    <div>
      <div style="font-size:.85rem;color:{status_color}">{status_msg}</div>
      <div style="margin-top:6px">{q_badges}</div>
    </div>
  </div>

  <!-- 輸入 -->
  <div style="display:grid;grid-template-columns:1fr auto;gap:12px;margin-bottom:14px;align-items:end" class="grade-grid">
    <div>
      <label>GitHub Repo URL</label>
      <input type="text" id="repo-url" placeholder="https://github.com/owner/repo/tree/main">
    </div>
    <div>
      <label>GitHub Token（私有 repo 才需要）</label>
      <input type="text" id="gh-token" placeholder="ghp_xxxxx（選填）" style="width:260px">
    </div>
  </div>

  <div style="display:flex;align-items:center;gap:14px">
    <button class="btn" id="grade-btn" onclick="doGrade()">🚀 開始評分</button>
    <button class="btn btn-ghost" id="stop-btn" style="display:none" onclick="stopGrade()">⏹ 停止</button>
    <span style="font-size:.8rem;color:#64748b">資料收集完成後會立即顯示 Prompt，無需等待 AI 評分</span>
  </div>
</div>

<!-- 進度 Log -->
<div id="progress-card" style="display:none">
  <div class="card">
    <h2>⏳ 進度</h2>
    <div id="progress-log" style="
      font-family:monospace;font-size:.82rem;color:#94a3b8;
      background:#0a0a0f;border:1px solid #1e2235;border-radius:8px;
      padding:14px;max-height:220px;overflow-y:auto;line-height:1.8
    "></div>
  </div>
</div>

<!-- Prompt 就緒（可提前使用）-->
<div id="prompt-card" style="display:none">
  <div class="card">
    <h2>📄 評分 Prompt <span style="font-size:.75rem;color:#64748b;font-weight:400">— 可複製到任何 AI 立即評分</span></h2>
    <div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">
      <button class="btn btn-sm" onclick="copyPrompt()">📋 複製 Prompt</button>
      <button class="btn btn-sm btn-ghost" onclick="downloadPrompt()">⬇ 下載 .txt</button>
      <a id="open-claude" href="#" target="_blank" class="btn btn-sm btn-ghost"
         style="text-decoration:none" onclick="openClaude()">🤖 在 Claude 開啟</a>
    </div>
    <div id="prompt-meta" style="font-size:.8rem;color:#64748b;margin-bottom:10px"></div>
    <pre id="prompt-box" style="
      white-space:pre-wrap;font-size:.78rem;color:#94a3b8;
      background:#0a0a0f;border:1px solid #1e2235;border-radius:8px;
      padding:14px;max-height:400px;overflow-y:auto
    "></pre>
  </div>
</div>

<!-- 報告區（AI 自動回傳時顯示）-->
<div id="report-card" style="display:none">
  <div class="card">
    <h2>📊 AI 評分報告</h2>
    <div style="display:flex;gap:10px;margin-bottom:14px">
      <button class="btn btn-sm btn-ghost" onclick="copyReport()">📋 複製報告</button>
      <button class="btn btn-sm btn-ghost" onclick="downloadReport()">⬇ 下載 .md</button>
    </div>
    <div id="report-html" style="
      background:#0f1117;border:1px solid #2d3154;border-radius:8px;
      padding:20px;font-size:.88rem;line-height:1.7;overflow-x:auto
    "></div>
  </div>
</div>

<div id="error-area"></div>

<!-- 對話視窗（評分完成後出現）-->
<div id="chat-card" style="display:none">
  <div class="card">
    <h2>💬 與 AI 討論評分結果</h2>
    <p style="font-size:.83rem;color:#64748b;margin-bottom:14px">評分報告已作為 context，可直接詢問如何改善、解釋評分理由、或討論具體題目</p>
    <div id="chat-messages" style="
      background:#0a0a0f;border:1px solid #1e2235;border-radius:8px;
      padding:14px;min-height:120px;max-height:480px;overflow-y:auto;
      font-size:.87rem;line-height:1.7;margin-bottom:12px
    "></div>
    <div style="display:flex;gap:10px;align-items:flex-end">
      <textarea id="chat-input" rows="2" placeholder="例如：第 3 題為什麼只得 X 分？如何改善？"
        style="flex:1;resize:vertical;min-height:56px;font-family:inherit;font-size:.87rem"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendChat();}}"></textarea>
      <button class="btn" id="chat-send-btn" onclick="sendChat()" style="white-space:nowrap">
        ➤ 送出
      </button>
    </div>
    <div style="font-size:.75rem;color:#475569;margin-top:6px">Enter 送出 ｜ Shift+Enter 換行</div>
    <hr style="border-color:#2d3154;margin:16px 0">
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <button class="btn btn-ghost" id="regen-btn" onclick="regenReport()" style="white-space:nowrap">
        🔄 根據討論重新產製評分報告
      </button>
      <span style="font-size:.78rem;color:#64748b">將對話內容納入考量，產製修正版報告並更新上方報告區</span>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<script>
mermaid.initialize({{ startOnLoad: false, theme: 'dark' }});

function renderMermaid() {{
  document.querySelectorAll('code.language-mermaid').forEach(function(code) {{
    var pre = code.parentElement;
    if (pre.tagName === 'PRE') {{
      var div = document.createElement('div');
      div.className = 'mermaid';
      div.textContent = code.textContent;
      pre.parentElement.replaceChild(div, pre);
    }}
  }});
  mermaid.init(undefined, document.querySelectorAll('.mermaid'));
}}
</script>
<script>
let _prompt   = '';
let _reportMd = '';
let _sse      = null;
let _meta     = {{}};
let _chatHistory = [];  // {{role, content}}

function log(msg) {{
  const el = document.getElementById('progress-log');
  el.innerHTML += msg + '<br>';
  el.scrollTop = el.scrollHeight;
}}

function doGrade() {{
  const repoUrl = document.getElementById('repo-url').value.trim();
  const token   = document.getElementById('gh-token').value.trim();
  if (!repoUrl) {{ alert('請輸入 GitHub Repo URL'); return; }}

  // Reset UI
  document.getElementById('grade-btn').disabled = true;
  document.getElementById('stop-btn').style.display = 'inline-block';
  document.getElementById('progress-card').style.display = 'block';
  document.getElementById('progress-log').innerHTML = '';
  document.getElementById('prompt-card').style.display = 'none';
  document.getElementById('report-card').style.display = 'none';
  document.getElementById('error-area').innerHTML = '';
  _prompt = ''; _reportMd = '';

  const params = new URLSearchParams({{ repo_url: repoUrl, token: token }});
  _sse = new EventSource('/grade_stream?' + params.toString());

  _sse.addEventListener('progress', e => {{
    log('<span style="color:#a78bfa">' + escHtml(e.data) + '</span>');
  }});

  _sse.addEventListener('done', e => {{
    _sse.close();
    document.getElementById('stop-btn').style.display = 'none';
    document.getElementById('grade-btn').disabled = false;

    const data = JSON.parse(e.data);
    _meta   = data;
    _prompt = data.grading_prompt;

    log('<span style="color:#86efac">✅ 完成！共讀取 ' + data.code_files_read + ' 個程式碼檔案</span>');

    // Show prompt card immediately
    document.getElementById('prompt-card').style.display = 'block';
    document.getElementById('prompt-meta').textContent =
      data.owner + '/' + data.repo + ' @' + data.branch +
      ' ｜ ' + data.file_count + ' 個檔案 ｜ 可對照題目：' + data.questions_available.join('、');
    document.getElementById('prompt-box').textContent = _prompt;

    // Auto-call AI grading
    log('<span style="color:#60a5fa">🤖 正在呼叫 AI 評分（claude-sonnet-4.6）...</span>');
    fetch('/ai_grade', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ prompt: _prompt, meta: _meta }})
    }})
    .then(r => {{
      if (!r.ok) {{
        return r.text().then(t => {{ throw new Error(`HTTP ${{r.status}}: ${{t.substring(0, 100)}}`); }});
      }}
      return r.json();
    }})
    .then(result => {{
      if (result.auto) {{
        _reportMd = result.report;
        document.getElementById('report-card').style.display = 'block';
        document.getElementById('report-html').innerHTML = marked.parse(_reportMd);
        log('<span style="color:#86efac">✅ AI 評分完成！</span>');
        // Show chat panel
        _chatHistory = [];
        document.getElementById('chat-card').style.display = 'block';
        document.getElementById('chat-messages').innerHTML = '';
        appendChatMsg('assistant', '評分完成！您可以在這裡詢問任何關於評分結果的問題，例如：「第 3 題為何扣分？」、「如何改善這份作業？」、「幫我寫一份改進建議給同學」');
      }} else {{
        log('<span style="color:#fbbf24">⚠️ ' + escHtml(result.message || 'AI 評分不可用') + '</span>');
      }}
    }})
    .catch(e => {{
      log('<span style="color:#fca5a5">❌ AI 評分呼叫失敗：' + escHtml(String(e)) + '</span>');
    }});
  }});

  _sse.addEventListener('error', e => {{
    _sse.close();
    document.getElementById('stop-btn').style.display = 'none';
    document.getElementById('grade-btn').disabled = false;
    if (e.data) {{
      log('<span style="color:#fca5a5">❌ ' + escHtml(e.data) + '</span>');
      document.getElementById('error-area').innerHTML =
        '<div class="alert alert-error">❌ ' + escHtml(e.data) + '</div>';
    }}
  }});

  _sse.onerror = () => {{
    if (_sse.readyState === EventSource.CLOSED) return;
    _sse.close();
    document.getElementById('stop-btn').style.display = 'none';
    document.getElementById('grade-btn').disabled = false;
    log('<span style="color:#fca5a5">⚠️ SSE 連線中斷</span>');
  }};
}}

function stopGrade() {{
  if (_sse) {{ _sse.close(); _sse = null; }}
  document.getElementById('stop-btn').style.display = 'none';
  document.getElementById('grade-btn').disabled = false;
  log('<span style="color:#fbbf24">⏹ 已停止</span>');
  // Still show prompt if already collected
  if (_prompt) {{
    document.getElementById('prompt-card').style.display = 'block';
    document.getElementById('prompt-box').textContent = _prompt;
  }}
}}

function copyPrompt() {{
  navigator.clipboard.writeText(_prompt).then(() => alert('評分 Prompt 已複製！'));
}}

function downloadPrompt() {{
  const blob = new Blob([_prompt], {{type:'text/plain'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'grading-prompt.txt';
  a.click();
}}

function openClaude() {{
  // Open Claude with the prompt pre-filled (via URL if supported)
  window.open('https://claude.ai/new', '_blank');
  // Also copy to clipboard so user can paste
  navigator.clipboard.writeText(_prompt).then(() => {{
    alert('已開啟 Claude，並將 Prompt 複製到剪貼簿，請在 Claude 貼上即可！');
  }});
  return false;
}}

function copyReport() {{
  navigator.clipboard.writeText(_reportMd).then(() => alert('已複製報告！'));
}}

function downloadReport() {{
  const blob = new Blob([_reportMd], {{type:'text/markdown'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'grading-report.md';
  a.click();
}}

function escHtml(s) {{
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function appendChatMsg(role, content) {{
  const el = document.getElementById('chat-messages');
  const isUser = role === 'user';
  const div = document.createElement('div');
  div.style.cssText = `margin-bottom:14px;display:flex;flex-direction:column;align-items:${{isUser?'flex-end':'flex-start'}}`;
  const bubble = document.createElement('div');
  bubble.style.cssText = `
    max-width:85%;padding:10px 14px;border-radius:12px;font-size:.87rem;line-height:1.6;
    background:${{isUser?'#3b3f6e':'#1a1d2e'}};
    color:${{isUser?'#e2e8f0':'#cbd5e1'}};
    border:1px solid ${{isUser?'#4f54a0':'#2d3154'}};
  `;
  bubble.innerHTML = isUser ? escHtml(content) : marked.parse(content);
  div.appendChild(bubble);
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}}

function appendChatThinking() {{
  removeChatThinking(); // 清除舊的（防止重複）
  const el = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.id = 'chat-thinking';
  div.style.cssText = 'margin-bottom:14px;display:flex;align-items:flex-start';
  div.innerHTML = '<div style="padding:10px 14px;background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;color:#64748b;font-size:.87rem;display:flex;align-items:center;gap:8px">'
    + '<span style="display:inline-block;width:14px;height:14px;border:2px solid #2d3154;border-top-color:#a78bfa;border-radius:50%;animation:spin 0.8s linear infinite;flex-shrink:0"></span>'
    + '🤖 AI 正在思考中<span id="chat-thinking-dot"></span>'
    + '</div>';
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  // 動態省略號動畫
  let _n = 0;
  window._chatThinkingTimer = setInterval(() => {{
    _n = (_n % 3) + 1;
    const d = document.getElementById('chat-thinking-dot');
    if (d) d.textContent = '.'.repeat(_n);
  }}, 500);
}}

function removeChatThinking() {{
  if (window._chatThinkingTimer) {{
    clearInterval(window._chatThinkingTimer);
    window._chatThinkingTimer = null;
  }}
  const t = document.getElementById('chat-thinking');
  if (t) t.remove();
}}

async function sendChat() {{
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg || !_reportMd) return;
  input.value = '';
  document.getElementById('chat-send-btn').disabled = true;

  // Add user message to UI and history
  appendChatMsg('user', msg);
  _chatHistory.push({{role:'user', content: msg}});
  appendChatThinking();

  try {{
    const resp = await fetch('/chat_with_report', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        report: _reportMd,
        history: _chatHistory.slice(0, -1),  // exclude current msg
        message: msg
      }})
    }});
    const data = await resp.json();
    removeChatThinking();

    const reply = data.reply || '（無回應）';
    appendChatMsg('assistant', reply);
    _chatHistory.push({{role:'assistant', content: reply}});
  }} catch(e) {{
    removeChatThinking();
    appendChatMsg('assistant', '❌ 連線失敗：' + String(e));
  }}
  document.getElementById('chat-send-btn').disabled = false;
  document.getElementById('chat-input').focus();
}}

async function regenReport() {{
  if (!_reportMd || _chatHistory.length === 0) {{
    alert('請先進行至少一輪對話，再重新產製報告');
    return;
  }}
  const btn = document.getElementById('regen-btn');
  btn.disabled = true;
  btn.textContent = '⏳ 重新產製中…';
  appendChatMsg('assistant', '🔄 正在根據討論內容重新產製評分報告，請稍候…');

  try {{
    const resp = await fetch('/regen_report', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        original_report: _reportMd,
        history: _chatHistory
      }})
    }});
    const data = await resp.json();
    if (data.report) {{
      _reportMd = data.report;
      document.getElementById('report-html').innerHTML = marked.parse(_reportMd);
      document.getElementById('report-card').style.display = 'block';
      document.getElementById('report-card').scrollIntoView({{behavior:'smooth', block:'start'}});
      appendChatMsg('assistant', '✅ 修正版評分報告已更新！請向上查看更新後的報告。');
    }} else {{
      appendChatMsg('assistant', '❌ 重新產製失敗：' + escHtml(data.error || '未知錯誤'));
    }}
  }} catch(e) {{
    appendChatMsg('assistant', '❌ 連線失敗：' + String(e));
  }}
  btn.disabled = false;
  btn.textContent = '🔄 根據討論重新產製評分報告';
}}
</script>
"""
    return HTMLResponse(_base_html(body, "評分 — RAG KB"))


def _load_copilot_token() -> str | None:
    """Load GitHub Copilot token, auto-refresh via openclaw if expired."""
    import time as _time
    token_path = os.path.expanduser("~/.openclaw/credentials/github-copilot.token.json")
    openclaw_bin = shutil.which("openclaw") or shutil.which("clawd")

    def _read_token():
        try:
            with open(token_path) as f:
                data = json.load(f)
            token = data.get("token", "")
            expires_at = data.get("expiresAt", 0) / 1000  # ms → s
            if token and expires_at > _time.time() + 60:
                return token
        except Exception:
            pass
        return None

    # First try reading current token
    token = _read_token()
    if token:
        return token

    # Token expired/missing — try refresh via openclaw
    if openclaw_bin:
        try:
            subprocess.run(
                [openclaw_bin, "auth", "refresh", "--provider", "github-copilot"],
                capture_output=True, text=True, timeout=30
            )
            token = _read_token()
            if token:
                return token
        except Exception:
            pass

    # Last resort: return token even if expired (let API decide)
    try:
        with open(token_path) as f:
            data = json.load(f)
        return data.get("token") or None
    except Exception:
        return None


async def _call_copilot_api(prompt: str, model: str = "claude-sonnet-4.6") -> str:
    """Call GitHub Copilot API (OpenAI-compatible) with the given prompt."""
    import httpx
    token = _load_copilot_token()
    if not token:
        raise RuntimeError("GitHub Copilot token not available or expired")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.95.0",
    }
    system_msg = (
        "你是一位嚴格、公正的作業評審。評分時必須嚴格遵守「評分方式」文件中的每一條規則，"
        "不得隨意加分或減分，分數必須完全依照評分標準計算。"
        "若程式碼未達標準，即使部分完成也只能給予該部分應得分數，不得給予同情分。"
        "回應請使用繁體中文。"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 4096,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            "https://api.githubcopilot.com/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


@app.post("/ai_grade")
async def ai_grade(payload: dict):
    """Grade using GitHub Copilot API (claude-sonnet-4.6 via GitHub Copilot)."""
    prompt = payload.get("prompt", "")
    if not prompt:
        raise HTTPException(400, "prompt required")

    try:
        report = await _call_copilot_api(prompt)
        return {"report": report, "auto": True}
    except Exception as e:
        # Fallback: return prompt for manual use
        return {
            "report": prompt,
            "auto": False,
            "message": f"AI 評分失敗（{e}），請複製 Prompt 到 Claude / ChatGPT"
        }


@app.post("/chat_with_report")
async def chat_with_report(payload: dict):
    """Chat with LLM using grading report as system context."""
    import httpx
    report  = payload.get("report", "")
    history = payload.get("history", [])   # [{role, content}, ...]
    message = payload.get("message", "").strip()
    if not message:
        raise HTTPException(400, "message required")

    token = _load_copilot_token()
    if not token:
        raise HTTPException(503, "GitHub Copilot token not available")

    # Build messages: system context + history + new message
    system_content = (
        "你是一位作業評分助理。以下是這份作業的 AI 評分報告，請根據報告內容回答使用者的問題。"
        "回答請使用繁體中文，語氣友善專業。\n\n"
        f"## 評分報告\n\n{report}"
    )
    messages = [{"role": "system", "content": system_content}]
    for h in history[-10:]:  # keep last 10 turns
        role = h.get("role", "user")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.95.0",
    }
    body = {
        "model": "claude-sonnet-4.6",
        "messages": messages,
        "max_tokens": 2048,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.githubcopilot.com/chat/completions",
                headers=headers, json=body
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(500, f"LLM 呼叫失敗：{e}")


@app.post("/regen_report")
async def regen_report(payload: dict):
    """Re-generate grading report incorporating chat discussion."""
    import httpx
    original_report = payload.get("original_report", "")
    history         = payload.get("history", [])  # [{role, content}, ...]

    if not original_report:
        raise HTTPException(400, "original_report required")
    if not history:
        raise HTTPException(400, "history required")

    token = _load_copilot_token()
    if not token:
        raise HTTPException(503, "GitHub Copilot token not available")

    # Build conversation summary for context
    chat_summary = "\n".join(
        f"{'【評分者】' if h['role']=='assistant' else '【老師】'}{h['content']}"
        for h in history[-20:]  # last 20 turns
        if h.get("content")
    )

    regen_prompt = (
        "你是一位專業的作業評分助理。以下是原始的 AI 評分報告，以及評分者與老師之間的討論對話。\n"
        "請根據討論內容，重新產製一份修正版的評分報告。\n"
        "要求：\n"
        "1. 保留原始報告的結構和格式（Markdown）\n"
        "2. 根據討論中提到的修正意見調整分數和評語\n"
        "3. 在報告開頭加上「**📝 修正版（依討論更新）**」標記\n"
        "4. 若某題有爭議，請在評語中說明修正原因\n"
        "5. 回應語言使用繁體中文\n\n"
        f"## 原始評分報告\n\n{original_report}\n\n"
        f"## 討論對話記錄\n\n{chat_summary}\n\n"
        "請現在產製修正版評分報告："
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.95.0",
    }
    regen_system = (
        "你是一位嚴格、公正的作業評審。重新產製評分報告時，只能根據老師明確指出的修正意見調整分數，"
        "其餘項目必須維持原始評分標準，不得自行加分或寬鬆評分。"
        "任何分數調整都必須在評語中清楚說明修正原因。回應請使用繁體中文。"
    )
    body = {
        "model": "claude-sonnet-4.6",
        "messages": [
            {"role": "system", "content": regen_system},
            {"role": "user", "content": regen_prompt}
        ],
        "max_tokens": 4096,
    }
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                "https://api.githubcopilot.com/chat/completions",
                headers=headers, json=body
            )
            resp.raise_for_status()
            data = resp.json()
            report = data["choices"][0]["message"]["content"]
        return {"report": report}
    except Exception as e:
        return {"error": str(e)}

# ═══════════════════════════════════════════════════════════════════════════════
#  Writer Portal — 寫手蝦投稿頁（獨立密碼）
# ═══════════════════════════════════════════════════════════════════════════════

_writer_sessions: set = set()

def _auth_writer(token: Optional[str]) -> bool:
    return token is not None and token in _writer_sessions

def _writer_base_html(body: str, title="投稿平台") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}}
  .topbar{{background:#1a1d2e;border-bottom:1px solid #2d3154;padding:12px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
  .topbar h1{{font-size:1.1rem;font-weight:600;color:#34d399}}
  .topbar a{{color:#94a3b8;font-size:.85rem;text-decoration:none;min-height:44px;display:inline-flex;align-items:center}}
  .container{{max-width:760px;margin:32px auto;padding:0 16px}}
  .card{{background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;padding:24px;margin-bottom:24px}}
  .card h2{{font-size:1rem;font-weight:600;color:#34d399;margin-bottom:18px}}
  label{{font-size:.9rem;color:#94a3b8;display:block;margin-bottom:6px}}
  input[type=text],input[type=password],textarea{{
    width:100%;padding:12px 14px;background:#0f1117;border:1px solid #2d3154;
    border-radius:8px;color:#e2e8f0;font-size:16px;outline:none;transition:.2s
  }}
  input:focus,textarea:focus{{border-color:#34d399}}
  .btn{{display:inline-flex;align-items:center;justify-content:center;padding:12px 22px;background:#059669;color:#fff;
    border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:500;transition:.2s;min-height:44px}}
  .btn:hover{{background:#047857}}
  .alert{{padding:12px 16px;border-radius:8px;font-size:.85rem;margin-bottom:16px}}
  .alert-error{{background:#450a0a;border:1px solid #7f1d1d;color:#fca5a5}}
  .alert-success{{background:#052e16;border:1px solid #14532d;color:#86efac}}
  .hint{{font-size:.8rem;color:#4b5563;margin-top:6px}}
  .filed-list{{list-style:none}}
  .filed-list li{{border-bottom:1px solid #1e2235;padding:10px 0;font-size:.87rem;color:#94a3b8}}
  .filed-list li:last-child{{border-bottom:none}}
</style>
</head>
<body>
<div class="topbar">
  <h1>✍️ 寫手投稿平台</h1>
  <a href="/writer/logout" style="margin-left:auto">登出</a>
</div>
<div class="container">{body}</div>
</body>
</html>"""

@app.post("/writer/api/submit")
async def writer_api_submit(request: Request):
    """API endpoint for writer skill to submit articles directly."""
    x_token = request.headers.get("X-Writer-Token", "")
    if not secrets.compare_digest(x_token, WRITER_PASSWORD):
        raise HTTPException(401, "Invalid writer token")
    payload = await request.json()
    title   = (payload.get("title") or "").strip()
    author  = (payload.get("author") or "").strip()
    content = (payload.get("content") or "").strip()
    note    = (payload.get("note") or "").strip()
    if not title or not author or not content:
        raise HTTPException(400, "title, author, content are required")

    article_id = str(uuid.uuid4())
    slug = re.sub(r"[^\w\-]", "-", title.lower())[:60]
    filename = f"{slug}.md"
    save_path = ARTICLES_DIR / f"{article_id}.md"
    save_path.write_text(content, encoding="utf-8")
    _articlemeta.append({
        "id": article_id,
        "title": title,
        "author": author,
        "note": note,
        "filename": filename,
        "size": len(content.encode()),
        "uploaded_at": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
    })
    _save_articlemeta()
    return {"status": "ok", "article_id": article_id, "title": title}



@app.get("/writer/login", response_class=HTMLResponse)
def writer_login_page(error: str = ""):
    err = '<div class="alert alert-error">密碼錯誤，請再試一次。</div>' if error else ""
    body = f"""
<div style="max-width:400px;margin:80px auto">
<div class="card">
  <h2>🔐 寫手登入</h2>{err}
  <form method="post" action="/writer/login">
    <label>投稿密碼</label>
    <input type="password" name="password" autofocus style="margin-bottom:16px">
    <button class="btn" type="submit" style="width:100%">登入</button>
  </form>
</div></div>"""
    return HTMLResponse(_writer_base_html(body, "登入 — 寫手投稿"))

@app.post("/writer/login")
def writer_login(response: Response, password: str = Form(...)):
    if not secrets.compare_digest(password, WRITER_PASSWORD):
        return RedirectResponse("/writer/login?error=1", status_code=303)
    token = secrets.token_urlsafe(32)
    _writer_sessions.add(token)
    resp = RedirectResponse("/writer", status_code=303)
    resp.set_cookie("writer_token", token, httponly=True, samesite="lax", max_age=86400*7)
    return resp

@app.get("/writer/logout")
def writer_logout(writer_token: Optional[str] = Cookie(None)):
    _writer_sessions.discard(writer_token)
    resp = RedirectResponse("/writer/login", status_code=303)
    resp.delete_cookie("writer_token")
    return resp

@app.get("/writer", response_class=HTMLResponse)
def writer_portal(writer_token: Optional[str] = Cookie(None), msg: str = ""):
    if not _auth_writer(writer_token):
        return RedirectResponse("/writer/login")

    ok_html = f'<div class="alert alert-success">✅ {msg}</div>' if msg else ""

    # List this writer's recent uploads (last 10)
    recent = list(reversed(_articlemeta))[:10]

    # Build article cards with unread indicator
    cards = ""
    for a in recent:
        size_kb = a.get("size", 0) // 1024
        msgs = _article_messages.get(a["id"], [])
        owner_msgs = [m for m in msgs if m["role"] == "owner"]
        badge = f'<span style="background:#7c3aed;color:#fff;border-radius:99px;padding:2px 8px;font-size:.72rem;margin-left:6px">💬 {len(owner_msgs)} 則回饋</span>' if owner_msgs else ""
        revised = f'<div style="font-size:.75rem;color:#34d399;margin-top:2px">✅ 已修改：{a["revised_at"]}</div>' if a.get("revised_at") else ""
        cards += f"""<div style="border:1px solid #2d3154;border-radius:10px;padding:14px 16px;margin-bottom:10px;background:#0f1117">
  <a href="/writer/article/{a['id']}" style="color:#34d399;font-size:1rem;font-weight:500;text-decoration:none;display:block;margin-bottom:4px;word-break:break-word">
    {a['title']} {badge}
  </a>
  <div style="font-size:.78rem;color:#64748b">✍️ {a.get('author','—')} &nbsp;·&nbsp; {a.get('uploaded_at','')} &nbsp;·&nbsp; {size_kb} KB</div>
  {revised}
</div>"""

    if not cards:
        cards = '<div class="empty">尚無投稿記錄</div>'

    body = f"""
{ok_html}
<div class="card">
  <h2>📤 投稿文章</h2>
  <p style="font-size:.85rem;color:#64748b;margin-bottom:18px">請上傳 Markdown (.md) 格式的文章，確認內容完整後再送出。</p>
  <form method="post" action="/writer/submit" enctype="multipart/form-data">
    <div style="margin-bottom:14px">
      <label>文章標題</label>
      <input type="text" name="title" placeholder="文章標題" required>
    </div>
    <div style="margin-bottom:14px">
      <label>作者名稱</label>
      <input type="text" name="author" placeholder="你的名字或筆名" required>
    </div>
    <div style="margin-bottom:14px">
      <label>上傳 .md 檔案</label>
      <input type="file" name="file" accept=".md,.txt" required>
      <div class="hint">僅接受 .md / .txt 格式，建議使用 Markdown 撰寫</div>
    </div>
    <div style="margin-bottom:18px">
      <label>備註（選填）</label>
      <textarea name="note" rows="2" placeholder="給編輯的備註，例如：這篇是系列第二篇…"></textarea>
    </div>
    <button class="btn" type="submit">📨 送出投稿</button>
  </form>
</div>

<div class="card">
  <h2>📋 我的文章</h2>
  <p style="font-size:.82rem;color:#64748b;margin-bottom:14px">點擊文章可查看景揚的回饋並回覆、提交修改版</p>
  {cards}
</div>
"""
    return HTMLResponse(_writer_base_html(body))


@app.get("/writer/article/{article_id}", response_class=HTMLResponse)
def writer_article_view(article_id: str, writer_token: Optional[str] = Cookie(None), msg: str = ""):
    """Writer views an article, sees owner feedback, can reply and submit revision."""
    if not _auth_writer(writer_token):
        return RedirectResponse("/writer/login")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404)

    ok_html = f'<div class="alert alert-success">✅ {msg}</div>' if msg else ""
    content = Path(am["path"]).read_text(encoding="utf-8") if Path(am["path"]).exists() else "（檔案遺失）"
    content_escaped = json.dumps(content)
    article_id_json = json.dumps(article_id)
    author_json = json.dumps(am.get("author", "寫手蝦"))

    body = f"""
{ok_html}
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:20px">
    <div style="min-width:0;flex:1">
      <h2 style="font-size:1.4rem;color:#e2e8f0;margin-bottom:6px;word-break:break-word">{am['title']}</h2>
      <div style="font-size:.85rem;color:#64748b">✍️ {am.get('author','—')} &nbsp;·&nbsp; {am.get('uploaded_at','')}</div>
      {version_select_html}
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;flex-shrink:0">
      <a href="/writer" class="btn btn-sm btn-ghost" style="text-decoration:none;flex-shrink:0">← 返回</a>
    </div>
  </div>
  <div id="rendered" style="line-height:1.8;color:#cbd5e1;font-size:.95rem;max-width:100%;overflow:hidden;word-break:break-word"></div>
</div>

<!-- ── 回饋對話視窗 ────────────────────────────────────────────────── -->
<div class="card">
  <h2>💬 景揚的回饋 & 你的回覆</h2>
  <div id="msg-list" style="
    background:#0a0a0f;border:1px solid #1e2235;border-radius:10px;
    padding:14px;min-height:80px;max-height:500px;overflow-y:auto;
    margin-bottom:14px;font-size:.87rem;line-height:1.65
  ">
    <div id="msg-loading" style="color:#4b5563;text-align:center;padding:16px">載入中…</div>
  </div>
  <div style="display:flex;gap:10px;align-items:flex-end">
    <textarea id="reply-input" rows="2" placeholder="輸入回覆，例如：收到！我會調整第二段的邏輯…"
      style="flex:1;resize:vertical;min-height:60px;font-family:inherit;font-size:.87rem"
      onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendReply();}}"
      oninput="document.getElementById('reply-btn').disabled = !this.value.trim()">
    </textarea>
    <button class="btn" id="reply-btn" onclick="sendReply()" style="background:#059669;white-space:nowrap" disabled>
      ➤ 回覆
    </button>
  </div>
  <div style="font-size:.75rem;color:#475569;margin-top:6px">Enter 送出 ｜ Shift+Enter 換行</div>
</div>

<!-- ── 提交修改版 ──────────────────────────────────────────────────── -->
<div class="card">
  <h2>📝 提交修改版</h2>
  <p style="font-size:.82rem;color:#64748b;margin-bottom:16px">
    根據回饋修改文章後，上傳新版本 .md 檔案，系統會自動更新並通知景揚。
  </p>
  <form method="post" action="/articles/{article_id}/revise" enctype="multipart/form-data">
    <input type="hidden" name="article_id" value="{article_id}">
    <div style="margin-bottom:14px">
      <label>修改後的 .md 檔案</label>
      <input type="file" name="file" accept=".md,.txt" required>
    </div>
    <div style="margin-bottom:16px">
      <label>修改說明</label>
      <textarea name="note" rows="2" placeholder="說明這次修改了什麼，例如：依照回饋修正第二、三段邏輯…" style="font-family:inherit"></textarea>
    </div>
    <button class="btn" type="submit" style="background:#059669">📨 送出修改版</button>
  </form>
</div>

<!-- 固定懸浮的重新載入按鈕 -->
<style>
  .floating-reload {{
    position: fixed;
    bottom: 30px;
    right: 30px;
    z-index: 100;
    background: #3b1f6e;
    color: #e2e8f0;
    border: 1px solid #6d28d9;
    border-radius: 50px;
    padding: 10px 16px;
    font-size: 0.9rem;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    transition: background 0.2s, transform 0.2s;
  }}
  .floating-reload:hover {{
    background: #4c1d95;
    transform: translateY(-2px);
  }}
  @media(max-width:900px){{
    .floating-reload {{ right: 20px; bottom: 20px; }}
  }}
</style>
<button onclick="window.location.reload()" class="floating-reload">🔄 重新載入</button>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<script>
mermaid.initialize({{ startOnLoad: false, theme: 'dark' }});

function renderMermaid() {{
  document.querySelectorAll('code.language-mermaid').forEach(function(code) {{
    var pre = code.parentElement;
    if (pre.tagName === 'PRE') {{
      var div = document.createElement('div');
      div.className = 'mermaid';
      div.textContent = code.textContent;
      pre.parentElement.replaceChild(div, pre);
    }}
  }});
  mermaid.init(undefined, document.querySelectorAll('.mermaid'));
}}
</script>
<script>
const md = {content_escaped};
const ARTICLE_ID = {article_id_json};
const AUTHOR = {author_json};
document.getElementById('rendered').innerHTML = marked.parse(md);
renderMermaid();
document.querySelectorAll('#rendered pre').forEach(p=>{{p.style.maxWidth='100%';p.style.overflowX='auto';}});

function escHtml(s){{return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}

function renderMessages(msgs){{
  const el = document.getElementById('msg-list');
  if(!msgs||msgs.length===0){{
    el.innerHTML='<div style="color:#4b5563;text-align:center;padding:16px">尚無訊息，等待景揚的回饋！</div>';
    return;
  }}
  el.innerHTML = msgs.map(m => {{
    const isOwner = m.role === 'owner';
    const align = isOwner ? 'flex-start' : 'flex-end';
    const bg = isOwner ? '#3b1f6e' : '#1a2e3e';
    const border = isOwner ? '#6d28d9' : '#1e4060';
    const label = isOwner ? '👑 景揚' : ('✍️ ' + escHtml(m.from||'寫手蝦'));
    return `<div style="display:flex;flex-direction:column;align-items:${{align}};margin-bottom:12px">
      <div style="font-size:.72rem;color:#4b5563;margin-bottom:4px">${{label}} &nbsp;·&nbsp; ${{escHtml(m.timestamp||'')}}</div>
      <div style="max-width:85%;padding:10px 14px;border-radius:12px;background:${{bg}};border:1px solid ${{border}};color:#cbd5e1;word-break:break-word">
        ${{marked.parse(m.content||'')}}
      </div>
    </div>`;
  }}).join('');
  el.scrollTop = el.scrollHeight;
}}

async function loadMessages(){{
  try{{
    const r = await fetch('/articles/' + ARTICLE_ID + '/messages', {{credentials:'include'}});
    const d = await r.json();
    renderMessages(d.messages||[]);
  }}catch(e){{
    document.getElementById('msg-list').innerHTML='<div style="color:#fca5a5">載入失敗</div>';
  }}
}}

async function sendReply(){{
  if(window._replySending) return;
  window._replySending = true;
  const input = document.getElementById('reply-input');
  const text = input.value.trim();
  if(!text){{ window._replySending=false; return; }}
  document.getElementById('reply-btn').disabled = true;
  input.value = '';
  try{{
    const r = await fetch('/articles/' + ARTICLE_ID + '/messages', {{credentials:'include',
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{role:'writer', from:AUTHOR, content:text}})
    }});
    const d = await r.json();
    renderMessages(d.messages||[]);
  }}catch(e){{
    input.value = text;
    alert('送出失敗：'+String(e));
  }}
  document.getElementById('reply-btn').disabled = !input.value.trim();
  document.getElementById('reply-input').focus();
  window._replySending = false;
}}

loadMessages();
setInterval(loadMessages, 5000);
</script>
<style>
  #rendered h1,#rendered h2,#rendered h3{{color:#34d399;margin:1.2em 0 .5em}}
  #rendered p{{margin-bottom:.9em}}
  #rendered code{{background:#0f1117;padding:2px 6px;border-radius:4px;font-size:.85em;color:#86efac;word-break:break-all}}
  #rendered pre{{background:#0f1117;border:1px solid #2d3154;border-radius:8px;padding:14px;overflow-x:auto;margin-bottom:1em}}
  #rendered blockquote{{border-left:3px solid #34d399;padding-left:14px;color:#94a3b8;margin-bottom:1em}}
  #rendered a{{color:#60a5fa;word-break:break-all}}
  #rendered ul,#rendered ol{{padding-left:1.5em;margin-bottom:.9em}}
  #rendered img{{max-width:100%;height:auto;border-radius:8px;display:block}}
</style>
"""
    return HTMLResponse(_writer_base_html(body, f"{am['title']} — 回饋"))

@app.post("/writer/submit")
async def writer_submit(
    writer_token: Optional[str] = Cookie(None),
    file: UploadFile = File(...),
    title: str = Form(...),
    author: str = Form(...),
    note: str = Form(""),
):
    if not _auth_writer(writer_token):
        return RedirectResponse("/writer/login")

    raw = await file.read()
    text = raw.decode("utf-8", errors="replace")

    article_id = str(uuid.uuid4())
    slug = re.sub(r"[^\w\-]", "-", title.lower())[:60]
    filename = f"{slug}.md"
    save_path = ARTICLES_DIR / f"{article_id}.md"
    save_path.write_text(text, encoding="utf-8")

    _articlemeta.append({
        "id": article_id,
        "title": title.strip(),
        "author": author.strip(),
        "note": note.strip(),
        "filename": filename,
        "size": len(raw),
        "uploaded_at": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
    })
    _save_articlemeta()

    return RedirectResponse(
        f"/writer?msg=投稿成功！《{title}》已送出，感謝你的貢獻 🎉",
        status_code=303
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Articles — 文章庫（Owner 閱讀 / 下載）
# ═══════════════════════════════════════════════════════════════════════════════


@app.post("/articles/{article_id}/share")
async def share_article(article_id: str, request: Request, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        raise HTTPException(401, "Unauthorized")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404, "Article not found")
        
    data = {"password": ""}
    # try to read json
    try:
        body = await request.json()



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
        
    return HTMLResponse(_share_html(f"""
<div style="max-width:400px;margin:100px auto;text-align:center;padding:24px;background:#1e2235;border-radius:12px;border:1px solid #2d3154;">
    <h2 style="color:#a78bfa;margin-bottom:16px;">《{am['title']}》</h2>
    <p style="color:#94a3b8;margin-bottom:24px;">這是一篇加密分享的文章，請輸入密碼以繼續閱讀。</p>
    <form method="post" action="/shared/{share_token}">
        <input type="password" name="password" placeholder="請輸入密碼" required
               style="width:100%;padding:10px;margin-bottom:16px;background:#0f1117;border:1px solid #3b1f6e;color:#e2e8f0;border-radius:6px;">
        <button type="submit" class="btn btn-primary" style="width:100%;padding:10px;">解鎖閱讀</button>
    </form>
</div>
""", "文章密碼保護"))

@app.post("/shared/{share_token}", response_class=HTMLResponse)
async def shared_article_post(share_token: str, request: Request):
    am = next((a for a in _articlemeta if a.get("share_token") == share_token), None)
    if not am:
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)
        
    form = await request.form()
    password = form.get("password", "")
    
    if am.get("share_password") and password != am.get("share_password"):
        return HTMLResponse(_share_html(f"""
<div style="max-width:400px;margin:100px auto;text-align:center;padding:24px;background:#1e2235;border-radius:12px;border:1px solid #2d3154;">
    <h2 style="color:#f87171;margin-bottom:16px;">密碼錯誤</h2>
    <p style="color:#94a3b8;margin-bottom:24px;">您輸入的密碼不正確，請重新嘗試。</p>
    <a href="/shared/{share_token}" class="btn btn-primary">返回重試</a>
</div>
""", "密碼錯誤"))

    # Render article
    path = Path(am["path"])
    content = path.read_text(encoding="utf-8") if path.exists() else "（檔案遺失）"
    content_escaped = json.dumps(content)
    
    body = f"""
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
  document.getElementById('rendered').innerHTML = marked.parse({content_escaped});
</script>
"""
    return HTMLResponse(_share_html(body, am['title']))

@app.get("/articles", response_class=HTMLResponse)
def articles_list(rag_token: Optional[str] = Cookie(None), msg: str = ""):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    ok_html = f'<div class="alert alert-success">✅ {msg}</div>' if msg else ""
    total = len(_articlemeta)

    if _articlemeta:
        rows = ""
        cards = ""
        for a in reversed(_articlemeta):
            size_kb = a.get("size", 0) // 1024
            # Desktop table row
            rows += f"""<tr>
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
</tr>"""
            # Mobile card
            cards += f"""<div class="article-card">
  <a href="/articles/{a['id']}" class="article-card-title">{a['title']}</a>
  <div class="article-card-meta">✍️ {a.get('author','—')} &nbsp;·&nbsp; {a.get('uploaded_at','')} &nbsp;·&nbsp; {size_kb} KB</div>
  <div class="article-card-actions">
    <button onclick="shareArticle('{a['id']}')" class="btn btn-sm btn-ghost">🔗 分享</button>
    <a href="/articles/{a['id']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ .md</a>
    <form method="post" action="/articles/{a['id']}/delete"
          onsubmit="return confirm('確定刪除？')">
      <button class="btn btn-sm btn-danger">🗑 刪除</button>
    </form>
  </div>
</div>"""
        table = f"""
<style>
  .article-card{{border:1px solid #2d3154;border-radius:10px;padding:14px 16px;margin-bottom:10px;background:#0f1117}}
  .article-card-title{{color:#a78bfa;font-size:1rem;font-weight:500;text-decoration:none;display:block;margin-bottom:6px;word-break:break-word}}
  .article-card-meta{{font-size:.78rem;color:#64748b;margin-bottom:10px}}
  .article-card-actions{{display:flex;gap:8px;flex-wrap:wrap}}
  .article-cards{{display:none}}
  .article-table{{display:block}}
  @media(max-width:640px){{
    .article-cards{{display:block}}
    .article-table{{display:none}}
  }}
</style>
<div class="article-cards">{cards}</div>
<div class="article-table">
<div class="table-wrap"><table>
<thead><tr>
  <th>標題</th><th>作者</th><th>投稿時間</th><th>大小</th><th>操作</th>
</tr></thead>
<tbody>{rows}</tbody>
</table></div>
</div>"""
    else:
        table = '<div class="empty">尚無文章，請請寫手蝦投稿 😊</div>'

    body = f"""
{ok_html}
<div class="stats" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px">
  <div class="stat"><span class="stat-val">{total}</span> 篇文章</div>
</div>
<div class="card">
  <h2>📰 文章庫</h2>
  <p style="font-size:.82rem;color:#64748b;margin-bottom:16px">
    寫手投稿頁：<a href="/writer/login" style="color:#34d399" target="_blank">/writer/login</a>
    （可分享給寫手蝦，密碼另行告知）
  </p>
  {table}
</div>
"""
    body += """
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
    return HTMLResponse(_base_html(body, "文章庫 — RAG KB"))

@app.get("/articles/{article_id}", response_class=HTMLResponse)
def article_view(article_id: str, v: Optional[str] = None, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404)

    versions = am.get("versions", [])
    if not versions:
        am["versions"] = [{"id": "v1", "timestamp": am.get("uploaded_at"), "path": am.get("path")}]
        versions = am["versions"]
        
    path = Path(am["path"])
    selected_ver = next((x for x in versions if x["id"] == v), None) if v else None
    if selected_ver:
        path = Path(selected_ver["path"])

    content = path.read_text(encoding="utf-8") if path.exists() else "（檔案遺失）"



    version_select_html = ""
    if len(versions) > 1:
        options = []
        for ver in reversed(versions):
            selected = "selected" if (v == ver["id"]) or (not v and ver["path"] == am["path"]) else ""
            label = f"版本 {ver['id']} ({ver['timestamp']})"
            options.append(f'<option value="{ver["id"]}" {selected}>{label}</option>')
        
        prefix = "/writer/article" if "msg" in locals() else "/articles"
        version_select_html = f'''
        <div style="margin-top:8px">
          <select onchange="window.location.href='{prefix}/{article_id}?v=' + this.value" 
                  style="background:#1e2235;color:#e2e8f0;border:1px solid #2d3154;padding:4px 8px;border-radius:4px;font-size:0.85rem;cursor:pointer">
            {''.join(options)}
          </select>
        </div>
        '''

    # Escape for JS string
    content_escaped = json.dumps(content)
    article_id_json = json.dumps(article_id)

    body = f"""
<style>
  /* ── 三欄式文章頁版面覆蓋 ─────────────────────────────────────────── */
  .container{{max-width:100%!important;padding:0!important;margin:0!important}}
  .layout-flex{{gap:0!important;padding:0!important}}
  .content-wrap{{padding:0!important}}
  /* 文章主體寬度自動調整（左欄 216px + 右欄 334px + 各 8px 間距） */
  #art-wrap{{padding:24px 342px 40px 224px}}
  /* 左側目錄面板 */
  #toc-panel{{
    position:fixed;left:0;top:54px;
    width:216px;height:calc(100vh - 54px);
    overflow-y:auto;
    background:#12152a;border-right:1px solid #2d3154;
    padding:16px 10px;z-index:50;
  }}
  #toc-list a{{
    display:block;color:#94a3b8;text-decoration:none;
    font-size:.76rem;border-radius:4px;margin-bottom:3px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    transition:background .15s,color .15s;
  }}
  #toc-list a:hover{{background:#2d3154;color:#e2e8f0}}
  #toc-list a.toc-active{{background:#3b1f6e;color:#c4b5fd}}
  /* 右側對話面板 */
  #chat-panel{{
    position:fixed;right:0;top:54px;
    width:334px;height:calc(100vh - 54px);
    background:#12152a;border-left:1px solid #2d3154;
    display:flex;flex-direction:column;z-index:50;
  }}
  /* 文章樣式 */
  #rendered{{max-width:100%;overflow-x:hidden;word-break:break-word;overflow-wrap:break-word}}
  #rendered h1,#rendered h2,#rendered h3{{color:#a78bfa;margin:1.2em 0 .5em;word-break:break-word}}
  #rendered p{{margin-bottom:.9em}}
  #rendered code{{background:#0f1117;padding:2px 6px;border-radius:4px;font-size:.85em;color:#86efac;word-break:break-all}}
  #rendered pre{{background:#0f1117;border:1px solid #2d3154;border-radius:8px;padding:14px;overflow-x:auto;margin-bottom:1em;max-width:100%}}
  #rendered pre code{{background:none;padding:0;word-break:normal}}
  #rendered blockquote{{border-left:3px solid #a78bfa;padding-left:14px;color:#94a3b8;margin-bottom:1em}}
  #rendered a{{color:#60a5fa;word-break:break-all}}
  #rendered ul,#rendered ol{{padding-left:1.5em;margin-bottom:.9em}}
  #rendered img{{max-width:100%;height:auto;border-radius:8px;display:block}}
  #rendered hr{{border:none;border-top:1px solid #2d3154;margin:1.5em 0}}
  #rendered table{{width:100%;border-collapse:collapse;font-size:.85rem;display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}}
  #rendered th{{text-align:left;padding:8px 10px;background:#0f1117;color:#64748b;border-bottom:1px solid #2d3154}}
  #rendered td{{padding:8px 10px;border-bottom:1px solid #1e2235;vertical-align:top}}
  .card{{overflow:hidden}}
  /* ── 手機版（<900px）退回單欄 ── */
  @media(max-width:900px){{
    #toc-panel{{display:none}}
    #chat-panel{{position:static;width:100%;height:auto;border-left:none;border-top:1px solid #2d3154;margin-top:16px}}
    #art-wrap{{padding:16px}}
  }}
</style>

<!-- ① 左側固定目錄導覽 -->
<div id="toc-panel">
  <div style="font-size:.7rem;color:#64748b;font-weight:700;margin-bottom:12px;letter-spacing:.07em;text-transform:uppercase">📑 目錄</div>
  <div id="toc-list"><div style="color:#4b5563;font-size:.74rem;padding:4px 8px">解析中…</div></div>
</div>

<!-- ② 右側固定對話面板 -->
<div id="chat-panel">
  <!-- 標題列（點擊展開/收合） -->
  <div onclick="toggleChat()" style="padding:12px 16px;border-bottom:1px solid #2d3154;display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none;flex-shrink:0;background:#1a1d2e">
    <span style="font-size:.9rem;font-weight:600;color:#a78bfa">🦐 與蝦蝦對話</span>
    <button id="chat-toggle-btn" style="background:none;border:none;color:#64748b;font-size:1rem;cursor:pointer;padding:0 4px;line-height:1" title="展開/收合">▼</button>
  </div>
  <!-- 對話主體 -->
  <div id="chat-body" style="flex:1;overflow:hidden;display:flex;flex-direction:column;padding:12px;min-height:0;background:#1a1d2e">
    <p style="font-size:.73rem;color:#64748b;margin-bottom:10px;flex-shrink:0;line-height:1.5">
      直接和蝦蝦對話，告訴她你的想法或修改需求；每篇文章對話獨立記錄。
    </p>
    <div id="msg-list" style="flex:1;overflow-y:auto;background:#0a0a0f;border:1px solid #1e2235;border-radius:8px;padding:10px;font-size:.82rem;line-height:1.6;margin-bottom:10px;min-height:80px">
      <div id="msg-loading" style="color:#4b5563;text-align:center;padding:16px">載入中…</div>
    </div>
    <div style="display:flex;gap:8px;align-items:flex-end;flex-shrink:0">
      <textarea id="fb-input" rows="2" placeholder="例如：第二段邏輯有點跳，可以加個過渡句…"
        style="flex:1;resize:none;min-height:56px;font-family:inherit;font-size:.83rem"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendFeedback();}}">
      </textarea>
      <button class="btn btn-sm" id="fb-send-btn" onclick="sendFeedback()" style="white-space:nowrap;padding:10px 14px">➤ 送出</button>
    </div>
    <div style="font-size:.7rem;color:#475569;margin-top:6px;flex-shrink:0">Enter 送出 ｜ Shift+Enter 換行</div>
  </div>
</div>

<!-- ③ 文章主體（左右 padding 讓固定面板不遮住內容） -->
<div id="art-wrap">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:20px">
      <div style="min-width:0;flex:1">
        <h2 style="font-size:1.4rem;color:#e2e8f0;margin-bottom:6px;word-break:break-word">{am['title']}</h2>
        <div style="font-size:.85rem;color:#64748b">✍️ {am.get('author','—')} &nbsp;·&nbsp; {am.get('uploaded_at','')}</div>
        {version_select_html}
        {f'<div style="font-size:.8rem;color:#4b5563;margin-top:4px">備註：{am["note"]}</div>' if am.get('note') else ''}
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;flex-shrink:0">
        <a href="/articles/{article_id}/download" class="btn btn-sm btn-ghost" style="text-decoration:none">⬇ 下載 .md</a>
        <a href="/articles" class="btn btn-sm btn-ghost" style="text-decoration:none">← 回列表</a>
      </div>
    </div>
    <div id="rendered" style="line-height:1.8;color:#cbd5e1;font-size:.95rem;max-width:100%;overflow:hidden;word-break:break-word;overflow-wrap:break-word"></div>
  </div>
</div>

<!-- 固定懸浮的重新載入按鈕 -->
<style>
  .floating-reload {{
    position: fixed;
    bottom: 30px;
    right: 354px; /* chat panel is 334px */
    z-index: 100;
    background: #3b1f6e;
    color: #e2e8f0;
    border: 1px solid #6d28d9;
    border-radius: 50px;
    padding: 10px 16px;
    font-size: 0.9rem;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    transition: background 0.2s, transform 0.2s;
  }}
  .floating-reload:hover {{
    background: #4c1d95;
    transform: translateY(-2px);
  }}
  @media(max-width:900px){{
    .floating-reload {{ right: 20px; bottom: 20px; }}
  }}
</style>
<button onclick="window.location.reload()" class="floating-reload">🔄 重新載入</button>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<script>
mermaid.initialize({{startOnLoad:false,theme:'dark'}});
function renderMermaid(){{
  document.querySelectorAll('code.language-mermaid').forEach(function(code){{
    var pre=code.parentElement;
    if(pre.tagName==='PRE'){{
      var div=document.createElement('div');div.className='mermaid';div.textContent=code.textContent;
      pre.parentElement.replaceChild(div,pre);
    }}
  }});
  mermaid.init(undefined,document.querySelectorAll('.mermaid'));
}}
</script>
<script>
const md = {content_escaped};
const ARTICLE_ID = {article_id_json};

// ── 渲染 Markdown ──────────────────────────────────────────────────────────
document.getElementById('rendered').innerHTML = marked.parse(md);
renderMermaid();
document.querySelectorAll('#rendered table').forEach(function(t){{
  if(!t.parentElement.classList.contains('table-wrap')){{
    var w=document.createElement('div');w.className='table-wrap';w.style.overflowX='auto';
    t.parentNode.insertBefore(w,t);w.appendChild(t);
  }}
}});
document.querySelectorAll('#rendered pre').forEach(function(p){{
  p.style.maxWidth='100%';p.style.overflowX='auto';p.style.boxSizing='border-box';
}});

// ── 目錄導覽 ───────────────────────────────────────────────────────────────
function buildTOC(){{
  const headings=document.querySelectorAll('#rendered h1,#rendered h2,#rendered h3');
  const tocList=document.getElementById('toc-list');
  if(!headings.length){{
    tocList.innerHTML='<div style="color:#4b5563;font-size:.74rem;padding:4px 8px">（無章節標題）</div>';
    return;
  }}
  tocList.innerHTML='';
  headings.forEach(function(h,i){{
    const id='toc-h-'+i;
    h.id=id;
    const level=parseInt(h.tagName[1]);
    const a=document.createElement('a');
    a.href='#'+id;
    a.title=h.textContent;
    a.textContent=h.textContent;
    a.style.paddingLeft=((level-1)*12+8)+'px';
    a.style.paddingTop='4px';
    a.style.paddingBottom='4px';
    a.style.paddingRight='8px';
    a.addEventListener('click',function(e){{
      e.preventDefault();
      document.getElementById(id).scrollIntoView({{behavior:'smooth',block:'start'}});
    }});
    tocList.appendChild(a);
  }});
  updateTOCActive();
}}

function updateTOCActive(){{
  const headings=Array.from(document.querySelectorAll('#rendered h1,#rendered h2,#rendered h3'));
  const links=Array.from(document.querySelectorAll('#toc-list a'));
  if(!headings.length)return;
  let activeIdx=0;
  headings.forEach(function(h,i){{if(h.getBoundingClientRect().top<=80)activeIdx=i;}});
  links.forEach(function(a,i){{a.classList.toggle('toc-active',i===activeIdx);}});
}}

window.addEventListener('scroll',updateTOCActive,{{passive:true}});
buildTOC();

// ── 對話面板展開/收合 ──────────────────────────────────────────────────────
var _chatOpen=true;
function toggleChat(){{
  _chatOpen=!_chatOpen;
  document.getElementById('chat-body').style.display=_chatOpen?'flex':'none';
  document.getElementById('chat-toggle-btn').textContent=_chatOpen?'▼':'▲';
}}

// ── 對話功能 ───────────────────────────────────────────────────────────────
function escHtml(s){{return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}

function renderMessages(msgs){{
  const el=document.getElementById('msg-list');
  if(!msgs||msgs.length===0){{
    el.innerHTML='<div style="color:#4b5563;text-align:center;padding:16px">開始和蝦蝦對話吧！👆 在下方輸入你想說的話…</div>';
    return;
  }}
  el.innerHTML=msgs.map(m=>{{
    const isOwner=m.role==='owner';
    const align=isOwner?'flex-end':'flex-start';
    const bg=isOwner?'#3b1f6e':'#1a2e3e';
    const border=isOwner?'#6d28d9':'#1e4060';
    const label=isOwner?'👑 景揚':('🦐 '+escHtml(m.from||'蝦蝦'));
    return `<div style="display:flex;flex-direction:column;align-items:${{align}};margin-bottom:12px">
      <div style="font-size:.7rem;color:#4b5563;margin-bottom:3px">${{label}} · ${{escHtml(m.timestamp||'')}}</div>
      <div style="max-width:90%;padding:8px 12px;border-radius:10px;background:${{bg}};border:1px solid ${{border}};color:#cbd5e1;word-break:break-word;font-size:.82rem">
        ${{marked.parse(m.content||'')}}
      </div>
    </div>`;
  }}).join('');
  el.scrollTop=el.scrollHeight;
}}

function showThinking(){{
  hideThinking();
  const el=document.getElementById('msg-list');
  const div=document.createElement('div');
  div.id='thinking-indicator';
  div.style.cssText='display:flex;flex-direction:column;align-items:flex-start;margin-bottom:12px';
  div.innerHTML=
    '<div style="font-size:.7rem;color:#4b5563;margin-bottom:3px">🦞 龍蝦</div>'
    +'<div style="padding:8px 14px;border-radius:10px;background:#1a2e3e;border:1px solid #1e4060;'
    +'color:#64748b;font-size:.82rem;display:flex;align-items:center;gap:8px">'
    +'<span style="display:inline-block;width:14px;height:14px;border:2px solid #2d3154;'
    +'border-top-color:#a78bfa;border-radius:50%;animation:spin 0.8s linear infinite;flex-shrink:0"></span>'
    +'🦞 龍蝦正在思考中<span id="thinking-dot"></span>'
    +'</div>';
  el.appendChild(div);
  el.scrollTop=el.scrollHeight;
  // 動態省略號
  let n=0;
  window._thinkingDotTimer=setInterval(()=>{{
    n=(n%3)+1;
    const d=document.getElementById('thinking-dot');
    if(d)d.textContent='.'.repeat(n);
  }},500);
}}

function hideThinking(){{
  if(window._thinkingDotTimer){{clearInterval(window._thinkingDotTimer);window._thinkingDotTimer=null;}}
  const t=document.getElementById('thinking-indicator');if(t)t.remove();
}}

async function fetchMessages(){{
  try{{
    const r=await fetch('/articles/'+ARTICLE_ID+'/messages',{{credentials:'include'}});
    const d=await r.json();return d.messages||[];
  }}catch(e){{return null;}}
}}

async function loadMessages(){{
  if(document.getElementById('thinking-indicator'))return;
  try{{
    const r=await fetch('/articles/'+ARTICLE_ID+'/messages',{{credentials:'include'}});
    const d=await r.json();renderMessages(d.messages||[]);
  }}catch(e){{
    document.getElementById('msg-list').innerHTML='<div style="color:#fca5a5">載入失敗：'+escHtml(String(e))+'</div>';
  }}
}}

async function sendFeedback(){{
  if(window._fbSending)return;
  window._fbSending=true;
  const input=document.getElementById('fb-input');
  const text=input.value.trim();
  if(!text){{window._fbSending=false;return;}}
  document.getElementById('fb-send-btn').disabled=true;
  input.value='';
  try{{
    const r=await fetch('/articles/'+ARTICLE_ID+'/messages',{{credentials:'include',
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{role:'owner',content:text}})
    }});
    const d=await r.json();
    const countAfterSend=(d.messages||[]).length;
    renderMessages(d.messages||[]);
    showThinking();
    // 每 3 秒輪詢一次，最多等 6 分鐘（120 次），足夠讓 LLM 慢慢產出
    let polls=0;
    window._rapidPollTimer=setInterval(()=>{{
      polls++;
      fetchMessages().then(msgs=>{{
        if(msgs&&msgs.length>countAfterSend){{
          clearInterval(window._rapidPollTimer);
          window._rapidPollTimer=null;
          hideThinking();
          renderMessages(msgs);
        }}else if(polls>=120){{
          clearInterval(window._rapidPollTimer);
          window._rapidPollTimer=null;
          hideThinking();
        }}
      }});
    }},3000);
  }}catch(e){{input.value=text;alert('送出失敗：'+String(e));}}
  document.getElementById('fb-send-btn').disabled=false;
  document.getElementById('fb-input').focus();
  window._fbSending=false;
}}

loadMessages();
setInterval(loadMessages,5000);
</script>
"""
    return HTMLResponse(_base_html(body, f"{am['title']} — 文章庫"))

@app.get("/articles/{article_id}/messages")
def get_article_messages(article_id: str, request: Request, rag_token: Optional[str] = Cookie(None), writer_token: Optional[str] = Cookie(None)):
    """Get messages for an article. Both owner and writer can read."""
    is_owner = _auth(rag_token)
    x_writer = request.headers.get("X-Writer-Token", "")
    is_writer = _auth_writer(writer_token) or (x_writer and secrets.compare_digest(x_writer, WRITER_PASSWORD))
    
    if not is_owner and not is_writer:
        raise HTTPException(401, "Not authenticated")
    msgs = _article_messages.get(article_id, [])
    return {"article_id": article_id, "messages": msgs}

@app.post("/articles/{article_id}/messages")
async def post_article_message(article_id: str, payload: dict, request: Request, rag_token: Optional[str] = Cookie(None), writer_token: Optional[str] = Cookie(None)):
    """Post a message. Owner uses rag_token, writer uses writer_token cookie or X-Writer-Token header."""
    is_owner  = _auth(rag_token)
    # 支援 header token 讓寫手蝦不需 cookie
    x_writer = request.headers.get("X-Writer-Token", "")
    is_writer = _auth_writer(writer_token) or (x_writer and secrets.compare_digest(x_writer, WRITER_PASSWORD))
    if not is_owner and not is_writer:
        raise HTTPException(401, "Not authenticated")

    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content is required")

    role = "owner" if is_owner else "writer"
    from_name = payload.get("from", "景揚" if is_owner else "寫手蝦")
    msg = {
        "role": role,
        "from": from_name,
        "content": content,
        "timestamp": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
    }
    if article_id not in _article_messages:
        _article_messages[article_id] = []
    _article_messages[article_id].append(msg)
    _save_article_messages()

    # Owner 留言 → 即時通知寫手蝦
    if is_owner:
        am = next((a for a in _articlemeta if a["id"] == article_id), None)
        if am:
            threading.Thread(
                target=_ask_writer_agent,
                args=(article_id, content),
                daemon=True,
            ).start()

    # 寫手蝦回覆 → 即時通知景揚
    if is_writer:
        am = next((a for a in _articlemeta if a["id"] == article_id), None)
        if am:
            threading.Thread(
                target=_notify_owner,
                args=(am["title"], article_id, from_name, content),
                daemon=True,
            ).start()

    return {"article_id": article_id, "messages": _article_messages[article_id]}

@app.post("/articles/{article_id}/revise")
async def writer_revise_article(article_id: str, request: Request, writer_token: Optional[str] = Cookie(None), file: UploadFile = File(...), note: str = Form("")):
    """Writer submits a revised version of an article."""
    x_writer = request.headers.get("X-Writer-Token", "")
    if not _auth_writer(writer_token) and not (x_writer and secrets.compare_digest(x_writer, WRITER_PASSWORD)):
        raise HTTPException(401, "Not authenticated")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404, "Article not found")

    raw = await file.read()
    text = raw.decode("utf-8", errors="replace")
    path = Path(am["path"])
    path.write_text(text, encoding="utf-8")

    # Update metadata
    am["size"] = len(raw)
    am["revised_at"] = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
    _save_articlemeta()

    # Append a system message about the revision
    if article_id not in _article_messages:
        _article_messages[article_id] = []
    revision_note = note.strip() or "（無備註）"
    _article_messages[article_id].append({
        "role": "writer",
        "from": am.get("author", "寫手蝦"),
        "content": f"📝 **已提交修改版本**\n\n修改說明：{revision_note}",
        "timestamp": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
    })
    _save_article_messages()
    # Notify 景揚 about the revision
    threading.Thread(
        target=_notify_owner,
        args=(am["title"], article_id, am.get("author", "寫手蝦"), f"📝 已提交修改版本\n修改說明：{revision_note}"),
        daemon=True,
    ).start()
    
    # Check if request accepts JSON, otherwise return HTML redirect
    accept = request.headers.get("Accept", "")
    if "application/json" in accept or x_writer:
        return {"status": "ok", "article_id": article_id}
    return RedirectResponse(f"/writer/article/{article_id}?msg=修改版已送出！", status_code=303)


@app.get("/articles/{article_id}/download")
def article_download(article_id: str, request: Request, rag_token: Optional[str] = Cookie(None), writer_token: Optional[str] = Cookie(None)):
    x_writer = request.headers.get("X-Writer-Token", "")
    is_writer = _auth_writer(writer_token) or (x_writer and secrets.compare_digest(x_writer, WRITER_PASSWORD))
    
    if not _auth(rag_token) and not is_writer:
        return RedirectResponse("/login")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404)
    path = Path(am["path"])
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(str(path), filename=am["filename"], media_type="text/markdown")

@app.post("/articles/{article_id}/delete")
def article_delete(article_id: str, rag_token: Optional[str] = Cookie(None)):
    global _articlemeta
    if not _auth(rag_token):
        return RedirectResponse("/login")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404)
    p = Path(am["path"])
    if p.exists():
        p.unlink()
    _articlemeta = [a for a in _articlemeta if a["id"] != article_id]
    _save_articlemeta()
    return RedirectResponse(f"/articles?msg=已刪除：{am['title']}", status_code=303)

# ═══════════════════════════════════════════════════════════════════════════════
#  System Status — 授權與模型額度狀態總覽
# ═══════════════════════════════════════════════════════════════════════════════

def _invoke_gateway_tool(tool: str, args: dict = {}) -> dict:
    """Call OpenClaw Gateway tools/invoke endpoint."""
    payload = {"tool": tool, "args": args}
    try:
        req = urllib.request.Request(
            f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


OPENCLAW_HOME = Path(os.path.expanduser("~/.openclaw"))


def _read_openclaw_config() -> dict:
    """Read openclaw.json config file directly."""
    try:
        cfg_path = OPENCLAW_HOME / "openclaw.json"
        return json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except Exception:
        return {}


def _read_cron_jobs() -> list:
    """Read cron jobs from ~/.openclaw/cron/jobs.json."""
    try:
        cron_path = OPENCLAW_HOME / "cron" / "jobs.json"
        if not cron_path.exists():
            return []
        data = json.loads(cron_path.read_text())
        jobs = data.get("jobs", [])
        result = []
        for j in jobs:
            state = j.get("state", {})
            result.append({
                "id": j.get("id", ""),
                "name": j.get("name", ""),
                "enabled": j.get("enabled", True),
                "schedule": j.get("schedule", {}),
                "agentId": j.get("agentId", "main"),
                "sessionTarget": j.get("sessionTarget", ""),
                "lastRunAtMs": state.get("lastRunAtMs"),
                "nextRunAtMs": state.get("nextRunAtMs"),
                "lastStatus": state.get("lastStatus", ""),
                "lastError": state.get("lastError", ""),
                "consecutiveErrors": state.get("consecutiveErrors", 0),
            })
        return result
    except Exception as e:
        return []


def _check_credential_status(provider: str) -> dict:
    """Check credential status from auth-profiles.json and github-copilot.token.json."""
    import time as _time

    auth_profiles_path = OPENCLAW_HOME / "agents" / "main" / "agent" / "auth-profiles.json"
    try:
        raw = json.loads(auth_profiles_path.read_text())
        profiles = raw.get("profiles", {})
    except Exception:
        profiles = {}

    # Find the profile matching this provider
    matched = None
    for _key, pval in profiles.items():
        if pval.get("provider", "") == provider:
            matched = pval
            break

    if not matched:
        return {"exists": False, "valid": False}

    # Extract token value
    token_val = matched.get("token") or matched.get("key") or ""
    exists = bool(token_val) and len(token_val) > 10
    masked = (token_val[:8] + "…" + token_val[-4:]) if len(token_val) > 12 else "***"

    if not exists:
        return {"exists": False, "valid": False}

    # GitHub Copilot: check expiry from dedicated token file
    if provider == "github-copilot":
        copilot_path = OPENCLAW_HOME / "credentials" / "github-copilot.token.json"
        try:
            cdata = json.loads(copilot_path.read_text())
            expires_ms = cdata.get("expiresAt", 0)
            expires_s = expires_ms / 1000
            valid = expires_s == 0 or expires_s > _time.time() + 60
            expires_str = (
                datetime.fromtimestamp(expires_s, TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
                if expires_s > 0 else "—"
            )
            return {"exists": True, "valid": valid, "masked": masked, "expiresAt": expires_str}
        except Exception:
            return {"exists": True, "valid": True, "masked": masked, "expiresAt": "—"}

    # Other providers: exists → valid
    return {"exists": True, "valid": True, "masked": masked}


def _collect_system_status() -> dict:
    """Collect all system status information."""
    now_ts = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")

    # 1. Session status (model, tokens, cache, version) via Gateway
    status_result = _invoke_gateway_tool("session_status", {})
    status_text = ""
    if status_result.get("ok"):
        r = status_result.get("result", {})
        details = r.get("details", {})
        status_text = details.get("statusText", "")

    # 2. Sessions list (active agents) via Gateway
    sessions_result = _invoke_gateway_tool("sessions_list", {"activeMinutes": 120, "limit": 50})
    sessions = []
    if sessions_result.get("ok"):
        r = sessions_result.get("result", {})
        d = r.get("details", {})
        raw_sessions = d.get("sessions", [])
        for s in raw_sessions:
            key = s.get("key", "")
            parts = key.split(":")
            agent_name = parts[1] if len(parts) > 1 else "unknown"
            sessions.append({
                "key": key,
                "agent": agent_name,
                "displayName": s.get("displayName", key),
                "label": s.get("label", ""),
                "model": s.get("model", "—"),
                "totalTokens": s.get("totalTokens", 0),
                "contextTokens": s.get("contextTokens", 0),
                "channel": s.get("channel", "—"),
                "updatedAt": s.get("updatedAt", 0),
                "sessionId": s.get("sessionId", ""),
            })

    # 3. Cron jobs (from file system)
    cron_jobs = _read_cron_jobs()

    # 4. Parse status_text for quick stats (moved up, needed for auth inference)
    quick_stats = {}
    if status_text:
        for line in status_text.split("\n"):
            if "Tokens:" in line:
                quick_stats["tokens"] = line.split("Tokens:")[-1].strip()
            elif "Cache:" in line:
                quick_stats["cache"] = line.split("Cache:")[-1].strip()
            elif "Context:" in line:
                quick_stats["context"] = line.split("Context:")[-1].strip()
            elif "Model:" in line:
                quick_stats["model"] = line.split("Model:")[-1].strip()
            elif "OpenClaw" in line:
                quick_stats["version"] = line.strip().lstrip("\U0001f99e").strip()
            elif "Session:" in line:
                quick_stats["session"] = line.split("Session:")[-1].strip()
            elif "Runtime:" in line:
                quick_stats["runtime"] = line.split("Runtime:")[-1].strip()
            elif "Queue:" in line:
                quick_stats["queue"] = line.split("Queue:")[-1].strip()

    # 5. OpenClaw config — auth profiles (from file system)
    cfg = _read_openclaw_config()
    auth_profiles = []
    profiles = cfg.get("auth", {}).get("profiles", {})
    # Infer anthropic validity from session_status success
    anthropic_valid_from_status = bool(quick_stats.get("model", "").startswith("anthropic/"))
    for profile_key, profile_val in profiles.items():
        provider = profile_val.get("provider", "")
        mode = profile_val.get("mode", "—")
        cred_status = _check_credential_status(provider)
        # For anthropic: if session_status used anthropic model, cred is valid
        if provider == "anthropic" and not cred_status.get("valid") and anthropic_valid_from_status:
            cred_status["valid"] = True
            cred_status["exists"] = True
            cred_status["masked"] = "sk-ant-***…*** (正在使用)"
        auth_profiles.append({
            "id": profile_key,
            "provider": provider,
            "mode": mode,
            "credExists": cred_status.get("exists", False),
            "credValid": cred_status.get("valid", False),
            "credMasked": cred_status.get("masked", "—"),
            "credExpires": cred_status.get("expiresAt", ""),
        })

    # 6. Services status (local systemd)
    services = []
    service_names = ["rag-kb", "rag-embed", "openclaw"]
    for svc in service_names:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=3
            )
            status_str = result.stdout.strip()
            services.append({"name": svc, "status": status_str, "ok": status_str == "active"})
        except Exception:
            services.append({"name": svc, "status": "unknown", "ok": False})

    # 6b. Docker services (banana-slides)
    docker_services = [
        {"key": "banana-slides", "containers": ["banana-slides-frontend", "banana-slides-backend"], "label": "banana-slides"},
    ]
    for dsvc in docker_services:
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name={dsvc['containers'][0]}", "--format", "{{.Status}}"],
                capture_output=True, text=True, timeout=5
            )
            running = bool(result.stdout.strip())
            services.append({"name": dsvc["label"], "status": "active" if running else "inactive", "ok": running, "docker": True, "key": dsvc["key"]})
        except Exception:
            services.append({"name": dsvc["label"], "status": "unknown", "ok": False, "docker": True, "key": dsvc["key"]})

    # 7. Model fallback list (from config)
    model_fallbacks = []
    agents_defaults = cfg.get("agents", {}).get("defaults", {})
    primary = agents_defaults.get("model", {}).get("primary", "")
    fallbacks = agents_defaults.get("model", {}).get("fallbacks", [])
    if primary:
        model_fallbacks.append({"model": primary, "role": "primary"})
    for fb in fallbacks:
        model_fallbacks.append({"model": fb, "role": "fallback"})

    # 8. LLM Proxy status + backends
    llm_proxy_ok = False
    llm_proxy_backends = {}
    try:
        req = urllib.request.Request("http://127.0.0.1:9000/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                llm_proxy_ok = True
                proxy_data = json.loads(resp.read())
                llm_proxy_backends = proxy_data.get("backends", {})
    except Exception:
        pass
    services.append({"name": "llm-proxy (port 9000)", "status": "active" if llm_proxy_ok else "inactive", "ok": llm_proxy_ok, "systemd": True, "key": "llm-proxy"})

    # city-game (PM2)
    city_game_status = "unknown"
    city_game_ok = False
    try:
        pm2_result = subprocess.run(
            ["/home/millalex921/.npm-global/bin/pm2", "jlist"],
            capture_output=True, text=True, timeout=5
        )
        if pm2_result.returncode == 0:
            pm2_list = json.loads(pm2_result.stdout)
            for proc in pm2_list:
                if proc.get("name") == "city-game":
                    pm2_status = proc.get("pm2_env", {}).get("status", "unknown")
                    city_game_status = pm2_status
                    city_game_ok = (pm2_status == "online")
                    break
            else:
                city_game_status = "not found"
    except Exception:
        city_game_status = "error"

    city_game_http = False
    try:
        req = urllib.request.Request("http://127.0.0.1:3003/", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            city_game_http = (resp.status == 200)
    except Exception:
        pass

    services.append({
        "name": "city-game (port 3003)",
        "status": city_game_status if city_game_ok else ("stopped" if city_game_status != "error" else "error"),
        "ok": city_game_ok and city_game_http,
        "extra": f"HTTP {'✅' if city_game_http else '❌'} | PM2 {city_game_status}",
        "pm2": True,
        "key": "city-game"
    })

    # 9. WhatsApp session status
    wa_dir = OPENCLAW_HOME / "credentials" / "whatsapp" / "default"
    wa_status = {"exists": False, "hasCreds": False, "fileCount": 0}
    if wa_dir.exists():
        wa_files = list(wa_dir.iterdir())
        wa_status = {
            "exists": True,
            "hasCreds": (wa_dir / "creds.json").exists(),
            "fileCount": len(wa_files),
        }

    # 10. Twilio config (from openclaw.json voice-call plugin)
    twilio_info = {}
    vc_plugin = cfg.get("plugins", {}).get("entries", {}).get("voice-call", {})
    if vc_plugin.get("enabled"):
        vc_cfg = vc_plugin.get("config", {})
        tw = vc_cfg.get("twilio", {})
        sid = tw.get("accountSid", "")
        twilio_info = {
            "enabled": True,
            "accountSidMasked": (sid[:6] + "…" + sid[-4:]) if len(sid) > 10 else sid,
            "fromNumber": vc_cfg.get("fromNumber", "—"),
            "webhookUrl": vc_cfg.get("publicUrl", "—"),
        }

    # 11. Agent model config (from agents.list)
    agent_configs = []
    for entry in cfg.get("agents", {}).get("list", []):
        agent_id = entry.get("id", "?")
        model = entry.get("model") or cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "—")
        agent_configs.append({
            "id": agent_id,
            "name": entry.get("name") or entry.get("identity", {}).get("name") or agent_id,
            "model": model,
        })

    # 12. Context window settings
    defaults_cfg = cfg.get("agents", {}).get("defaults", {})
    context_settings = {
        "pruningMode": defaults_cfg.get("contextPruning", {}).get("mode", "—"),
        "pruningTtl": defaults_cfg.get("contextPruning", {}).get("ttl", "—"),
        "compactionMode": defaults_cfg.get("compaction", {}).get("mode", "—"),
        "memoryFlush": defaults_cfg.get("compaction", {}).get("memoryFlush", {}).get("enabled", False),
        "subagentMaxConcurrent": defaults_cfg.get("subagents", {}).get("maxConcurrent", "—"),
        "subagentArchiveMin": defaults_cfg.get("subagents", {}).get("archiveAfterMinutes", "—"),
    }

    return {
        "collectedAt": now_ts,
        "statusText": status_text,
        "quickStats": quick_stats,
        "authProfiles": auth_profiles,
        "sessions": sessions,
        "cronJobs": cron_jobs,
        "services": services,
        "modelFallbacks": model_fallbacks,
        "llmProxyBackends": llm_proxy_backends,
        "whatsappStatus": wa_status,
        "twilioInfo": twilio_info,
        "agentConfigs": agent_configs,
        "contextSettings": context_settings,
    }


DOCKER_SERVICE_MAP = {
    "banana-slides": ["banana-slides-frontend", "banana-slides-backend"],
}

PM2_SERVICE_MAP = {
    "city-game": "city-game",
}

SYSTEMD_SERVICE_MAP = {
    "llm-proxy": "llm-proxy",
}

@app.post("/api/service_control")
async def api_service_control(request: Request, rag_token: Optional[str] = Cookie(None)):
    """Start or stop a docker-based or systemd service."""
    if not _auth(rag_token):
        raise HTTPException(401, "Not authenticated")
    body = await request.json()
    service_key = body.get("service")
    action = body.get("action")  # "start" or "stop"
    if action not in ("start", "stop"):
        raise HTTPException(400, f"Invalid action: {action}")

    # Docker service
    if service_key in DOCKER_SERVICE_MAP:
        containers = DOCKER_SERVICE_MAP[service_key]
        results = []
        for container in containers:
            try:
                r = subprocess.run(
                    ["docker", action, container],
                    capture_output=True, text=True, timeout=30
                )
                results.append({"container": container, "ok": r.returncode == 0, "output": r.stdout.strip() or r.stderr.strip()})
            except Exception as e:
                results.append({"container": container, "ok": False, "output": str(e)})
        all_ok = all(r["ok"] for r in results)
        return {"ok": all_ok, "results": results}

    # Systemd service
    if service_key in SYSTEMD_SERVICE_MAP:
        svc_name = SYSTEMD_SERVICE_MAP[service_key]
        systemd_action = "start" if action == "start" else "stop"
        try:
            r = subprocess.run(
                ["sudo", "systemctl", systemd_action, svc_name],
                capture_output=True, text=True, timeout=15
            )
            ok = r.returncode == 0
            return {"ok": ok, "results": [{"container": svc_name, "ok": ok, "output": r.stdout.strip() or r.stderr.strip()}]}
        except Exception as e:
            return {"ok": False, "results": [{"container": svc_name, "ok": False, "output": str(e)}]}

    # PM2 service
    if service_key in PM2_SERVICE_MAP:
        pm2_name = PM2_SERVICE_MAP[service_key]
        pm2_action = "start" if action == "start" else "stop"
        try:
            r = subprocess.run(
                ["/home/millalex921/.npm-global/bin/pm2", pm2_action, pm2_name],
                capture_output=True, text=True, timeout=15
            )
            ok = r.returncode == 0
            return {"ok": ok, "results": [{"container": pm2_name, "ok": ok, "output": r.stdout.strip() or r.stderr.strip()}]}
        except Exception as e:
            return {"ok": False, "results": [{"container": pm2_name, "ok": False, "output": str(e)}]}

    raise HTTPException(400, f"Unknown service: {service_key}")


@app.post("/api/change_password")
async def api_change_password(
    request: Request,
    rag_token: Optional[str] = Cookie(None)
):
    """Change the RAG station access password (persists to .env if available)."""
    global PASSWORD
    if not _auth(rag_token):
        raise HTTPException(401, "Not authenticated")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    new_password = (body.get("new_password") or "").strip()
    if not new_password:
        raise HTTPException(400, "新密碼不能為空")
    if len(new_password) < 6:
        raise HTTPException(400, "密碼長度至少需要 6 個字元")
    # Update in-memory password immediately
    PASSWORD = new_password
    # Try to persist to .env file
    persisted = False
    try:
        env_path = _ENV_FILE
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
            new_lines = []
            found = False
            for line in lines:
                if line.startswith("RAG_PASSWORD="):
                    new_lines.append(f"RAG_PASSWORD={new_password}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"RAG_PASSWORD={new_password}")
            env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            persisted = True
    except Exception:
        pass  # In-memory update still succeeded
    return {"ok": True, "persisted": persisted}


ENGRAM_FACTS_DIR = Path("/home/millalex921/.openclaw/workspace/memory/local/facts")
USER_MD_PATH = Path("/home/millalex921/.openclaw/workspace/USER.md")


def _parse_yaml_frontmatter(text: str) -> dict:
    """Parse YAML front-matter from a markdown file."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    yaml_text = text[3:end].strip()
    result = {}
    for line in yaml_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
            elif v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            elif v.replace(".", "").isdigit():
                v = float(v) if "." in v else int(v)
            result[k] = v
    return result


@app.post("/api/run_memory_audit_cleanup")
async def run_memory_audit_cleanup(request: Request, rag_token: Optional[str] = Cookie(None)):
    """Execute memory audit cleanup: dedup, expire, sync USER.md, remove test files."""
    if not _auth(rag_token):
        raise HTTPException(401, "Not authenticated")

    deleted_files = []
    before_count = 0
    after_count = 0

    if not ENGRAM_FACTS_DIR.exists():
        return {"deletedFiles": [], "updatedUserMd": False, "syncedStocks": None,
                "summary": "facts 目錄不存在，無需清理", "beforeCount": 0, "afterCount": 0}

    all_md = list(ENGRAM_FACTS_DIR.rglob("*.md"))
    before_count = len(all_md)

    # Parse all files
    parsed = []
    for f in all_md:
        try:
            text = f.read_text(encoding="utf-8")
            fm = _parse_yaml_frontmatter(text)
            parsed.append({"path": f, "fm": fm, "mtime": f.stat().st_mtime, "text": text})
        except Exception:
            pass

    to_delete = set()

    # 1. Dedup: same category + tags overlap > 60%, different id
    def tags_set(fm):
        t = fm.get("tags", [])
        if isinstance(t, str):
            t = [x.strip() for x in t.split(",")]
        return set(t)

    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            a, b = parsed[i], parsed[j]
            if a["fm"].get("id") and b["fm"].get("id") and a["fm"].get("id") == b["fm"].get("id"):
                continue
            if a["fm"].get("category") and a["fm"].get("category") == b["fm"].get("category"):
                ta, tb = tags_set(a["fm"]), tags_set(b["fm"])
                if ta or tb:
                    overlap = len(ta & tb) / max(len(ta | tb), 1)
                    if overlap > 0.6:
                        older = a if a["mtime"] < b["mtime"] else b
                        to_delete.add(str(older["path"]))

    # 2. Expired memories: tags contains one-time/override AND confidence <= 0.7
    for p in parsed:
        t = tags_set(p["fm"])
        if ("one-time" in t or "override" in t):
            conf = p["fm"].get("confidence", 1.0)
            if isinstance(conf, (int, float)) and conf <= 0.7:
                to_delete.add(str(p["path"]))

    # 4. Test files
    for f in ENGRAM_FACTS_DIR.rglob("*test*.md"):
        to_delete.add(str(f))

    # Delete marked files
    for path_str in to_delete:
        try:
            Path(path_str).unlink()
            deleted_files.append(path_str)
        except Exception:
            pass

    after_count = before_count - len(deleted_files)

    # 3. Sync USER.md stocks
    synced_stocks = None
    updated_user_md = False
    tsmc_files = [p for p in parsed if "台積電" in p["text"] and str(p["path"]) not in to_delete]
    if tsmc_files:
        newest = max(tsmc_files, key=lambda x: x["mtime"])
        text = newest["text"]
        # Try to extract shares and avg price
        shares_match = re.search(r"(?:持有|股數)[：:]\s*(\d[\d,]*)", text)
        avg_match = re.search(r"(?:均價|平均成本)[：:]\s*([\d.]+)", text)
        if shares_match or avg_match:
            shares = shares_match.group(1).replace(",", "") if shares_match else None
            avg = avg_match.group(1) if avg_match else None
            today = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
            synced_stocks = {"shares": shares, "avgPrice": avg, "source": newest["path"].name}
            if USER_MD_PATH.exists():
                user_text = USER_MD_PATH.read_text(encoding="utf-8")
                # Update or append 持股資訊 section
                stock_section = f"\n## 持股資訊\n\n- **台積電（2330）**：{shares or '—'} 股，均價 {avg or '—'} 元\n- **更新日期**：{today}\n"
                if "## 持股資訊" in user_text:
                    user_text = re.sub(r"## 持股資訊.*?(?=\n## |\Z)", stock_section.lstrip(), user_text, flags=re.DOTALL)
                else:
                    user_text = user_text.rstrip() + "\n" + stock_section
                USER_MD_PATH.write_text(user_text, encoding="utf-8")
                updated_user_md = True

    summary_parts = [f"清理前：{before_count} 筆記憶", f"清理後：{after_count} 筆記憶",
                     f"刪除 {len(deleted_files)} 筆"]
    if updated_user_md:
        summary_parts.append("已同步 USER.md 持股資訊")
    summary = "；".join(summary_parts)

    return {
        "deletedFiles": deleted_files,
        "updatedUserMd": updated_user_md,
        "syncedStocks": synced_stocks,
        "summary": summary,
        "beforeCount": before_count,
        "afterCount": after_count,
    }


@app.post("/api/publish_memory_audit_report")
async def publish_memory_audit_report(request: Request, rag_token: Optional[str] = Cookie(None)):
    """Compose and publish memory audit cleanup report to the article library."""
    if not _auth(rag_token):
        raise HTTPException(401, "Not authenticated")

    payload = await request.json()
    today = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    title = f"記憶稽核後續處理報告（{today}）"
    deleted = payload.get("deletedFiles", [])
    summary = payload.get("summary", "")
    before = payload.get("beforeCount", 0)
    after = payload.get("afterCount", 0)
    synced = payload.get("syncedStocks")
    updated_user = payload.get("updatedUserMd", False)

    deleted_list_md = "\n".join(f"- `{Path(f).name}`" for f in deleted) if deleted else "- （無）"
    stocks_md = ""
    if synced:
        stocks_md = f"\n## 持股同步\n\n- 台積電股數：{synced.get('shares', '—')}\n- 均價：{synced.get('avgPrice', '—')}\n- 來源檔案：`{synced.get('source', '—')}`\n"

    content = f"""# {title}

**執行時間**：{today}
**執行者**：OpenClaw 主蝦

## 執行摘要

{summary}

| 項目 | 數值 |
|------|------|
| 清理前記憶數 | {before} |
| 清理後記憶數 | {after} |
| 刪除筆數 | {len(deleted)} |
| USER.md 更新 | {'是' if updated_user else '否'} |

## 刪除的記憶檔案

{deleted_list_md}
{stocks_md}
## 備註

此報告由系統自動產生，記錄本次記憶稽核後續清理作業的執行結果。
"""

    article_id = str(uuid.uuid4())
    slug = re.sub(r"[^\w\-]", "-", title.lower())[:60]
    filename = f"{slug}.md"
    save_path = ARTICLES_DIR / f"{article_id}.md"
    save_path.write_text(content, encoding="utf-8")
    _articlemeta.append({
        "id": article_id,
        "title": title,
        "author": "OpenClaw 主蝦",
        "note": "記憶稽核後續處理自動報告",
        "filename": filename,
        "size": len(content.encode()),
        "uploaded_at": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
    })
    _save_articlemeta()
    article_url = f"https://rag.alex-stu24801.com/articles/{article_id}"
    return {"status": "ok", "article_id": article_id, "url": article_url, "title": title}


@app.get("/api/sys_status")
def api_sys_status(rag_token: Optional[str] = Cookie(None)):
    """JSON API: collect and return full system status."""
    if not _auth(rag_token):
        raise HTTPException(401, "Not authenticated")
    try:
        data = _collect_system_status()
        return data
    except Exception as e:
        raise HTTPException(500, f"Status collection failed: {e}")


@app.get("/sys_status", response_class=HTMLResponse)
def sys_status_page(rag_token: Optional[str] = Cookie(None)):
    """System status overview page."""
    if not _auth(rag_token):
        return RedirectResponse("/login")

    body = r"""
<style>
  .status-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px}
  .status-card{background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;padding:20px}
  .status-card h3{font-size:.9rem;font-weight:600;color:#a78bfa;margin-bottom:14px;display:flex;align-items:center;gap:8px}
  .stat-row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid #1e2235;font-size:.84rem}
  .stat-row:last-child{border-bottom:none}
  .stat-label{color:#64748b}
  .stat-val{color:#e2e8f0;font-family:monospace;font-size:.82rem;text-align:right;max-width:60%;word-break:break-all}
  .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .dot-green{background:#22c55e}
  .dot-red{background:#ef4444}
  .dot-yellow{background:#f59e0b}
  .dot-gray{background:#6b7280}
  .badge-model{background:#1e3a5f;color:#93c5fd;border-radius:6px;padding:2px 8px;font-size:.74rem;font-family:monospace}
  .badge-primary{background:#3b1f6e;color:#c4b5fd;border-radius:6px;padding:2px 8px;font-size:.74rem;font-family:monospace}
  .badge-ok{background:#052e16;color:#86efac;border-radius:6px;padding:2px 7px;font-size:.75rem}
  .badge-err{background:#450a0a;color:#fca5a5;border-radius:6px;padding:2px 7px;font-size:.75rem}
  .badge-warn{background:#422006;color:#fde68a;border-radius:6px;padding:2px 7px;font-size:.75rem}
  .refresh-bar{display:flex;align-items:center;gap:14px;margin-bottom:22px;flex-wrap:wrap}
  .refresh-bar .last-updated{font-size:.8rem;color:#64748b}
  #loading-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(15,17,23,.7);z-index:999;
    display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px}
  #loading-overlay.hidden{display:none}
  .session-row{padding:8px 0;border-bottom:1px solid #1e2235;font-size:.82rem}
  .session-row:last-child{border-bottom:none}
  .session-key{color:#94a3b8;font-family:monospace;font-size:.75rem;word-break:break-all}
  .session-meta{color:#64748b;font-size:.74rem;margin-top:2px}
  .cron-row{padding:8px 0;border-bottom:1px solid #1e2235;font-size:.82rem}
  .cron-row:last-child{border-bottom:none}
  .model-row{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #1e2235;font-size:.82rem}
  .model-row:last-child{border-bottom:none}
  .status-text-box{background:#0a0a0f;border:1px solid #1e2235;border-radius:8px;padding:12px;
    font-family:monospace;font-size:.8rem;color:#94a3b8;white-space:pre-wrap;line-height:1.7}
  @media(max-width:640px){
    .status-grid{grid-template-columns:1fr}
    .stat-val{max-width:55%}
  }
</style>

<!-- Loading overlay -->
<div id="loading-overlay">
  <div class="spinner"></div>
  <div style="color:#94a3b8;font-size:.9rem">載入系統狀態中，請稍候…</div>
</div>

<div class="refresh-bar">
  <button class="btn" id="refresh-btn" onclick="refreshStatus()">🔄 立即刷新</button>
  <label style="display:flex;align-items:center;gap:8px;font-size:.85rem;color:#94a3b8;cursor:pointer">
    <input type="checkbox" id="auto-refresh" onchange="toggleAutoRefresh()" style="width:auto">
    每 30 秒自動刷新
  </label>
  <button class="btn" id="memory-audit-btn" onclick="runMemoryAuditCleanup()" style="background:#1a1d2e;border:1px solid #6d28d9;color:#a78bfa">🧹 記憶稽核後續處理</button>
  <button class="btn" onclick="openChangePwdModal()" style="margin-left:auto">🔑 更改站台密碼</button>
  <div class="last-updated" id="last-updated">— 尚未載入 —</div>
</div>

<!-- Change Password Modal -->
<div id="change-pwd-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);z-index:1000;align-items:center;justify-content:center">
  <div style="background:#1a1d2e;border:1px solid #2d3154;border-radius:14px;padding:32px;max-width:420px;width:90%;position:relative">
    <h3 style="font-size:1.1rem;font-weight:600;color:#a78bfa;margin-bottom:22px">🔑 更改站台密碼</h3>
    <div id="change-pwd-error" style="display:none;background:#450a0a;border:1px solid #7f1d1d;color:#fca5a5;border-radius:8px;padding:10px 14px;font-size:.84rem;margin-bottom:16px"></div>
    <div id="change-pwd-success" style="display:none;background:#052e16;border:1px solid #14532d;color:#86efac;border-radius:8px;padding:10px 14px;font-size:.84rem;margin-bottom:16px"></div>
    <label style="display:block;font-size:.85rem;color:#94a3b8;margin-bottom:6px">新密碼</label>
    <div style="position:relative;margin-bottom:16px">
      <input id="pwd-new" type="password" placeholder="輸入新密碼（至少 6 個字元）" autocomplete="new-password"
        style="width:100%;box-sizing:border-box;padding:10px 40px 10px 12px;background:#0f1117;border:1px solid #2d3154;border-radius:8px;color:#e2e8f0;font-size:.9rem">
      <button type="button" onclick="togglePwdVisible('pwd-new','eye-new')" style="position:absolute;right:10px;top:50%;transform:translateY(-50%);background:none;border:none;color:#64748b;cursor:pointer;font-size:1rem" id="eye-new">👁</button>
    </div>
    <label style="display:block;font-size:.85rem;color:#94a3b8;margin-bottom:6px">確認新密碼</label>
    <div style="position:relative;margin-bottom:24px">
      <input id="pwd-confirm" type="password" placeholder="再次輸入新密碼" autocomplete="new-password"
        style="width:100%;box-sizing:border-box;padding:10px 40px 10px 12px;background:#0f1117;border:1px solid #2d3154;border-radius:8px;color:#e2e8f0;font-size:.9rem">
      <button type="button" onclick="togglePwdVisible('pwd-confirm','eye-confirm')" style="position:absolute;right:10px;top:50%;transform:translateY(-50%);background:none;border:none;color:#64748b;cursor:pointer;font-size:1rem" id="eye-confirm">👁</button>
    </div>
    <div style="display:flex;gap:12px;justify-content:flex-end">
      <button class="btn" onclick="closeChangePwdModal()" style="background:#1e2235;border:1px solid #2d3154;color:#94a3b8">取消</button>
      <button class="btn" id="pwd-submit-btn" onclick="submitChangePassword()">確認更改</button>
    </div>
  </div>
</div>

<div id="status-container">
  <!-- 由 JS 動態填入 -->
</div>

<script>
let _autoTimer = null;
let _statusData = null;

function toggleAutoRefresh() {
  const checked = document.getElementById('auto-refresh').checked;
  if (checked) {
    _autoTimer = setInterval(refreshStatus, 30000);
  } else {
    if (_autoTimer) { clearInterval(_autoTimer); _autoTimer = null; }
  }
}

function escHtml(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtTs(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  return d.toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false });
}

function dotClass(ok) {
  return ok ? 'dot-green' : 'dot-red';
}

function renderStatus(data) {
  const qs = data.quickStats || {};
  const container = document.getElementById('status-container');

  // ── 快速統計行 ───────────────────────────────────────────────────────────
  const quickRows = [
    ['版本', qs.version || '—'],
    ['目前 Session', qs.session || '—'],
    ['主要模型', qs.model || '—'],
    ['Token 使用', qs.tokens || '—'],
    ['Cache 命中', qs.cache || '—'],
    ['Context 使用', qs.context || '—'],
    ['Runtime', qs.runtime || '—'],
  ].map(([k, v]) => `<div class="stat-row"><span class="stat-label">${k}</span><span class="stat-val">${escHtml(v)}</span></div>`).join('');

  // ── 授權設定 ─────────────────────────────────────────────────────────────
  const authRows = (data.authProfiles || []).map(p => {
    let badge;
    if (p.credExists && p.credValid) {
      badge = '<span class="badge-ok">✓ 有效</span>';
    } else if (p.credExists && !p.credValid) {
      badge = '<span class="badge-warn">⚠ 已過期</span>';
    } else {
      badge = '<span class="badge-err">✗ 未設定</span>';
    }
    const expiryLine = p.credExpires && p.credExpires !== '—'
      ? `<div style="font-size:.7rem;color:#64748b;margin-top:2px">到期：${escHtml(p.credExpires)}</div>`
      : '';
    return `<div class="stat-row">
      <span class="stat-label">${escHtml(p.id)}</span>
      <span class="stat-val" style="display:flex;flex-direction:column;align-items:flex-end;gap:2px">
        <span style="display:flex;align-items:center;gap:6px">
          <span style="color:#64748b;font-size:.74rem">${escHtml(p.provider)} / ${escHtml(p.mode)}</span>
          ${badge}
        </span>
        ${expiryLine}
      </span>
    </div>`;
  }).join('') || '<div class="stat-row"><span class="stat-label" style="color:#4b5563">無授權設定</span></div>';

  // ── 服務狀態 ─────────────────────────────────────────────────────────────
  const svcRows = (data.services || []).map(s => {
    const badge = s.ok ? '<span class="badge-ok">● active</span>' : `<span class="badge-err">● ${escHtml(s.status)}</span>`;
    const ctrlBtn = (s.docker || s.systemd || s.pm2)
      ? `<div style="margin-top:6px;display:flex;gap:6px">
          <button class="btn" style="font-size:.72rem;padding:3px 10px;background:#052e16;border:1px solid #14532d;color:#86efac"
            onclick="serviceControl('${escHtml(s.key)}','start',this)">▶ 啟動</button>
          <button class="btn" style="font-size:.72rem;padding:3px 10px;background:#450a0a;border:1px solid #7f1d1d;color:#fca5a5"
            onclick="serviceControl('${escHtml(s.key)}','stop',this)">■ 停止</button>
        </div>`
      : '';
    return `<div class="stat-row" style="flex-direction:column;align-items:flex-start">
      <div style="display:flex;justify-content:space-between;align-items:center;width:100%">
        <span class="stat-label" style="display:flex;align-items:center;gap:7px">
          <span class="dot ${s.ok ? 'dot-green' : 'dot-red'}"></span>${escHtml(s.name)}
        </span>
        ${badge}
      </div>
      ${ctrlBtn}
    </div>`;
  }).join('') || '<div class="stat-row"><span class="stat-label" style="color:#4b5563">無服務資訊</span></div>';

  // ── 模型清單 ─────────────────────────────────────────────────────────────
  const modelRows = (data.modelFallbacks || []).slice(0, 12).map((m, i) => {
    const isPrimary = m.role === 'primary';
    const badge = isPrimary
      ? '<span class="badge-primary">★ Primary</span>'
      : `<span style="color:#4b5563;font-size:.72rem">#${i}</span>`;
    return `<div class="model-row">
      ${badge}
      <span class="badge-model">${escHtml(m.model)}</span>
    </div>`;
  }).join('') || '<div style="color:#4b5563;font-size:.84rem;padding:8px 0">無模型設定</div>';
  const modelCount = (data.modelFallbacks || []).length;

  // ── Active Sessions ───────────────────────────────────────────────────────
  // Group by agent
  const sessionsByAgent = {};
  (data.sessions || []).forEach(s => {
    if (!sessionsByAgent[s.agent]) sessionsByAgent[s.agent] = [];
    sessionsByAgent[s.agent].push(s);
  });

  const agentColors = {
    main: '#a78bfa', engineer: '#60a5fa', writer: '#34d399',
    hb: '#fbbf24', default: '#94a3b8'
  };

  let sessionRows = '';
  Object.entries(sessionsByAgent).forEach(([agent, sessions]) => {
    const color = agentColors[agent] || agentColors.default;
    // Only show the most recent session per agent key
    const mainSessions = sessions.filter(s => s.key.endsWith(':main'));
    const displaySessions = mainSessions.length ? mainSessions : sessions.slice(0, 1);
    displaySessions.forEach(s => {
      const tokPct = s.contextTokens ? Math.round(s.totalTokens / s.contextTokens * 100) : 0;
      const tokBar = s.contextTokens ? `
        <div style="height:3px;background:#1e2235;border-radius:2px;margin-top:4px;overflow:hidden">
          <div style="height:100%;background:${color};width:${Math.min(tokPct,100)}%;border-radius:2px;transition:width .5s"></div>
        </div>` : '';
      sessionRows += `<div class="session-row">
        <div style="display:flex;align-items:center;gap:7px">
          <span style="color:${color};font-weight:600;font-size:.82rem">[${escHtml(agent)}]</span>
          <span class="badge-model">${escHtml(s.model)}</span>
          ${s.label ? `<span style="color:#64748b;font-size:.72rem">${escHtml(s.label)}</span>` : ''}
        </div>
        <div class="session-key">${escHtml(s.key)}</div>
        ${s.totalTokens ? `<div class="session-meta">Tokens: ${s.totalTokens.toLocaleString()} / ${(s.contextTokens||0).toLocaleString()} (${tokPct}%)${tokBar}</div>` : ''}
        ${s.updatedAt ? `<div class="session-meta">更新：${fmtTs(s.updatedAt)}</div>` : ''}
      </div>`;
    });
  });
  if (!sessionRows) sessionRows = '<div style="color:#4b5563;font-size:.84rem;padding:8px 0">無活躍 Session</div>';

  // ── 費用估算 ──────────────────────────────────────────────────────────────
  function resolveProvider(model) {
    const m = (model || '').toLowerCase();
    if (m.startsWith('anthropic/') || m.includes('claude')) return 'anthropic';
    if (m.startsWith('github-copilot/')) return 'github-copilot';
    if (m.startsWith('google-vertex/') || m.startsWith('google/') || m.includes('gemini')) return 'google';
    if (m.startsWith('openai/') || m.includes('gpt-')) return 'openai';
    return 'other';
  }
  function modelCost(model, tokens) {
    const m = (model || '').toLowerCase();
    let inPrice = 0, outPrice = 0, label = null;
    // Anthropic
    if (m.includes('claude-opus-4') || m.includes('claude-opus-4.6')) { inPrice = 15.0; outPrice = 75.0; }
    else if (m.includes('claude-sonnet-4') || m.includes('claude-sonnet-4.6') || m.includes('claude-sonnet-4-6')) { inPrice = 3.0; outPrice = 15.0; }
    else if (m.includes('sonnet')) { inPrice = 3.0; outPrice = 15.0; }
    else if (m.includes('opus')) { inPrice = 15.0; outPrice = 75.0; }
    else if (m.includes('haiku')) { inPrice = 0.25; outPrice = 1.25; }
    // OpenAI
    else if (m.includes('gpt-5') && !m.includes('mini')) { inPrice = 10.0; outPrice = 30.0; }
    else if (m.includes('gpt-5-mini') || m.includes('gpt-5.2-mini') || m.includes('gpt-4o-mini')) { inPrice = 0.15; outPrice = 0.6; }
    else if (m.includes('gpt-4o')) { inPrice = 2.5; outPrice = 10.0; }
    else if (m.includes('gpt-4')) { inPrice = 30.0; outPrice = 60.0; }
    // GitHub Copilot — 訂閱制
    else if (m.startsWith('github-copilot/')) { label = '訂閱制'; }
    // Google — 依方案
    else if (m.startsWith('google/') || m.startsWith('google-vertex/') || m.includes('gemini')) { label = 'Google (依方案)'; }
    if (label !== null) return { cost: 0, label };
    const cost = (tokens * 0.3 / 1e6) * inPrice + (tokens * 0.7 / 1e6) * outPrice;
    return { cost, label: null };
  }
  const costSessions = (data.sessions || []).filter(s => (s.totalTokens || 0) > 0);
  // 按 provider 分開累計費用
  const providerCosts = {};
  let costRows = '';
  for (const s of costSessions) {
    const tokens = s.totalTokens || 0;
    const { cost, label } = modelCost(s.model, tokens);
    const provider = resolveProvider(s.model);
    if (cost > 0) {
      providerCosts[provider] = (providerCosts[provider] || 0) + cost;
    }
    const costStr = label ? `<span style="color:#64748b">${label}</span>`
      : cost > 0 ? `<span style="color:#fde68a">$${cost.toFixed(4)}</span>`
      : '<span style="color:#4b5563">$0.0000</span>';
    const keyShort = (s.key || s.sessionKey || '—').slice(0, 30);
    const modelShort = (s.model || '—').slice(0, 28);
    costRows += `<tr>
      <td style="padding:6px 10px;font-size:.82rem;color:#94a3b8;font-family:monospace">${escHtml(keyShort)}</td>
      <td style="padding:6px 10px;font-size:.78rem;color:#64748b">${escHtml(modelShort)}</td>
      <td style="padding:6px 10px;font-size:.82rem;color:#e2e8f0;text-align:right">${tokens.toLocaleString()}</td>
      <td style="padding:6px 10px;font-size:.82rem;text-align:right">${costStr}</td>
    </tr>`;
  }
  // 合計列 — 每個有費用的 provider 各顯示一行
  const providerLabels = { anthropic: 'Anthropic', openai: 'OpenAI', google: 'Google', 'github-copilot': 'GitHub Copilot', other: 'Other' };
  let footerRows = '';
  const hasCost = Object.keys(providerCosts).length > 0;
  if (hasCost) {
    for (const [prov, total] of Object.entries(providerCosts)) {
      footerRows += `<tr style="border-top:1px solid #2d3154;background:#0f1117">
        <td colspan="3" style="padding:6px 10px;font-size:.84rem;color:#a78bfa;font-weight:600">合計 ${providerLabels[prov] || prov} 費用</td>
        <td style="padding:6px 10px;text-align:right;font-size:.88rem;font-weight:700;color:#fde68a">$${total.toFixed(4)} USD</td>
      </tr>`;
    }
  }
  const costCard = costSessions.length === 0
    ? '<div style="color:#4b5563;font-size:.84rem;padding:8px 0">無 Token 使用紀錄</div>'
    : `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:.84rem">
        <thead><tr style="background:#0f1117">
          <th style="padding:6px 10px;text-align:left;color:#64748b;font-weight:500">Session</th>
          <th style="padding:6px 10px;text-align:left;color:#64748b;font-weight:500">Model</th>
          <th style="padding:6px 10px;text-align:right;color:#64748b;font-weight:500">Tokens</th>
          <th style="padding:6px 10px;text-align:right;color:#64748b;font-weight:500">費用估算</th>
        </tr></thead>
        <tbody>${costRows}</tbody>
        ${hasCost ? `<tfoot>${footerRows}</tfoot>` : ''}
      </table></div>`;

  // ── Cron Jobs ─────────────────────────────────────────────────────────────
  const cronRows = (data.cronJobs || []).map(j => {
    const en = j.enabled !== false;
    const badge = en ? '<span class="badge-ok">✓ 啟用</span>' : '<span class="badge-warn">⏸ 停用</span>';
    const schedText = j.schedule?.kind === 'cron' ? j.schedule.expr
      : j.schedule?.kind === 'every' ? `每 ${Math.round((j.schedule.everyMs||0)/60000)} 分`
      : j.schedule?.kind === 'at' ? j.schedule.at
      : JSON.stringify(j.schedule || {});
    return `<div class="cron-row">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span style="color:#e2e8f0;font-size:.84rem;flex:1;min-width:0;word-break:break-word">${escHtml(j.name || j.id)}</span>
        ${badge}
      </div>
      <div style="font-size:.74rem;color:#64748b;margin-top:3px">
        ⏰ ${escHtml(schedText)}
        ${j.lastRunAt ? ' &nbsp;·&nbsp; 上次：' + fmtTs(j.lastRunAt) : ''}
      </div>
    </div>`;
  }).join('') || '<div style="color:#4b5563;font-size:.84rem;padding:8px 0">無排程任務</div>';

  // ── LLM Proxy Backends ────────────────────────────────────────────────────
  const backends = data.llmProxyBackends || {};
  const proxyBackendRows = Object.entries(backends).map(([name, b]) => {
    const enabled = b.enabled !== false;
    let badge;
    if (enabled && b.token_valid !== false) badge = '<span class="badge-ok">✓ 啟用</span>';
    else if (enabled && b.token_valid === false) badge = '<span class="badge-warn">⚠ Token 無效</span>';
    else badge = '<span class="badge-err">✗ 已停用</span>';
    return `<div class="stat-row">
      <span class="stat-label" style="display:flex;align-items:center;gap:7px">
        <span class="dot ${enabled ? 'dot-green' : 'dot-red'}"></span>${escHtml(name)}
      </span>${badge}</div>`;
  }).join('') || '<div class="stat-row"><span class="stat-label" style="color:#4b5563">無法讀取後端狀態</span></div>';

  // ── WhatsApp 狀態 ─────────────────────────────────────────────────────────
  const wa = data.whatsappStatus || {};
  const waBadge = wa.hasCreds
    ? `<span class="badge-ok">✓ Session 已建立</span>`
    : wa.exists
      ? `<span class="badge-warn">⚠ 目錄存在但無 creds</span>`
      : `<span class="badge-err">✗ 未配對</span>`;
  const waRows = `<div class="stat-row"><span class="stat-label">Session 目錄</span>${waBadge}</div>
    <div class="stat-row"><span class="stat-label">Session 檔案數</span><span class="stat-val">${wa.fileCount || 0}</span></div>`;

  // ── Twilio 狀態 ──────────────────────────────────────────────────────────
  const tw = data.twilioInfo || {};
  const twilioRows = tw.enabled
    ? `<div class="stat-row"><span class="stat-label">Account SID</span><span class="stat-val" style="font-family:monospace">${escHtml(tw.accountSidMasked||'—')}</span></div>
       <div class="stat-row"><span class="stat-label">發話號碼</span><span class="stat-val">${escHtml(tw.fromNumber||'—')}</span></div>
       <div class="stat-row"><span class="stat-label">Webhook URL</span><span class="stat-val" style="font-size:.74rem;word-break:break-all">${escHtml(tw.webhookUrl||'—')}</span></div>`
    : '<div class="stat-row"><span class="stat-label" style="color:#4b5563">Voice-call plugin 未啟用</span></div>';

  // ── Agent 模型配置 ────────────────────────────────────────────────────────
  const agentConfigRows = (data.agentConfigs || []).map(a => {
    const isPremium = (a.model||'').includes('claude') || (a.model||'').includes('sonnet') || (a.model||'').includes('opus');
    const modelBadge = isPremium
      ? `<span class="badge-primary">${escHtml(a.model||'—')}</span>`
      : `<span class="badge-model">${escHtml(a.model||'—')}</span>`;
    return `<div class="stat-row">
      <span class="stat-label">🦐 ${escHtml(a.name||a.id)}</span>
      <span class="stat-val">${modelBadge}</span></div>`;
  }).join('') || '<div class="stat-row"><span class="stat-label" style="color:#4b5563">無 Agent 設定</span></div>';

  // ── Context 設定 ─────────────────────────────────────────────────────────
  const cs = data.contextSettings || {};
  const ctxRows = [
    ['Pruning 模式', cs.pruningMode || '—'],
    ['Pruning TTL', cs.pruningTtl || '—'],
    ['Compaction 模式', cs.compactionMode || '—'],
    ['Memory Flush', cs.memoryFlush ? '✓ 啟用' : '✗ 停用'],
    ['Subagent 最大並發', String(cs.subagentMaxConcurrent ?? '—')],
    ['Subagent 封存時限', cs.subagentArchiveMin ? cs.subagentArchiveMin + ' 分鐘' : '—'],
  ].map(([k,v]) => `<div class="stat-row"><span class="stat-label">${k}</span><span class="stat-val">${escHtml(v)}</span></div>`).join('');

  // ── 頂端異常警告條 ────────────────────────────────────────────────────────
  const alerts = [];
  if (backends.anthropic && backends.anthropic.enabled === false) {
    alerts.push('⚠️ <strong>LLM Proxy Anthropic backend 已停用</strong>：Anthropic 請求走 Gateway 直連，無 proxy 層備援。');
  }
  (data.authProfiles || []).forEach(p => {
    if (p.credExists && !p.credValid) {
      alerts.push(`⚠️ <strong>${escHtml(p.provider)}</strong> 授權已失效，請重新設定。`);
    } else if (!p.credExists && p.provider !== 'whatsapp') {
      alerts.push(`⚠️ <strong>${escHtml(p.provider)}</strong> 授權未設定。`);
    }
  });
  const alertBanner = alerts.length > 0
    ? alerts.map(a => `<div style="background:#422006;border:1px solid #92400e;color:#fde68a;border-radius:8px;padding:10px 14px;font-size:.84rem;margin-bottom:8px">${a}</div>`).join('')
    : '';

  // ── Status Raw Text ───────────────────────────────────────────────────────
  const rawText = data.statusText ? `
    <div class="status-card" style="margin-top:18px">
      <h3>📋 完整狀態原文</h3>
      <div class="status-text-box">${escHtml(data.statusText)}</div>
    </div>` : '';

  // ── Assemble ──────────────────────────────────────────────────────────────
  container.innerHTML = `
    ${alertBanner}
    <div class="status-grid">

      <div class="status-card">
        <h3>⚡ 快速統計</h3>
        ${quickRows}
      </div>

      <div class="status-card">
        <h3>🔐 授權設定（${(data.authProfiles||[]).length} 個）</h3>
        ${authRows}
      </div>

      <div class="status-card">
        <h3>🖧 服務狀態</h3>
        ${svcRows}
      </div>

      <div class="status-card">
        <h3>🔀 LLM Proxy Backends</h3>
        ${proxyBackendRows}
      </div>

      <div class="status-card">
        <h3>📱 WhatsApp 狀態</h3>
        ${waRows}
      </div>

      <div class="status-card">
        <h3>📞 Twilio 語音通話</h3>
        ${twilioRows}
      </div>

      <div class="status-card">
        <h3>🤖 模型清單（${modelCount} 個）</h3>
        <div style="max-height:260px;overflow-y:auto">${modelRows}</div>
      </div>

      <div class="status-card">
        <h3>🦐 Agent 模型配置</h3>
        ${agentConfigRows}
      </div>

      <div class="status-card">
        <h3>🪟 Context 視窗設定</h3>
        ${ctxRows}
      </div>

      <div class="status-card" style="grid-column:1/-1">
        <h3>🦐 Agent Sessions</h3>
        <div style="max-height:360px;overflow-y:auto">${sessionRows}</div>
      </div>

      <div class="status-card" style="grid-column:1/-1">
        <h3>💰 Token 費用估算</h3>
        ${costCard}
      </div>

      <div class="status-card" style="grid-column:1/-1">
        <h3>⏰ Cron Jobs（${(data.cronJobs||[]).length} 個）</h3>
        <div style="max-height:300px;overflow-y:auto">${cronRows}</div>
      </div>

    </div>
    ${rawText}
  `;

  document.getElementById('last-updated').textContent = '最後更新：' + data.collectedAt + ' (Asia/Taipei)';
}

async function serviceControl(serviceKey, action, btn) {
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳…';
  try {
    const r = await fetch('/api/service_control', {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({service: serviceKey, action: action})
    });
    const d = await r.json();
    if (d.ok) {
      btn.textContent = action === 'start' ? '✅ 已啟動' : '✅ 已停止';
      setTimeout(() => refreshStatus(), 1500);
    } else {
      const errMsg = (d.results || []).map(x => x.output).join('; ');
      btn.textContent = '❌ 失敗';
      alert('操作失敗：' + errMsg);
      setTimeout(() => { btn.textContent = origText; btn.disabled = false; }, 2000);
    }
  } catch(e) {
    btn.textContent = '❌ 錯誤';
    setTimeout(() => { btn.textContent = origText; btn.disabled = false; }, 2000);
  }
}

async function refreshStatus() {
  const btn = document.getElementById('refresh-btn');
  btn.disabled = true;
  btn.textContent = '⏳ 載入中…';
  document.getElementById('loading-overlay').classList.remove('hidden');
  try {
    const r = await fetch('/api/sys_status', { credentials: 'include' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    _statusData = await r.json();
    renderStatus(_statusData);
  } catch(e) {
    document.getElementById('status-container').innerHTML =
      `<div class="alert alert-error">❌ 載入失敗：${e.message}</div>`;
  } finally {
    document.getElementById('loading-overlay').classList.add('hidden');
    btn.disabled = false;
    btn.textContent = '🔄 立即刷新';
  }
}

// Initial load
refreshStatus();

function openChangePwdModal() {
  document.getElementById('change-pwd-modal').style.display = 'flex';
  document.getElementById('pwd-new').value = '';
  document.getElementById('pwd-confirm').value = '';
  document.getElementById('change-pwd-error').style.display = 'none';
  document.getElementById('change-pwd-success').style.display = 'none';
  document.getElementById('pwd-submit-btn').disabled = false;
}

function closeChangePwdModal() {
  document.getElementById('change-pwd-modal').style.display = 'none';
}

function togglePwdVisible(inputId, btnId) {
  const inp = document.getElementById(inputId);
  inp.type = inp.type === 'password' ? 'text' : 'password';
}

async function submitChangePassword() {
  const newPwd = document.getElementById('pwd-new').value;
  const confirmPwd = document.getElementById('pwd-confirm').value;
  const errEl = document.getElementById('change-pwd-error');
  const okEl = document.getElementById('change-pwd-success');
  errEl.style.display = 'none';
  okEl.style.display = 'none';

  if (!newPwd) {
    errEl.textContent = '新密碼不能為空';
    errEl.style.display = 'block';
    return;
  }
  if (newPwd.length < 6) {
    errEl.textContent = '密碼長度至少需要 6 個字元';
    errEl.style.display = 'block';
    return;
  }
  if (newPwd !== confirmPwd) {
    errEl.textContent = '兩次輸入的密碼不一致，請重新確認';
    errEl.style.display = 'block';
    return;
  }

  const btn = document.getElementById('pwd-submit-btn');
  btn.disabled = true;
  btn.textContent = '更新中…';
  try {
    const r = await fetch('/api/change_password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ new_password: newPwd })
    });
    const data = await r.json();
    if (!r.ok) {
      errEl.textContent = data.detail || '更改失敗，請重試';
      errEl.style.display = 'block';
    } else {
      okEl.textContent = data.persisted
        ? '✅ 密碼已更新並持久化到 .env 檔！下次重啟容器後仍然有效。'
        : '✅ 密碼已更新（本次運行有效）。注意：.env 檔更新失敗，重啟容器後需重新設定。';
      okEl.style.display = 'block';
      document.getElementById('pwd-new').value = '';
      document.getElementById('pwd-confirm').value = '';
      setTimeout(closeChangePwdModal, 3000);
    }
  } catch(e) {
    errEl.textContent = '網路錯誤：' + e.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = '確認更改';
  }
}

async function runMemoryAuditCleanup() {
  const btn = document.getElementById('memory-audit-btn');
  btn.disabled = true; btn.textContent = '⏳ 處理中…';
  try {
    const r1 = await fetch('/api/run_memory_audit_cleanup', {method:'POST'});
    const cleanup = await r1.json();
    const r2 = await fetch('/api/publish_memory_audit_report', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(cleanup)
    });
    const pub = await r2.json();
    alert('✅ 清理完成！\n\n' + cleanup.summary + '\n\n報告已上架：' + (pub.url || pub.articleUrl || '（查看文章庫）'));
  } catch(e) {
    alert('❌ 執行失敗：' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '🧹 記憶稽核後續處理';
  }
}
</script>
"""
    return HTMLResponse(_base_html(body, "系統狀態 — RAG KB"))
