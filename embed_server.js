/**
 * Lightweight embedding server using QMD's GGUF model via node-llama-cpp.
 * Listens on localhost:8766, POST /embed {"text":"..."} -> {"embedding":[...]}
 */
const http = require('http');
const path = require('path');

const QMD_DIR = path.join(process.env.HOME, '.npm-global/lib/node_modules/@tobilu/qmd');
const MODEL_PATH = path.join(process.env.HOME, '.cache/qmd/models/hf_ggml-org_embeddinggemma-300M-Q8_0.gguf');
const PORT = 8766;

let embeddingCtx = null;

async function loadModel() {
  const { getLlama } = require(path.join(QMD_DIR, 'node_modules/node-llama-cpp'));
  const llama = await getLlama({ gpu: false });
  const model = await llama.loadModel({ modelPath: MODEL_PATH });
  embeddingCtx = await model.createEmbeddingContext();
  console.log('[embed-server] Model loaded, ready on port', PORT);
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
      if (!embeddingCtx) { res.writeHead(503); res.end('Model not ready'); return; }
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
  loadModel().catch(e => { console.error('Failed to load model:', e); process.exit(1); });
});
