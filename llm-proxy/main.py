"""
LLM Proxy — OpenAI-compatible, powered by GitHub Copilot API
Uses the GitHub Copilot token already configured in OpenClaw.
Automatically refreshes the token when expired (reads from openclaw credentials).
No extra API Key needed.
"""

import os, json, time, logging, uuid
from typing import Optional
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
INTERNAL_TOKEN    = os.getenv("PROXY_TOKEN", "internal-change-me")
DEFAULT_MODEL     = os.getenv("DEFAULT_MODEL", "gemini-3.1-pro-preview")
GH_COPILOT_CRED   = os.getenv("GH_COPILOT_CRED",
    "/home/millalex921/.openclaw/credentials/github-copilot.token.json")
OPENCLAW_BIN      = os.getenv("OPENCLAW_BIN",
    "/home/millalex921/.npm-global/bin/openclaw")
COPILOT_API_BASE  = "https://api.githubcopilot.com"
LOG_DIR           = Path(os.getenv("LOG_DIR", "/opt/llm-proxy/logs"))
RAG_ENDPOINT      = os.getenv("RAG_ENDPOINT", "")
LOG_DIR.mkdir(parents=True, exist_ok=True)

COPILOT_HEADERS = {
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version":         "vscode/1.96.0",
    "Editor-Plugin-Version":  "copilot-chat/0.23.1",
    "Content-Type":           "application/json",
}

app = FastAPI(title="LLM Proxy via GitHub Copilot", version="3.0.0")

# ── Token management ──────────────────────────────────────────────────────────
_token_cache: dict = {}

def _load_gh_token() -> str:
    """Load GitHub Copilot token; refresh via openclaw if expired."""
    global _token_cache
    now = time.time()
    
    # Try reading from file
    try:
        cred = json.loads(Path(GH_COPILOT_CRED).read_text())
        expires_at = cred.get("expiresAt", 0) / 1000  # ms → s
        token = cred.get("token", "")
        
        if token and expires_at > now + 60:  # valid for >60s
            return token
    except Exception as e:
        logger.warning(f"Could not read token file: {e}")
    
    # Token expired or missing — refresh via openclaw
    logger.info("Refreshing GitHub Copilot token via openclaw...")
    import subprocess
    try:
        result = subprocess.run(
            [OPENCLAW_BIN, "auth", "refresh", "--provider", "github-copilot"],
            capture_output=True, text=True, timeout=30
        )
        # Re-read after refresh
        cred = json.loads(Path(GH_COPILOT_CRED).read_text())
        token = cred.get("token", "")
        if token:
            logger.info("Token refreshed successfully")
            return token
    except Exception as e:
        logger.error(f"Token refresh failed: {e}")
    
    raise Exception("Cannot obtain valid GitHub Copilot token")

# ── Auth ──────────────────────────────────────────────────────────────────────
def _check_auth(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization")
    if authorization[7:] != INTERNAL_TOKEN:
        raise HTTPException(403, "Invalid token")

# ── RAG injection ─────────────────────────────────────────────────────────────
async def _maybe_inject_rag(messages: list) -> list:
    if not RAG_ENDPOINT:
        return messages
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if not last_user or not isinstance(last_user.get("content"), str):
        return messages
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(RAG_ENDPOINT, params={"q": last_user["content"][:200], "top_k": 3})
            chunks = resp.json().get("results", [])
        if chunks:
            context = "\n\n".join(f"[{c['source']}] {c['text']}" for c in chunks)
            return [{"role": "system", "content": f"知識庫相關內容：\n\n{context}"}] + messages
    except Exception as e:
        logger.warning(f"RAG failed: {e}")
    return messages

# ── Call GitHub Copilot ───────────────────────────────────────────────────────
async def _call_copilot(model: str, messages: list) -> str:
    gh_token = _load_gh_token()
    headers = {**COPILOT_HEADERS, "Authorization": f"Bearer {gh_token}"}
    
    payload = {"model": model, "messages": messages, "stream": False}
    
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{COPILOT_API_BASE}/chat/completions",
            headers=headers,
            json=payload,
        )
        
        if resp.status_code == 401:
            raise Exception("GitHub Copilot token expired or unauthorized")
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

# ── Logging ───────────────────────────────────────────────────────────────────
def _log(req_id, model, messages, response_text, elapsed):
    log_file = LOG_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
    entry = {
        "id": req_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": model, "elapsed_s": round(elapsed, 2),
        "in_chars":  sum(len(m.get("content","")) for m in messages if isinstance(m.get("content"), str)),
        "out_chars": len(response_text),
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

# ── OpenAI-compatible endpoint ────────────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    body     = await request.json()
    model    = body.get("model", DEFAULT_MODEL)
    messages = body.get("messages", [])
    req_id   = str(uuid.uuid4())[:8]

    messages = await _maybe_inject_rag(messages)

    t0 = time.time()
    try:
        text = await _call_copilot(model, messages)
    except Exception as e:
        logger.error(f"[{req_id}] {e}")
        raise HTTPException(502, str(e))

    elapsed = time.time() - t0
    _log(req_id, model, messages, text, elapsed)
    logger.info(f"[{req_id}] model={model} out={len(text)}chars elapsed={elapsed:.1f}s")

    return JSONResponse({
        "id": f"chatcmpl-{req_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1}
    })

@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"object": "list", "data": [
        {"id": "gemini-3.1-pro-preview",  "object": "model"},
        {"id": "gemini-2.5-pro",          "object": "model"},
        {"id": "gemini-3-flash-preview",  "object": "model"},
        {"id": "gpt-5",                   "object": "model"},
        {"id": "claude-sonnet-4-5",       "object": "model"},
    ]}

@app.get("/health")
def health():
    try:
        token = _load_gh_token()
        token_ok = bool(token)
    except:
        token_ok = False
    return {
        "status": "ok",
        "backend":       "github-copilot",
        "default_model": DEFAULT_MODEL,
        "token_valid":   token_ok,
        "rag_enabled":   bool(RAG_ENDPOINT),
    }
