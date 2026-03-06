/**
 * Lightweight embedding server using QMD's GGUF model via node-llama-cpp.
 * Listens on localhost:8766, POST /embed {"text":"..."} -> {"embedding":[...], "dim": 768}
 */
import http from 'http';
import path from 'path';
import { fileURLToPath } from 'url';

const HOME = process.env.HOME;
const QMD_DIR = path.join(HOME, '.npm-global/lib/node_modules/@tobilu/qmd');
const MODEL_PATH = path.join(HOME, '.cache/qmd/models/hf_ggml-org_embeddinggemma-300M-Q8_0.gguf');
const PORT = 8766;

let embeddingCtx = null;

async function loadModel() {
  const { getLlama } = await import(path.join(QMD_DIR, 'node_modules/node-llama-cpp/dist/index.js'));
  const llama = await getLlama({ gpu: false });
  const model = await llama.loadModel({ modelPath: MODEL_PATH });
  embeddingCtx = await model.createEmbeddingContext();
  console.log('[embed-server] Model loaded ✓, ready on port', PORT);
}

const server = http.createServer(async (req, res) => {
  if (req.method !== 'POST' || req.url !== '/embed') {
    res.writeHead(404); res.end('Not found'); return;
  }
  let body = '';
  req.on('data', d => body += d);
  req.on('end', async () => {
    try {
      const { text } = JSON.parse(body);
      if (!embeddingCtx) { res.writeHead(503); res.end(JSON.stringify({error:'Model not ready'})); return; }
      const result = await embeddingCtx.getEmbeddingFor(text);
      const vec = Array.from(result.vector);
      res.writeHead(200, {'Content-Type':'application/json'});
      res.end(JSON.stringify({ embedding: vec, dim: vec.length }));
    } catch(e) {
      res.writeHead(500); res.end(JSON.stringify({error: e.message}));
    }
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log('[embed-server] Server listening, loading model...');
  loadModel().catch(e => { console.error('Failed to load model:', e); process.exit(1); });
});
