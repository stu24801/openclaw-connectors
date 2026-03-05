"""
RAG Knowledge Base Server — with Web UI + Password Auth
"""

import os, uuid, json, hashlib, secrets, io
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Cookie, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import numpy as np

try:
    from sentence_transformers import SentenceTransformer
    import faiss
    READY = True
except ImportError:
    READY = False

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR    = Path(os.getenv("RAG_DATA_DIR", "./data"))
FILES_DIR   = DATA_DIR / "files"          # uploaded raw files
INDEX_PATH  = DATA_DIR / "index.faiss"
META_PATH   = DATA_DIR / "meta.json"
FILEMETA_PATH = DATA_DIR / "filemeta.json"
CHUNK_SIZE  = int(os.getenv("RAG_CHUNK_SIZE", "500"))
TOP_K       = int(os.getenv("RAG_TOP_K", "5"))
MODEL_NAME  = os.getenv("RAG_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
PASSWORD    = os.getenv("RAG_PASSWORD", "changeme")   # ← set via env

# ── In-memory session store ───────────────────────────────────────────────────
_sessions: set = set()   # valid session tokens

app = FastAPI(title="RAG Knowledge Base")

# ── Lazy globals ──────────────────────────────────────────────────────────────
_model = None
_index = None
_meta: List[dict] = []
_filemeta: List[dict] = []   # [{id, filename, source_name, size, uploaded_at}, ...]

def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model

def _load_store():
    global _index, _meta, _filemeta
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    _meta     = json.loads(META_PATH.read_text())     if META_PATH.exists()     else []
    _filemeta = json.loads(FILEMETA_PATH.read_text()) if FILEMETA_PATH.exists() else []
    if INDEX_PATH.exists() and _meta:
        _index = faiss.read_index(str(INDEX_PATH))

def _save_store():
    META_PATH.write_text(json.dumps(_meta, ensure_ascii=False, indent=2))
    FILEMETA_PATH.write_text(json.dumps(_filemeta, ensure_ascii=False, indent=2))
    if _index is not None:
        faiss.write_index(_index, str(INDEX_PATH))

def _chunk_text(text: str) -> List[str]:
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i + CHUNK_SIZE])
        i += CHUNK_SIZE - 100
    return [c for c in chunks if c.strip()]

def _add_chunks(source: str, chunks: List[str]):
    global _index, _meta
    model = _get_model()
    vecs  = model.encode(chunks, normalize_embeddings=True).astype("float32")
    if _index is None:
        _index = faiss.IndexFlatIP(vecs.shape[1])
    _index.add(vecs)
    for i, chunk in enumerate(chunks):
        _meta.append({"id": str(uuid.uuid4()), "source": source, "chunk_index": i, "text": chunk})
    _save_store()

def _auth(token: Optional[str]) -> bool:
    return token is not None and token in _sessions

@app.on_event("startup")
def startup():
    _load_store()

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

    # stats
    from collections import Counter
    src_counts = Counter(m["source"] for m in _meta)
    doc_count  = len(_filemeta)
    chunk_count = len(_meta)
    src_count  = len(src_counts)

    ok_html = f'<div class="alert alert-success">✅ {msg}</div>' if msg else ""

    # file table
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
  <div class="stat"><span class="stat-val">{chunk_count}</span> 個向量段落</div>
  <div class="stat"><span class="stat-val">{src_count}</span> 個來源</div>
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

    # Decode text
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

    # Save raw file
    file_id  = str(uuid.uuid4())
    save_ext = Path(file.filename).suffix if file.filename else ".txt"
    save_path = FILES_DIR / f"{file_id}{save_ext}"
    save_path.write_bytes(raw)

    _filemeta.append({
        "id": file_id,
        "filename": file.filename or "unnamed",
        "source_name": name,
        "size": len(raw),
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "path": str(save_path),
        "ext": save_ext,
    })

    # Vectorize
    if READY:
        chunks = _chunk_text(text)
        if chunks:
            _add_chunks(name, chunks)
    else:
        _save_store()

    return RedirectResponse(f"/dashboard?msg=上傳成功：{file.filename}（{len(text)}字）", status_code=303)


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
    global _index, _meta, _filemeta
    if not _auth(rag_token):
        return RedirectResponse("/login")
    fm = next((f for f in _filemeta if f["id"] == file_id), None)
    if not fm:
        raise HTTPException(404, "File not found")

    # Delete raw file
    p = Path(fm["path"])
    if p.exists():
        p.unlink()

    # Remove from filemeta
    _filemeta = [f for f in _filemeta if f["id"] != file_id]

    # Remove chunks from vector store and rebuild
    source = fm["source_name"]
    _meta = [m for m in _meta if m["source"] != source]
    if _meta and READY:
        model = _get_model()
        vecs  = model.encode([m["text"] for m in _meta], normalize_embeddings=True).astype("float32")
        _index = faiss.IndexFlatIP(vecs.shape[1])
        _index.add(vecs)
    else:
        _index = None
    _save_store()

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
        if not READY or _index is None:
            results_html = '<div class="alert alert-error">知識庫為空或向量模組未安裝。</div>'
        else:
            model = _get_model()
            vec   = model.encode([q], normalize_embeddings=True).astype("float32")
            k     = min(TOP_K * 3, len(_meta))
            scores, indices = _index.search(vec, k)
            rows = ""
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0: continue
                m = _meta[idx]
                preview = m["text"][:300].replace("<","&lt;").replace(">","&gt;")
                rows += f"""<tr>
  <td style="color:#a78bfa;font-weight:600">{score:.3f}</td>
  <td><span class="badge badge-purple">{m['source']}</span></td>
  <td style="white-space:pre-wrap;font-size:.8rem;color:#cbd5e1">{preview}…</td>
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
    return {"status":"ok","ready":READY,"doc_count":len(_meta),"sources":list({m["source"] for m in _meta})}

@app.get("/search")
def search_api(q: str, top_k: int = TOP_K, source: Optional[str] = None):
    if not READY or _index is None:
        return {"results":[], "note":"Knowledge base empty."}
    model = _get_model()
    vec   = model.encode([q], normalize_embeddings=True).astype("float32")
    k     = min(top_k*3, len(_meta))
    scores, indices = _index.search(vec, k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0: continue
        m = _meta[idx]
        if source and m["source"] != source: continue
        results.append({"score":float(score),"source":m["source"],"chunk_index":m["chunk_index"],"text":m["text"]})
        if len(results) >= top_k: break
    return {"query":q,"results":results}

@app.post("/upload_text")
async def upload_text_api(payload: dict):
    text   = payload.get("text","")
    source = payload.get("source","inline-"+str(uuid.uuid4())[:8])
    if not text.strip(): raise HTTPException(400,"text is empty")
    chunks = _chunk_text(text)
    _add_chunks(source, chunks)
    return {"source":source,"chunks_added":len(chunks),"total_docs":len(_meta)}

@app.get("/sources")
def list_sources():
    from collections import Counter
    c = Counter(m["source"] for m in _meta)
    return {"sources":[{"name":k,"chunks":v} for k,v in c.items()]}
