"""
RAG Knowledge Base Server — QMD-backed (sqlite-vec + GGUF embeddings)
No sentence-transformers or faiss required.
"""

import os, uuid, json, hashlib, secrets, io, subprocess, shutil, re
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Cookie, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import numpy as np

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR      = Path(os.getenv("RAG_DATA_DIR", "./data"))
FILES_DIR     = DATA_DIR / "files"
KB_DOCS_DIR   = DATA_DIR / "kb_docs"          # QMD collection dir (markdown files)
FILEMETA_PATH = DATA_DIR / "filemeta.json"
QMD_BIN       = os.getenv("QMD_BIN", "qmd")   # path to qmd CLI
QMD_COLL      = "rag-kb"
TOP_K         = int(os.getenv("RAG_TOP_K", "5"))
PASSWORD      = os.getenv("RAG_PASSWORD", "changeme")

# ── In-memory session store ────────────────────────────────────────────────────
_sessions: set = set()

app = FastAPI(title="RAG Knowledge Base")

# ── Filemeta helpers ──────────────────────────────────────────────────────────
_filemeta: List[dict] = []

def _load_filemeta():
    global _filemeta
    _filemeta = json.loads(FILEMETA_PATH.read_text()) if FILEMETA_PATH.exists() else []

def _save_filemeta():
    FILEMETA_PATH.write_text(json.dumps(_filemeta, ensure_ascii=False, indent=2))

# ── QMD helpers ───────────────────────────────────────────────────────────────

def _qmd_update_embed():
    """Re-index and embed the rag-kb collection (best-effort, background ok)."""
    try:
        subprocess.run([QMD_BIN, "update"], capture_output=True, timeout=120)
        subprocess.run([QMD_BIN, "embed", "-f"], capture_output=True, timeout=300)
    except Exception as e:
        print(f"[qmd embed] warning: {e}")

def _qmd_vsearch(query: str, top_k: int = TOP_K) -> List[dict]:
    """Run qmd vsearch --json and return list of results."""
    try:
        result = subprocess.run(
            [QMD_BIN, "vsearch", query, "--json", "-n", str(top_k * 3), f"qmd://{QMD_COLL}/"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = json.loads(result.stdout.strip())
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[qmd vsearch] error: {e}")
        return []

def _chunk_text(text: str, chunk_size: int = 900, overlap: int = 135) -> List[str]:
    """Split text into overlapping chunks."""
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i + chunk_size])
        i += chunk_size - overlap
    return [c for c in chunks if c.strip()]

def _write_doc_to_kb(file_id: str, filename: str, source_name: str, text: str):
    """Write document as markdown files in KB_DOCS_DIR for QMD indexing."""
    doc_dir = KB_DOCS_DIR / file_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    chunks = _chunk_text(text)
    for i, chunk in enumerate(chunks):
        md_path = doc_dir / f"chunk_{i:04d}.md"
        md_path.write_text(
            f"---\nsource: {source_name}\nfilename: {filename}\nfile_id: {file_id}\nchunk: {i}\n---\n\n{chunk}",
            encoding="utf-8"
        )

def _remove_doc_from_kb(file_id: str):
    """Remove document chunks from KB_DOCS_DIR."""
    doc_dir = KB_DOCS_DIR / file_id
    if doc_dir.exists():
        shutil.rmtree(doc_dir)

def _auth(token: Optional[str]) -> bool:
    return token is not None and token in _sessions

@app.on_event("startup")
def startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    KB_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    _load_filemeta()

# ═══════════════════════════════════════════════════════════════════════════════
#  HTML helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _base_html(body: str, title="RAG Knowledge Base") -> str:
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
  .topbar span{{font-size:.8rem;color:#64748b;margin-left:auto}}
  .topbar a{{color:#94a3b8;font-size:.85rem;text-decoration:none}}
  .topbar a:hover{{color:#a78bfa}}
  .container{{max-width:860px;margin:40px auto;padding:0 24px}}
  .card{{background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;padding:28px;margin-bottom:24px}}
  .card h2{{font-size:1rem;font-weight:600;color:#a78bfa;margin-bottom:18px;display:flex;align-items:center;gap:8px}}
  label{{font-size:.85rem;color:#94a3b8;display:block;margin-bottom:6px}}
  input[type=text],input[type=password],input[type=file]{{
    width:100%;padding:10px 14px;background:#0f1117;border:1px solid #2d3154;
    border-radius:8px;color:#e2e8f0;font-size:.9rem;outline:none;transition:.2s
  }}
  input:focus{{border-color:#a78bfa}}
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
  .empty{{color:#4b5563;text-align:center;padding:32px;font-size:.9rem}}
  .form-row{{display:flex;gap:12px;align-items:flex-end}}
  .form-row>*{{flex:1}}
  .form-row .btn{{flex:0 0 auto}}
  .stat{{display:inline-flex;align-items:center;gap:6px;background:#0f1117;
    border:1px solid #2d3154;border-radius:8px;padding:8px 16px;font-size:.85rem}}
  .stat-val{{font-size:1.2rem;font-weight:700;color:#a78bfa}}
  .stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}}
</style>
</head>
<body>
<div class="topbar">
  <h1>🦐 RAG Knowledge Base</h1>
  <a href="/slides/" target="_blank" style="margin-left:auto;background:#3b0764;color:#c4b5fd;padding:6px 14px;border-radius:8px;font-size:.82rem;font-weight:500;border:1px solid #6d28d9">🍌 Banana Slides</a>
  <a href="/logout">登出</a>
</div>
<div class="container">{body}</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
#  Auth routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def root(rag_token: Optional[str] = Cookie(None)):
    if _auth(rag_token):
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")

@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    err_html = f'<div class="alert alert-error">密碼錯誤，請再試一次。</div>' if error else ""
    body = f"""
<div style="max-width:400px;margin:80px auto">
<div class="card">
  <h2>🔐 登入</h2>
  {err_html}
  <form method="post" action="/login">
    <label>存取密碼</label>
    <input type="password" name="password" autofocus style="margin-bottom:16px">
    <button class="btn" type="submit" style="width:100%">確認登入</button>
  </form>
</div>
</div>"""
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
def dashboard(rag_token: Optional[str] = Cookie(None), msg: str = ""):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    doc_count   = len(_filemeta)
    chunk_count = sum(len(list((KB_DOCS_DIR / f["id"]).glob("chunk_*.md"))) for f in _filemeta if (KB_DOCS_DIR / f["id"]).exists())
    ok_html = f'<div class="alert alert-success">✅ {msg}</div>' if msg else ""

    if _filemeta:
        rows = ""
        for f in reversed(_filemeta):
            size_kb = f.get("size", 0) // 1024
            rows += f"""<tr>
  <td>{f['filename']}</td>
  <td><span class="badge badge-purple">{f.get('source_name','—')}</span></td>
  <td>{size_kb} KB</td>
  <td style="color:#64748b">{f.get('uploaded_at','')}</td>
  <td>
    <a href="/download/{f['id']}" class="btn btn-sm btn-ghost">⬇ 下載</a>
    &nbsp;
    <form method="post" action="/delete/{f['id']}" style="display:inline"
          onsubmit="return confirm('確定刪除？')">
      <button class="btn btn-sm btn-danger">🗑</button>
    </form>
  </td>
</tr>"""
        file_table = f"""<table>
<thead><tr><th>檔名</th><th>來源標籤</th><th>大小</th><th>上傳時間</th><th>操作</th></tr></thead>
<tbody>{rows}</tbody></table>"""
    else:
        file_table = '<div class="empty">尚未上傳任何文件</div>'

    body = f"""
{ok_html}
<div class="stats">
  <div class="stat"><span class="stat-val">{doc_count}</span> 份文件</div>
  <div class="stat"><span class="stat-val">{chunk_count}</span> 個段落</div>
</div>

<div class="card">
  <h2>📤 上傳文件</h2>
  <form method="post" action="/upload_form" enctype="multipart/form-data">
    <div class="form-row" style="margin-bottom:14px">
      <div>
        <label>選擇檔案（.txt / .md / .pdf）</label>
        <input type="file" name="file" accept=".txt,.md,.pdf" required>
      </div>
      <div>
        <label>來源標籤（選填）</label>
        <input type="text" name="source_name" placeholder="e.g. 產品手冊">
      </div>
    </div>
    <button class="btn" type="submit">上傳並向量化</button>
  </form>
</div>

<div class="card">
  <h2>📋 文件列表</h2>
  {file_table}
</div>

<div class="card">
  <h2>🔍 快速搜尋</h2>
  <form method="get" action="/search_ui">
    <div class="form-row">
      <input type="text" name="q" placeholder="輸入查詢關鍵字…" required>
      <button class="btn" type="submit">搜尋</button>
    </div>
  </form>
</div>
"""
    return HTMLResponse(_base_html(body))


# ═══════════════════════════════════════════════════════════════════════════════
#  Upload (form)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/upload_form")
async def upload_form(
    rag_token: Optional[str] = Cookie(None),
    file: UploadFile = File(...),
    source_name: Optional[str] = Form(None),
):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    raw = await file.read()
    name = source_name.strip() if source_name and source_name.strip() else (file.filename or "unnamed")

    if file.filename and file.filename.lower().endswith(".pdf"):
        try:
            import pdfplumber
            text_parts = []
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page in pdf.pages:
                    text_parts.append(page.extract_text() or "")
            text = "\n".join(text_parts)
        except ImportError:
            return RedirectResponse("/dashboard?msg=PDF+支援需安裝+pdfplumber", status_code=303)
    else:
        text = raw.decode("utf-8", errors="replace")

    file_id  = str(uuid.uuid4())
    save_ext = Path(file.filename).suffix if file.filename else ".txt"
    save_path = FILES_DIR / f"{file_id}{save_ext}"
    save_path.write_bytes(raw)

    _write_doc_to_kb(file_id, file.filename or "unnamed", name, text)

    _filemeta.append({
        "id": file_id,
        "filename": file.filename or "unnamed",
        "source_name": name,
        "size": len(raw),
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
        "ext": save_ext,
    })
    _save_filemeta()

    # Update QMD index in background
    import threading
    threading.Thread(target=_qmd_update_embed, daemon=True).start()

    return RedirectResponse(f"/dashboard?msg=上傳成功：{file.filename}（{len(text)}字）已排入向量化", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
#  Download
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/download/{file_id}")
def download(file_id: str, rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")
    fm = next((f for f in _filemeta if f["id"] == file_id), None)
    if not fm:
        raise HTTPException(404, "File not found")
    path = Path(fm["path"])
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(str(path), filename=fm["filename"])


# ═══════════════════════════════════════════════════════════════════════════════
#  Delete
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/delete/{file_id}")
def delete_file(file_id: str, rag_token: Optional[str] = Cookie(None)):
    global _filemeta
    if not _auth(rag_token):
        return RedirectResponse("/login")
    fm = next((f for f in _filemeta if f["id"] == file_id), None)
    if not fm:
        raise HTTPException(404, "File not found")

    p = Path(fm["path"])
    if p.exists():
        p.unlink()

    _remove_doc_from_kb(file_id)
    _filemeta = [f for f in _filemeta if f["id"] != file_id]
    _save_filemeta()

    import threading
    threading.Thread(target=_qmd_update_embed, daemon=True).start()

    return RedirectResponse(f"/dashboard?msg=已刪除：{fm['filename']}", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
#  Search UI
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/search_ui", response_class=HTMLResponse)
def search_ui(q: str = "", rag_token: Optional[str] = Cookie(None)):
    if not _auth(rag_token):
        return RedirectResponse("/login")

    results_html = ""
    if q:
        raw_results = _qmd_vsearch(q, TOP_K)
        if not raw_results:
            results_html = '<div class="alert alert-error">知識庫為空或尚未建立向量索引，請上傳文件後稍候再試。</div>'
        else:
            rows = ""
            for r in raw_results[:TOP_K]:
                score = r.get("score", 0)
                source = r.get("title", r.get("file", "—"))
                body_text = (r.get("body") or r.get("snippet") or "")[:300]
                body_text = body_text.replace("<","&lt;").replace(">","&gt;")
                rows += f"""<tr>
  <td style="color:#a78bfa;font-weight:600">{score:.3f}</td>
  <td><span class="badge badge-purple">{source}</span></td>
  <td style="white-space:pre-wrap;font-size:.8rem;color:#cbd5e1">{body_text}…</td>
</tr>"""
            if rows:
                results_html = f"""<table>
<thead><tr><th>相關度</th><th>來源</th><th>內容片段</th></tr></thead>
<tbody>{rows}</tbody></table>"""
            else:
                results_html = '<div class="empty">沒有找到相關段落。</div>'

    body = f"""
<div class="card">
  <h2>🔍 語意搜尋</h2>
  <form method="get" action="/search_ui" style="margin-bottom:20px">
    <div class="form-row">
      <input type="text" name="q" value="{q}" placeholder="輸入查詢關鍵字…" autofocus>
      <button class="btn" type="submit">搜尋</button>
      <a href="/dashboard" class="btn btn-ghost">← 返回</a>
    </div>
  </form>
  {results_html}
</div>"""
    return HTMLResponse(_base_html(body, "搜尋 — RAG KB"))


# ═══════════════════════════════════════════════════════════════════════════════
#  JSON API (for AI use)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    doc_count = len(_filemeta)
    return {"status": "ok", "backend": "qmd+sqlite-vec", "doc_count": doc_count}

@app.get("/search")
def search_api(q: str, top_k: int = TOP_K, source: Optional[str] = None):
    results = _qmd_vsearch(q, top_k)
    if source:
        results = [r for r in results if source.lower() in (r.get("title","") + r.get("file","")).lower()]
    return {"query": q, "results": results[:top_k]}

@app.post("/upload_text")
async def upload_text_api(payload: dict):
    text   = payload.get("text", "")
    source = payload.get("source", "inline-" + str(uuid.uuid4())[:8])
    if not text.strip():
        raise HTTPException(400, "text is empty")

    file_id = str(uuid.uuid4())
    save_path = FILES_DIR / f"{file_id}.txt"
    save_path.write_text(text, encoding="utf-8")
    _write_doc_to_kb(file_id, f"{source}.txt", source, text)
    _filemeta.append({
        "id": file_id,
        "filename": f"{source}.txt",
        "source_name": source,
        "size": len(text.encode()),
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
        "ext": ".txt",
    })
    _save_filemeta()

    import threading
    threading.Thread(target=_qmd_update_embed, daemon=True).start()

    chunks_written = len(list((KB_DOCS_DIR / file_id).glob("chunk_*.md")))
    return {"source": source, "chunks_added": chunks_written, "total_docs": len(_filemeta)}

@app.get("/sources")
def list_sources():
    sources = [{"name": f["source_name"], "filename": f["filename"]} for f in _filemeta]
    return {"sources": sources}
