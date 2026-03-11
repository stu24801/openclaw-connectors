"""
LLM Proxy — OpenAI-compatible, multi-backend
Supports two backends:
  1. GitHub Copilot   — models: gemini-*, gpt-*, (auto-selected when model doesn't start with 'claude-')
  2. Anthropic Direct — models: claude-*  (uses ANTHROPIC_API_KEY)

Routing logic (in /v1/chat/completions):
  - model starts with "claude-" → Anthropic API
  - otherwise                   → GitHub Copilot API
"""

import os, json, time, logging, uuid
from typing import Optional
from pathlib import Path

import httpx
import base64
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
def _load_internal_token() -> str:
    """Load gateway auth token from OpenClaw config. Falls back to PROXY_TOKEN env var."""
    openclaw_cfg = Path(os.getenv("OPENCLAW_CONFIG",
        str(Path.home() / ".openclaw" / "openclaw.json")))
    try:
        cfg = json.loads(openclaw_cfg.read_text())
        token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
        if token:
            return token
    except Exception as e:
        logger.warning(f"Could not load openclaw.json: {e}")
    return os.getenv("PROXY_TOKEN", "internal-change-me")

INTERNAL_TOKEN    = _load_internal_token()
DEFAULT_MODEL     = os.getenv("DEFAULT_MODEL", "gemini-3.1-pro-preview")
GOOGLE_API_KEY    = os.getenv("GOOGLE_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
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

app = FastAPI(title="LLM Proxy (GitHub Copilot + Anthropic)", version="4.0.0")

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
    # Re-read token dynamically each time (supports token rotation without restart)
    current_token = _load_internal_token()
    if authorization[7:] != current_token:
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



async def _stream_copilot(model: str, messages: list, req_id: str):
    gh_token = _load_gh_token()
    headers = {**COPILOT_HEADERS, "Authorization": f"Bearer {gh_token}"}
    payload = {"model": model, "messages": messages, "stream": True}
    
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            f"{COPILOT_API_BASE}/chat/completions",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code == 401:
                yield f'data: {{"error": "GitHub Copilot token expired"}}\n\n'
                return
            if not resp.is_success:
                err = await resp.aread()
                yield f'data: {{"error": "GitHub Copilot API error {resp.status_code}"}}\n\n'
                return
            async for chunk in resp.aiter_lines():
                if chunk.strip():
                    yield f"{chunk}\n\n"

async def _stream_anthropic(model: str, messages: list, req_id: str):
    if not ANTHROPIC_API_KEY:
        yield f'data: {{"error": "ANTHROPIC_API_KEY not configured"}}\n\n'
        return

    system_prompt = None
    anthropic_messages = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_prompt = content
        elif role in ("user", "assistant"):
            anthropic_messages.append({"role": role, "content": content})

    if not anthropic_messages:
        anthropic_messages = [{"role": "user", "content": "Hello"}]

    payload = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": 8192,
        "stream": True,
    }
    if system_prompt:
        payload["system"] = system_prompt

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            f"{ANTHROPIC_API_BASE}/messages",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code == 401:
                yield f'data: {{"error": "Anthropic API key invalid"}}\n\n'
                return
            if not resp.is_success:
                err = await resp.aread()
                err_text = err.decode("utf-8") if isinstance(err, bytes) else str(err)
                yield f'data: {{"error": "Anthropic API error {resp.status_code}"}}\n\n'
                return
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]": continue
                    try:
                        data = json.loads(data_str)
                        if data.get("type") == "content_block_delta":
                            text = data["delta"].get("text", "")
                            chunk = {
                                "id": f"chatcmpl-{req_id}",
                                "object": "chat.completion.chunk",
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": text}}]
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                    except Exception as e:
                        pass
            yield "data: [DONE]\n\n"

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

# ── Call Anthropic ───────────────────────────────────────────────────────────
async def _call_anthropic(model: str, messages: list) -> str:
    """Call Anthropic Messages API. Converts OpenAI message format to Anthropic format."""
    if not ANTHROPIC_API_KEY:
        raise Exception("ANTHROPIC_API_KEY not configured in proxy .env")

    # Separate system prompt from user/assistant turns
    system_prompt = None
    anthropic_messages = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_prompt = content
        elif role in ("user", "assistant"):
            anthropic_messages.append({"role": role, "content": content})

    # Ensure conversation starts with "user"
    if not anthropic_messages:
        anthropic_messages = [{"role": "user", "content": "Hello"}]

    payload: dict = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": 8192,
    }
    if system_prompt:
        payload["system"] = system_prompt

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{ANTHROPIC_API_BASE}/messages",
            headers=headers,
            json=payload,
        )
        if resp.status_code == 401:
            raise Exception("Anthropic API key invalid or unauthorized")
        if not resp.is_success:
            raise Exception(f"Anthropic API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data["content"][0]["text"]


def _get_google_api_key(default_key: str) -> str:
    if default_key:
        return default_key
    try:
        import json
        from pathlib import Path
        prof_path = Path("/home/millalex921/.openclaw/agents/main/agent/auth-profiles.json")
        profiles = json.loads(prof_path.read_text())
        return profiles["profiles"]["google:default"]["key"]
    except Exception:
        return ""

async def _call_google(model: str, messages: list) -> str:
    api_key = _get_google_api_key(GOOGLE_API_KEY)
    if not api_key:
        raise Exception("GOOGLE_API_KEY not configured")

    actual_model = model.replace("google/", "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{actual_model}:generateContent?key={api_key}"

    system_prompt = next((m["content"] for m in messages if m["role"] == "system"), None)
    
    contents = []
    for msg in messages:
        if msg.get("role") == "system":
            continue
        role = "user" if msg.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})
        
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Hello"}]}]
    if contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "Hello"}]})
        
    if system_prompt:
        contents[0]["parts"].insert(0, {"text": f"System:\n{system_prompt}\n\n"})
    
    payload = {"contents": contents}
    
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=payload)
        if not resp.is_success:
            raise Exception(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

async def _stream_google(model: str, messages: list, req_id: str):
    api_key = _get_google_api_key(GOOGLE_API_KEY)
    if not api_key:
        yield f'data: {{"error": "GOOGLE_API_KEY not configured"}}\n\n'
        return

    actual_model = model.replace("google/", "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{actual_model}:streamGenerateContent?key={api_key}&alt=sse"

    system_prompt = next((m["content"] for m in messages if m["role"] == "system"), None)
    
    contents = []
    for msg in messages:
        if msg.get("role") == "system":
            continue
        role = "user" if msg.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})
        
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Hello"}]}]
    if contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "Hello"}]})
        
    if system_prompt:
        contents[0]["parts"].insert(0, {"text": f"System:\n{system_prompt}\n\n"})
    
    payload = {"contents": contents}
    
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", url, json=payload) as resp:
            if not resp.is_success:
                err = await resp.aread()
                yield f'data: {{"error": "Gemini API error {resp.status_code}"}}\n\n'
                return
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]": continue
                    try:
                        data = json.loads(data_str)
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        chunk = {
                            "id": f"chatcmpl-{req_id}",
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": text}}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    except Exception as e:
                        pass
            yield "data: [DONE]\n\n"

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
    modalities = body.get("modalities", [])
    req_id   = str(uuid.uuid4())[:8]

    # If image modality requested → route to Gemini instead of Copilot
    if "image" in modalities:
        return await _chat_completions_gemini_image(req_id, model, messages, body)

    # If any message contains image_url → route to Gemini vision (Copilot doesn't support vision)
    has_image_url = any(
        isinstance(msg.get("content"), list) and
        any(p.get("type") == "image_url" for p in msg["content"] if isinstance(p, dict))
        for msg in messages
    )
    if has_image_url:
        return await _chat_completions_gemini_vision(req_id, messages)

    messages = await _maybe_inject_rag(messages)

    # ── Backend routing ──────────────────────────────────────────────────────
    # All LLM requests go through GitHub Copilot — same credential/quota as OpenClaw.
    # Claude models use dot-notation IDs (e.g. claude-sonnet-4.6) which Copilot supports.
    # Google gemini models go directly to Google API.
    is_google = "gemini" in model
    backend = "google" if is_google else "github-copilot"

    logger.info(f"[{req_id}] routing: model={model} backend={backend}")

    if body.get("stream", False):
        logger.info(f"[{req_id}] streaming=True backend={backend} model={model}")
        if backend == "google":
            return StreamingResponse(_stream_google(model, messages, req_id), media_type="text/event-stream")
        else:
            return StreamingResponse(_stream_copilot(model, messages, req_id), media_type="text/event-stream")

    t0 = time.time()
    try:
        if backend == "google":
            text = await _call_google(model, messages)
        else:
            text = await _call_copilot(model, messages)
    except Exception as e:
        logger.error(f"[{req_id}] backend={backend} {e}")
        raise HTTPException(502, str(e))

    elapsed = time.time() - t0
    _log(req_id, model, messages, text, elapsed)
    logger.info(f"[{req_id}] backend={backend} model={model} out={len(text)}chars elapsed={elapsed:.1f}s")

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


async def _chat_completions_gemini_image(req_id: str, model: str, messages: list, body: dict):
    """Handle chat completions with image modality — call Gemini and return image in multi_mod_content format."""
    logger.info(f"[{req_id}] image modality detected, routing to Gemini model={model}")

    # Extract prompt text and any input images from messages
    prompt_parts = []
    input_images_b64 = []

    for msg in messages:
        if msg.get("role") == "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            prompt_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        prompt_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:image"):
                            b64 = url.split(",", 1)[1]
                            input_images_b64.append(b64)

    prompt = "\n".join(p for p in prompt_parts if p)

    # Get aspect ratio from system message if present
    aspect_ratio = "16:9"
    for msg in messages:
        if msg.get("role") == "system":
            txt = msg.get("content", "")
            if "aspect_ratio=" in txt:
                aspect_ratio = txt.split("aspect_ratio=", 1)[1].strip()

    # Map aspect_ratio to width:height
    ratio_map = {"16:9": (1920, 1080), "4:3": (1600, 1200), "1:1": (1024, 1024)}
    w, h = ratio_map.get(aspect_ratio, (1920, 1080))

    t0 = time.time()
    img_bytes = await _call_gemini_image(model, prompt, input_images_b64 or None, w, h)
    elapsed = time.time() - t0

    b64_str = base64.b64encode(img_bytes).decode()
    logger.info(f"[{req_id}] Gemini image generated elapsed={elapsed:.1f}s size={len(img_bytes)}bytes")

    # Return in multi_mod_content format that banana-slides expects
    return JSONResponse({
        "id": f"chatcmpl-{req_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "multi_mod_content": [
                    {"inline_data": {"mime_type": "image/png", "data": b64_str}}
                ]
            },
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1}
    })

@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"object": "list", "data": [
        # Google natively via OpenClaw profile
        {"id": "google/gemini-3-pro-preview", "object": "model", "backend": "google"},
        {"id": "gemini-3.1-pro-preview",      "object": "model", "backend": "google"},
        {"id": "gemini-2.5-pro",              "object": "model", "backend": "google"},
        {"id": "gemini-3-flash-preview",      "object": "model", "backend": "google"},
        # GitHub Copilot backend
        {"id": "gpt-5",                       "object": "model", "backend": "github-copilot"},
        # Anthropic direct backend (verified model IDs via /v1/models)
        {"id": "claude-sonnet-4-6",           "object": "model", "backend": "anthropic"},
        {"id": "claude-opus-4-6",             "object": "model", "backend": "anthropic"},
        {"id": "claude-haiku-4-5-20251001",   "object": "model", "backend": "anthropic"},
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
        "backends": {
            "github-copilot": {"enabled": True, "token_valid": token_ok},
            "anthropic":      {"enabled": bool(ANTHROPIC_API_KEY), "models": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001"]},
        },
        "default_model": DEFAULT_MODEL,
        "routing": "claude-* → Anthropic, others → GitHub Copilot",
        "rag_enabled":   bool(RAG_ENDPOINT),
    }

# ── Gemini vision (text analysis with image input) ────────────────────────────
async def _chat_completions_gemini_vision(req_id: str, messages: list) -> JSONResponse:
    """Route vision requests (image_url content) to Gemini text API."""
    api_key = _get_google_api_key(GOOGLE_API_KEY)
    if not api_key:
        raise HTTPException(502, "No GOOGLE_API_KEY configured for vision")

    vision_model = "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{vision_model}:generateContent?key={api_key}"

    parts = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    parts.append({"text": part["text"]})
                elif part.get("type") == "image_url":
                    img_url = part.get("image_url", {}).get("url", "")
                    if img_url.startswith("data:image"):
                        mime, b64 = img_url.split(";base64,")
                        mime = mime.split("data:")[-1]
                        parts.append({"inline_data": {"mime_type": mime, "data": b64}})

    payload = {"contents": [{"role": "user", "parts": parts}]}

    t0 = time.time()
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload)
        if not resp.is_success:
            logger.error(f"[{req_id}] Gemini vision error {resp.status_code}: {resp.text[:200]}")
            raise HTTPException(502, f"Gemini vision error: {resp.status_code}")
        data = resp.json()

    text = ""
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        text = str(data)

    elapsed = time.time() - t0
    logger.info(f"[{req_id}] gemini-vision out={len(text)}chars elapsed={elapsed:.1f}s")

    return JSONResponse({
        "id": f"chatcmpl-{req_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": vision_model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": len(text) // 4, "total_tokens": len(text) // 4}
    })


# ── Gemini image generation (for banana-slides) ───────────────────────────────
async def _call_gemini_image(model: str, prompt: str, images_b64: list = None,
                              width: int = 1920, height: int = 1080) -> bytes:
    """Call Gemini image generation API, return raw image bytes (PNG)."""
    api_key = _get_google_api_key(GOOGLE_API_KEY)
    if not api_key:
        raise Exception("No GOOGLE_API_KEY configured")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    parts = []
    if images_b64:
        for img_b64 in images_b64:
            parts.append({"inline_data": {"mime_type": "image/png", "data": img_b64}})
    parts.append({"text": prompt})

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
        }
    }

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=payload)
        if not resp.is_success:
            raise Exception(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    # Extract image bytes — API returns camelCase "inlineData"
    for part in data["candidates"][0]["content"]["parts"]:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline:
            return base64.b64decode(inline["data"])

    raise Exception("No image in Gemini response")


@app.post("/v1/images/generations")
async def image_generations(request: Request, authorization: Optional[str] = Header(None)):
    """OpenAI-compatible image generation endpoint backed by Gemini."""
    _check_auth(authorization)
    body   = await request.json()
    model  = body.get("model", "gemini-3-pro-image-preview")
    prompt = body.get("prompt", "")
    n      = body.get("n", 1)
    size   = body.get("size", "1920x1080")
    req_id = str(uuid.uuid4())[:8]

    # Parse input images (OpenAI extension: "image" field as base64 list)
    images_b64 = body.get("images", [])  # banana-slides custom field
    if isinstance(images_b64, str):
        images_b64 = [images_b64]

    try:
        w, h = (int(x) for x in size.split("x")) if "x" in size else (1920, 1080)
    except Exception:
        w, h = 1920, 1080

    t0 = time.time()
    results = []
    errors  = []
    for i in range(max(1, n)):
        try:
            img_bytes = await _call_gemini_image(model, prompt, images_b64, w, h)
            b64_str   = base64.b64encode(img_bytes).decode()
            results.append({"b64_json": b64_str})
        except Exception as e:
            errors.append(str(e))
            logger.error(f"[{req_id}] image gen #{i} failed: {e}")

    elapsed = time.time() - t0
    logger.info(f"[{req_id}] image model={model} generated={len(results)} errors={len(errors)} elapsed={elapsed:.1f}s")

    if not results:
        raise HTTPException(502, f"Image generation failed: {'; '.join(errors)}")

    return JSONResponse({
        "created": int(time.time()),
        "data": results,
    })


@app.post("/v1/images/edits")
async def image_edits(request: Request, authorization: Optional[str] = Header(None)):
    """Image editing — same as generations but expects input image(s)."""
    return await image_generations(request, authorization)

