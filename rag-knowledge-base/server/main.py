"""
RAG Knowledge Base Server — QMD SQLite direct + embed_server (port 8766)
v2: 支援分層分類 (category) 管理與搜尋過濾
"""

import os, uuid, json, hashlib, secrets, io, subprocess, shutil, re, sqlite3, struct, urllib.request
import threading, queue, time
from pathlib import Path
from typing import List, Optional, AsyncGenerator
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Cookie, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, StreamingResponse

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

# ── Articles (writer uploads) ─────────────────────────────────────────────────
ARTICLES_DIR      = DATA_DIR / "articles"
ARTICLEMETA_PATH  = DATA_DIR / "articlemeta.json"

# ── In-memory session store ───────────────────────────────────────────────────
_sessions: set = set()

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

def _qmd_update_embed():
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
    _load_filemeta()
    _load_articlemeta()

# ═══════════════════════════════════════════════════════════════════════════════
#  HTML helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _base_html(body: str, title="RAG Knowledge Base", sidebar_cats: List[str] = None) -> str:
    # Build sidebar category links
    sidebar_html = ""
    if sidebar_cats is not None:
        links = '<a href="/dashboard" style="color:#94a3b8;text-decoration:none;display:block;padding:6px 8px;border-radius:6px;font-size:.82rem">📋 全部文件</a>'
        for cat in sidebar_cats:
            depth = cat.count("/")
            indent = depth * 14
            label = cat.split("/")[-1]
            links += f'<a href="/dashboard?cat={cat}" style="color:#94a3b8;text-decoration:none;display:block;padding:5px 8px 5px {8+indent}px;border-radius:6px;font-size:.82rem">{"  " * depth}📁 {label}</a>'
        sidebar_html = f"""
<div style="width:200px;flex-shrink:0">
  <div style="background:#1a1d2e;border:1px solid #2d3154;border-radius:10px;padding:14px">
    <div style="color:#64748b;font-size:.75rem;font-weight:600;margin-bottom:10px;letter-spacing:.05em">分類</div>
    {links}
  </div>
</div>"""

    layout_start = '<div style="display:flex;gap:24px;align-items:flex-start">' if sidebar_cats is not None else ""
    layout_end   = "</div>" if sidebar_cats is not None else ""
    content_wrap_start = '<div style="flex:1;min-width:0">' if sidebar_cats is not None else ""
    content_wrap_end   = "</div>" if sidebar_cats is not None else ""

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}}
  .topbar{{background:#1a1d2e;border-bottom:1px solid #2d3154;padding:14px 32px;display:flex;align-items:center;gap:16px}}
  .topbar h1{{font-size:1.1rem;font-weight:600;color:#a78bfa}}
  .topbar a{{color:#94a3b8;font-size:.85rem;text-decoration:none}}
  .topbar a:hover{{color:#a78bfa}}
  .topbar a.active{{color:#a78bfa;border-bottom:2px solid #a78bfa;padding-bottom:2px}}
  .container{{max-width:1100px;margin:40px auto;padding:0 24px}}
  .card{{background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;padding:28px;margin-bottom:24px}}
  .card h2{{font-size:1rem;font-weight:600;color:#a78bfa;margin-bottom:18px;display:flex;align-items:center;gap:8px}}
  label{{font-size:.85rem;color:#94a3b8;display:block;margin-bottom:6px}}
  input[type=text],input[type=password],input[type=file],select{{
    width:100%;padding:10px 14px;background:#0f1117;border:1px solid #2d3154;
    border-radius:8px;color:#e2e8f0;font-size:.9rem;outline:none;transition:.2s
  }}
  select option{{background:#1a1d2e}}
  input:focus,select:focus{{border-color:#a78bfa}}
  .btn{{display:inline-block;padding:10px 22px;background:#7c3aed;color:#fff;
    border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:500;transition:.2s}}
  .btn:hover{{background:#6d28d9}}
  .btn-sm{{padding:6px 14px;font-size:.8rem;border-radius:6px}}
  .btn-danger{{background:#dc2626}}.btn-danger:hover{{background:#b91c1c}}
  .btn-ghost{{background:transparent;border:1px solid #374151;color:#94a3b8}}
  .btn-ghost:hover{{background:#1f2937;color:#e2e8f0}}
  .alert{{padding:12px 16px;border-radius:8px;font-size:.85rem;margin-bottom:16px}}
  .alert-error{{background:#450a0a;border:1px solid #7f1d1d;color:#fca5a5}}
  .alert-success{{background:#052e16;border:1px solid #14532d;color:#86efac}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem}}
  th{{text-align:left;padding:10px 12px;background:#0f1117;color:#64748b;font-weight:500;border-bottom:1px solid #2d3154}}
  td{{padding:10px 12px;border-bottom:1px solid #1e2235;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#1e2235}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.75rem;font-weight:500}}
  .badge-purple{{background:#3b0764;color:#c4b5fd}}
  .badge-blue{{background:#1e3a5f;color:#93c5fd}}
  .badge-green{{background:#052e16;color:#86efac}}
  .empty{{color:#4b5563;text-align:center;padding:32px;font-size:.9rem}}
  .form-row{{display:flex;gap:12px;align-items:flex-end}}
  .form-row>*{{flex:1}}
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
</style>
</head>
<body>
<div class="topbar">
  <h1>🦐 RAG Knowledge Base</h1>
  <a href="/dashboard">Dashboard</a>
  <a href="/search_ui">搜尋</a>
  <a href="/grade_ui">📝 評分</a>
  <a href="/articles">📰 文章庫</a>
  <a href="/logout" style="margin-left:auto">登出</a>
</div>
<div class="container">
  {layout_start}
  {sidebar_html}
  {content_wrap_start}
  {body}
  {content_wrap_end}
  {layout_end}
</div>
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
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px">
      <div>
        <label>選擇檔案（.txt / .md / .pdf）</label>
        <input type="file" name="file" accept=".txt,.md,.pdf" required>
      </div>
      <div>
        <label>標籤名稱（選填）</label>
        <input type="text" name="source_name" placeholder="e.g. 玉山銀行規格書">
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
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
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
  <div style="display:grid;grid-template-columns:1fr auto auto;gap:12px;margin-bottom:20px;align-items:end">
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
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
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

3. **輸出報告**，格式如下：

# 📋 評分報告

## 基本資訊
- Repo: {repo_url}
- 分支: {branch}
- 評分時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}

## 🎯 題目判定
（說明對應哪一題及判斷依據）

## 📊 評分結果
（依評分方式逐項評分，含✅⚠️❌）

## 💯 總分
（各項得分統計表格）

## 💡 改進建議
（主要缺失與建議）
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

3. **輸出報告**，格式如下：

# 📋 評分報告

## 基本資訊
- Repo: {repo_url}
- 分支: {branch}
- 評分時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}

## 🎯 題目判定
（說明對應哪一題及判斷依據）

## 📊 評分結果
（依評分方式逐項評分，含✅⚠️❌）

## 💯 總分
（各項得分統計表格）

## 💡 改進建議
（主要缺失與建議）
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
  <div style="display:grid;grid-template-columns:1fr auto;gap:12px;margin-bottom:14px;align-items:end">
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
    .then(r => r.json())
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
  const el = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.id = 'chat-thinking';
  div.style.cssText = 'margin-bottom:14px;display:flex;align-items:flex-start';
  div.innerHTML = '<div style="padding:10px 14px;background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;color:#64748b;font-size:.87rem">⏳ AI 思考中…</div>';
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
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
    document.getElementById('chat-thinking')?.remove();

    const reply = data.reply || '（無回應）';
    appendChatMsg('assistant', reply);
    _chatHistory.push({{role:'assistant', content: reply}});
  }} catch(e) {{
    document.getElementById('chat-thinking')?.remove();
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
  .topbar{{background:#1a1d2e;border-bottom:1px solid #2d3154;padding:14px 32px;display:flex;align-items:center;gap:16px}}
  .topbar h1{{font-size:1.1rem;font-weight:600;color:#34d399}}
  .topbar a{{color:#94a3b8;font-size:.85rem;text-decoration:none}}
  .container{{max-width:760px;margin:40px auto;padding:0 24px}}
  .card{{background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;padding:28px;margin-bottom:24px}}
  .card h2{{font-size:1rem;font-weight:600;color:#34d399;margin-bottom:18px}}
  label{{font-size:.85rem;color:#94a3b8;display:block;margin-bottom:6px}}
  input[type=text],input[type=password],textarea{{
    width:100%;padding:10px 14px;background:#0f1117;border:1px solid #2d3154;
    border-radius:8px;color:#e2e8f0;font-size:.9rem;outline:none;transition:.2s
  }}
  input:focus,textarea:focus{{border-color:#34d399}}
  .btn{{display:inline-block;padding:10px 22px;background:#059669;color:#fff;
    border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:500;transition:.2s}}
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
    items = "".join(
        f'<li>📄 {a["title"]} <span style="color:#4b5563;font-size:.78rem">— {a["uploaded_at"]}</span></li>'
        for a in recent
    ) or "<li style='color:#4b5563'>尚無投稿記錄</li>"

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
  <h2>📋 最近投稿</h2>
  <ul class="filed-list">{items}</ul>
</div>
"""
    return HTMLResponse(_writer_base_html(body))

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
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
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

@app.get("/articles", response_class=HTMLResponse)
def articles_list(rag_token: Optional[str] = Cookie(None), msg: str = ""):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    ok_html = f'<div class="alert alert-success">✅ {msg}</div>' if msg else ""
    total = len(_articlemeta)

    if _articlemeta:
        rows = ""
        for a in reversed(_articlemeta):
            size_kb = a.get("size", 0) // 1024
            rows += f"""<tr>
  <td><a href="/articles/{a['id']}" style="color:#a78bfa;text-decoration:none">{a['title']}</a></td>
  <td style="color:#94a3b8">{a.get('author','—')}</td>
  <td style="color:#64748b;font-size:.8rem">{a.get('uploaded_at','')}</td>
  <td style="color:#64748b;font-size:.8rem">{size_kb} KB</td>
  <td>
    <a href="/articles/{a['id']}/download" class="btn btn-sm btn-ghost" style="text-decoration:none;padding:5px 12px;background:transparent;border:1px solid #374151;color:#94a3b8;border-radius:6px;font-size:.8rem">⬇ .md</a>
    &nbsp;
    <form method="post" action="/articles/{a['id']}/delete" style="display:inline"
          onsubmit="return confirm('確定刪除？')">
      <button style="padding:5px 10px;background:#dc2626;border:none;border-radius:6px;color:#fff;font-size:.8rem;cursor:pointer">🗑</button>
    </form>
  </td>
</tr>"""
        table = f"""<table>
<thead><tr>
  <th>標題</th><th>作者</th><th>投稿時間</th><th>大小</th><th>操作</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
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
    return HTMLResponse(_base_html(body, "文章庫 — RAG KB"))

@app.get("/articles/{article_id}", response_class=HTMLResponse)
def article_view(article_id: str, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")
    am = next((a for a in _articlemeta if a["id"] == article_id), None)
    if not am:
        raise HTTPException(404)
    path = Path(am["path"])
    content = path.read_text(encoding="utf-8") if path.exists() else "（檔案遺失）"

    # Escape for JS string
    content_escaped = json.dumps(content)

    body = f"""
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:20px">
    <div>
      <h2 style="font-size:1.4rem;color:#e2e8f0;margin-bottom:6px">{am['title']}</h2>
      <div style="font-size:.85rem;color:#64748b">✍️ {am.get('author','—')} &nbsp;·&nbsp; {am.get('uploaded_at','')}</div>
      {f'<div style="font-size:.8rem;color:#4b5563;margin-top:4px">備註：{am["note"]}</div>' if am.get('note') else ''}
    </div>
    <div style="display:flex;gap:10px">
      <a href="/articles/{article_id}/download"
         style="padding:8px 16px;background:transparent;border:1px solid #374151;color:#94a3b8;border-radius:8px;font-size:.85rem;text-decoration:none">
        ⬇ 下載 .md
      </a>
      <a href="/articles" style="padding:8px 16px;background:transparent;border:1px solid #374151;color:#94a3b8;border-radius:8px;font-size:.85rem;text-decoration:none">
        ← 回列表
      </a>
    </div>
  </div>

  <div id="rendered" style="
    line-height:1.8;color:#cbd5e1;
    font-size:.95rem;
  "></div>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
const md = {content_escaped};
document.getElementById('rendered').innerHTML = marked.parse(md);
</script>
<style>
  #rendered h1,#rendered h2,#rendered h3{{color:#a78bfa;margin:1.2em 0 .5em}}
  #rendered p{{margin-bottom:.9em}}
  #rendered code{{background:#0f1117;padding:2px 6px;border-radius:4px;font-size:.85em;color:#86efac}}
  #rendered pre{{background:#0f1117;border:1px solid #2d3154;border-radius:8px;padding:14px;overflow-x:auto;margin-bottom:1em}}
  #rendered pre code{{background:none;padding:0}}
  #rendered blockquote{{border-left:3px solid #a78bfa;padding-left:14px;color:#94a3b8;margin-bottom:1em}}
  #rendered a{{color:#60a5fa}}
  #rendered ul,#rendered ol{{padding-left:1.5em;margin-bottom:.9em}}
  #rendered img{{max-width:100%;border-radius:8px}}
  #rendered hr{{border:none;border-top:1px solid #2d3154;margin:1.5em 0}}
</style>
"""
    return HTMLResponse(_base_html(body, f"{am['title']} — 文章庫"))

@app.get("/articles/{article_id}/download")
def article_download(article_id: str, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
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
