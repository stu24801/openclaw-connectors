"""
RAG Knowledge Base Server — QMD SQLite direct + embed_server (port 8766)
v2: 支援分層分類 (category) 管理與搜尋過濾
"""

import os, uuid, json, hashlib, secrets, io, subprocess, shutil, re, sqlite3, struct, urllib.request
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Cookie, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse

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
PASSWORD      = os.getenv("RAG_PASSWORD", "changeme")

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
    _load_filemeta()

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
    Grade a GitHub repo against KB questions and scoring criteria.
    Body: { "repo_url": "...", "token": "ghp_xxx (optional)" }
    Returns: { "question": "...", "report": "markdown...", "raw_code_summary": [...] }
    """
    repo_url = payload.get("repo_url", "").strip()
    token    = payload.get("token", "").strip() or None
    if not repo_url:
        raise HTTPException(400, "repo_url is required")

    # 1. Parse URL
    try:
        owner, repo, branch = _parse_github_url(repo_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 2. Get all questions from KB
    questions = [f for f in _filemeta if "題目" in f.get("category","") or "題目" in f.get("source_name","")]
    scoring   = [f for f in _filemeta if "評分" in f.get("category","") or "評分" in f.get("source_name","")]

    if not questions:
        raise HTTPException(404, "知識庫中找不到題目文件，請先上傳題目到分類「新人面試題庫/題目_md」")
    if not scoring:
        raise HTTPException(404, "知識庫中找不到評分方式文件，請先上傳評分方式到分類「新人面試題庫/評分方式_md」")

    # 3. Read scoring criteria
    scoring_content = Path(scoring[0]["path"]).read_text(encoding="utf-8")

    # 4. Get repo file tree
    try:
        tree = _github_tree(owner, repo, branch, token)
    except Exception as e:
        raise HTTPException(502, f"GitHub API 錯誤：{e}")

    # 5. Read priority files (max 30)
    code_files = [item["path"] for item in tree if _should_read(item["path"])][:30]
    code_snippets = []
    for fpath in code_files:
        content = _github_file(owner, repo, branch, fpath, token)
        if content:
            code_snippets.append({"path": fpath, "content": content[:3000]})

    # 6. Concatenate all questions text
    questions_text = ""
    for q in questions:
        qcontent = Path(q["path"]).read_text(encoding="utf-8")
        questions_text += f"\n\n---\n## 題目：{q['source_name']}\n{qcontent}"

    # 7. Build prompt for OpenClaw
    code_summary = "\n\n".join(
        f"### {s['path']}\n```\n{s['content'][:1500]}\n```"
        for s in code_snippets[:15]
    )

    return {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "file_count": len(tree),
        "code_files_read": len(code_snippets),
        "questions_available": [q["source_name"] for q in questions],
        "scoring_criteria": scoring_content,
        "questions_text": questions_text,
        "code_summary": code_summary,
        "grading_prompt": f"""你是一位嚴格但公正的後端工程師面試評審。

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

3. **輸出報告**：以下列格式輸出完整評分報告：

```
# 📋 評分報告

## 基本資訊
- Repo: {repo_url}
- 分支: {branch}
- 評分時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}

## 🎯 題目判定
（說明對應哪一題及判斷依據）

## 📊 評分結果

（依評分方式逐項評分）

## 💯 總分
（統計各項得分）

## 💡 改進建議
（列出主要缺失與建議）
```
""",
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  Grade UI
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/grade_ui", response_class=HTMLResponse)
def grade_ui(rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    # Count available questions
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

  <!-- KB 狀態 -->
  <div style="background:#0f1117;border:1px solid #2d3154;border-radius:8px;padding:14px;margin-bottom:20px;display:flex;align-items:center;gap:12px">
    <span style="font-size:1.4rem">{status_icon}</span>
    <div>
      <div style="font-size:.85rem;color:{status_color}">{status_msg}</div>
      <div style="margin-top:6px">{q_badges}</div>
    </div>
  </div>

  <!-- 輸入表單 -->
  <div style="margin-bottom:16px">
    <label>GitHub Repo URL</label>
    <input type="text" id="repo-url" placeholder="https://github.com/owner/repo/tree/main"
           style="margin-bottom:12px">
    <label>GitHub Token（選填，私有 repo 才需要）</label>
    <input type="text" id="gh-token" placeholder="ghp_xxxxx（選填）">
  </div>

  <button class="btn" id="grade-btn" onclick="doGrade()">🚀 開始評分</button>
  <span style="margin-left:12px;font-size:.8rem;color:#64748b">評分約需 30–60 秒</span>
</div>

<!-- Loading -->
<div id="loading" style="display:none">
  <div class="card" style="text-align:center;padding:48px">
    <div class="spinner" style="margin-bottom:16px"></div>
    <div style="color:#a78bfa;font-weight:600;margin-bottom:8px">正在分析 Repo 並評分中…</div>
    <div id="loading-step" style="color:#64748b;font-size:.85rem">讀取題目與評分方式</div>
  </div>
</div>

<!-- Result -->
<div id="result-area" style="display:none">
  <div class="card">
    <h2>📊 評分報告</h2>
    <div style="display:flex;gap:10px;margin-bottom:16px">
      <button class="btn btn-sm btn-ghost" onclick="copyReport()">📋 複製報告</button>
      <button class="btn btn-sm btn-ghost" onclick="downloadReport()">⬇ 下載 .md</button>
    </div>
    <div id="report-md" style="display:none"></div>
    <div id="report-html" style="
      background:#0f1117;border:1px solid #2d3154;border-radius:8px;
      padding:20px;font-size:.88rem;line-height:1.7;overflow-x:auto;
      white-space:pre-wrap;font-family:'Segoe UI',sans-serif;color:#e2e8f0
    "></div>
  </div>
</div>

<!-- Error -->
<div id="error-area"></div>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
let _reportMd = '';

async function doGrade() {{
  const repoUrl = document.getElementById('repo-url').value.trim();
  const token   = document.getElementById('gh-token').value.trim();
  if (!repoUrl) {{ alert('請輸入 GitHub Repo URL'); return; }}

  document.getElementById('grade-btn').disabled = true;
  document.getElementById('loading').style.display = 'block';
  document.getElementById('result-area').style.display = 'none';
  document.getElementById('error-area').innerHTML = '';

  const steps = [
    '讀取題目與評分方式…',
    '連接 GitHub API，取得檔案列表…',
    '讀取程式碼檔案…',
    'AI 分析中，判定題目…',
    '逐項評分中…',
    '產生報告…',
  ];
  let si = 0;
  const stepInterval = setInterval(() => {{
    if (si < steps.length) {{
      document.getElementById('loading-step').textContent = steps[si++];
    }}
  }}, 8000);

  try {{
    // Step 1: Get grading context from RAG KB
    const gradeRes = await fetch('/grade', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ repo_url: repoUrl, token: token || null }}),
    }});
    if (!gradeRes.ok) {{
      const err = await gradeRes.json();
      throw new Error(err.detail || gradeRes.statusText);
    }}
    const gradeData = await gradeRes.json();
    document.getElementById('loading-step').textContent = 'AI 評分中，請稍候…';

    // Step 2: Call OpenClaw AI with the prompt
    const aiRes = await fetch('/ai_grade', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ prompt: gradeData.grading_prompt, meta: gradeData }}),
    }});
    if (!aiRes.ok) {{
      const err = await aiRes.json();
      throw new Error(err.detail || aiRes.statusText);
    }}
    const aiData = await aiRes.json();
    _reportMd = aiData.report;

    clearInterval(stepInterval);
    document.getElementById('loading').style.display = 'none';
    document.getElementById('result-area').style.display = 'block';
    document.getElementById('grade-btn').disabled = false;
    document.getElementById('report-md').textContent = _reportMd;
    document.getElementById('report-html').innerHTML =
      typeof marked !== 'undefined' ? marked.parse(_reportMd) : _reportMd;

  }} catch(e) {{
    clearInterval(stepInterval);
    document.getElementById('loading').style.display = 'none';
    document.getElementById('grade-btn').disabled = false;
    document.getElementById('error-area').innerHTML =
      `<div class="alert alert-error">❌ 評分失敗：${{e.message}}</div>`;
  }}
}}

function copyReport() {{
  navigator.clipboard.writeText(_reportMd).then(() => alert('已複製到剪貼簿！'));
}}

function downloadReport() {{
  const blob = new Blob([_reportMd], {{type:'text/markdown'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'grading-report.md';
  a.click();
}}
</script>
"""
    return HTMLResponse(_base_html(body, "評分 — RAG KB"))


@app.post("/ai_grade")
async def ai_grade(payload: dict):
    """
    Call OpenClaw AI to generate grading report.
    Uses qmd llm or clawdbot to perform AI inference.
    """
    prompt = payload.get("prompt", "")
    if not prompt:
        raise HTTPException(400, "prompt required")

    # Try clawdbot CLI for AI inference
    try:
        result = subprocess.run(
            ["clawdbot", "--no-stream", "--prompt", prompt],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "HOME": os.path.expanduser("~")},
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"report": result.stdout.strip()}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: try qmd llm
    try:
        result = subprocess.run(
            [QMD_BIN, "llm", prompt],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"report": result.stdout.strip()}
    except Exception:
        pass

    # Final fallback: return the prompt context with instructions
    # so the user can paste it to any AI
    meta = payload.get("meta", {})
    return {
        "report": f"""# ⚠️ 自動 AI 評分暫不可用

請將以下評分 Prompt 貼入您慣用的 AI（如 Claude、GPT）進行評分：

---

Repo 資訊：
- Owner: {meta.get('owner','')}
- Repo: {meta.get('repo','')}
- 分支: {meta.get('branch','')}
- 讀取檔案數: {meta.get('code_files_read', 0)} / {meta.get('file_count', 0)}

可用題目：
{chr(10).join('- ' + q for q in meta.get('questions_available', []))}

---

評分 Prompt 已準備好，AI 推論服務暫時離線（clawdbot/qmd llm 未回應）。
請稍後再試，或手動將程式碼貼入 AI 請求評分。
"""
    }
