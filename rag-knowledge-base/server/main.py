"""
RAG Knowledge Base Server
A lightweight FastAPI server for document upload, vectorization, and semantic search.
Documents are chunked, embedded (via sentence-transformers), and stored in a local FAISS index.
"""

import os
import uuid
import json
import hashlib
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import JSONResponse
import numpy as np

# Optional heavy deps — imported lazily so server starts even if not yet installed
try:
    from sentence_transformers import SentenceTransformer
    import faiss
    READY = True
except ImportError:
    READY = False

DATA_DIR = Path(os.getenv("RAG_DATA_DIR", "./data"))
INDEX_PATH = DATA_DIR / "index.faiss"
META_PATH  = DATA_DIR / "meta.json"
CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "500"))   # chars per chunk
TOP_K      = int(os.getenv("RAG_TOP_K", "5"))
MODEL_NAME = os.getenv("RAG_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")

app = FastAPI(title="RAG Knowledge Base", version="1.0.0")

# ── Lazy globals ──────────────────────────────────────────────────────────────
_model  = None
_index  = None
_meta: List[dict] = []   # [{id, source, chunk_index, text}, ...]

def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model

def _load_store():
    global _index, _meta
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if META_PATH.exists():
        _meta = json.loads(META_PATH.read_text())
    else:
        _meta = []
    if INDEX_PATH.exists() and _meta:
        _index = faiss.read_index(str(INDEX_PATH))
    else:
        _index = None

def _save_store():
    META_PATH.write_text(json.dumps(_meta, ensure_ascii=False, indent=2))
    if _index is not None:
        faiss.write_index(_index, str(INDEX_PATH))

def _chunk_text(text: str, size: int = CHUNK_SIZE) -> List[str]:
    """Split text into overlapping chunks."""
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i + size])
        i += size - 100  # 100-char overlap
    return [c for c in chunks if c.strip()]

def _add_chunks(source: str, chunks: List[str]):
    global _index, _meta
    model = _get_model()
    vecs = model.encode(chunks, normalize_embeddings=True).astype("float32")
    dim  = vecs.shape[1]
    if _index is None:
        _index = faiss.IndexFlatIP(dim)   # inner-product == cosine for unit vecs
    _index.add(vecs)
    for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
        _meta.append({
            "id": str(uuid.uuid4()),
            "source": source,
            "chunk_index": i,
            "text": chunk,
        })
    _save_store()


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup():
    _load_store()


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "ready": READY,
        "doc_count": len(_meta),
        "sources": list({m["source"] for m in _meta}),
    }


# ── Upload ────────────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    source_name: Optional[str] = Form(None),
):
    if not READY:
        raise HTTPException(503, "Dependencies not installed. Run: pip install -r requirements.txt")
    raw = await file.read()
    # Decode — support txt, md; PDF support via optional dependency
    if file.filename and file.filename.lower().endswith(".pdf"):
        try:
            import pdfplumber
            import io
            text_parts = []
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page in pdf.pages:
                    text_parts.append(page.extract_text() or "")
            text = "\n".join(text_parts)
        except ImportError:
            raise HTTPException(400, "PDF support requires pdfplumber. Run: pip install pdfplumber")
    else:
        text = raw.decode("utf-8", errors="replace")

    name = source_name or file.filename or hashlib.md5(raw).hexdigest()[:8]
    chunks = _chunk_text(text)
    if not chunks:
        raise HTTPException(400, "No text content found in file.")
    _add_chunks(name, chunks)
    return {"source": name, "chunks_added": len(chunks), "total_docs": len(_meta)}


# ── Plain-text upload ─────────────────────────────────────────────────────────
@app.post("/upload_text")
async def upload_text(payload: dict):
    """
    Body: { "text": "...", "source": "my-doc" }
    Useful when calling from AI without multipart.
    """
    if not READY:
        raise HTTPException(503, "Dependencies not installed.")
    text   = payload.get("text", "")
    source = payload.get("source", "inline-" + str(uuid.uuid4())[:8])
    if not text.strip():
        raise HTTPException(400, "text field is empty")
    chunks = _chunk_text(text)
    _add_chunks(source, chunks)
    return {"source": source, "chunks_added": len(chunks), "total_docs": len(_meta)}


# ── Search ────────────────────────────────────────────────────────────────────
@app.get("/search")
def search(q: str, top_k: int = TOP_K, source: Optional[str] = None):
    if not READY:
        raise HTTPException(503, "Dependencies not installed.")
    if _index is None or not _meta:
        return {"results": [], "note": "Knowledge base is empty. Upload documents first."}
    model = _get_model()
    vec   = model.encode([q], normalize_embeddings=True).astype("float32")
    k     = min(top_k * 3, len(_meta))   # over-fetch then filter
    scores, indices = _index.search(vec, k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        m = _meta[idx]
        if source and m["source"] != source:
            continue
        results.append({
            "score": float(score),
            "source": m["source"],
            "chunk_index": m["chunk_index"],
            "text": m["text"],
        })
        if len(results) >= top_k:
            break
    return {"query": q, "results": results}


# ── List sources ──────────────────────────────────────────────────────────────
@app.get("/sources")
def list_sources():
    from collections import Counter
    c = Counter(m["source"] for m in _meta)
    return {"sources": [{"name": k, "chunks": v} for k, v in c.items()]}


# ── Delete a source ───────────────────────────────────────────────────────────
@app.delete("/source/{source_name}")
def delete_source(source_name: str):
    global _index, _meta
    before = len(_meta)
    _meta = [m for m in _meta if m["source"] != source_name]
    if len(_meta) == before:
        raise HTTPException(404, f"Source '{source_name}' not found.")
    # Rebuild index from remaining entries
    if _meta:
        model = _get_model()
        texts = [m["text"] for m in _meta]
        vecs  = model.encode(texts, normalize_embeddings=True).astype("float32")
        _index = faiss.IndexFlatIP(vecs.shape[1])
        _index.add(vecs)
    else:
        _index = None
    _save_store()
    return {"deleted_source": source_name, "remaining_docs": len(_meta)}
