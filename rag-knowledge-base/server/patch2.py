import os

FILE = "/home/millalex921/.openclaw/workspace-engineer/rag-knowledge-base/server/main.py"
content = open(FILE, "r").read()

STANDALONE_HTML_FUNC = """
def _share_html(body: str, title: str) -> str:
    return f\"\"\"<!DOCTYPE html>
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
</html>\"\"\"
"""

if "def _share_html" not in content:
    # 注入到 _base_html 下方
    content = content.replace(
        'def _base_html(',
        STANDALONE_HTML_FUNC + '\ndef _base_html('
    )

# 替換 share 路由中的 _base_html
# GET /shared/{share_token}
content = content.replace(
    'return HTMLResponse(_base_html(f"""\n<div style="max-width:400px;margin:100px auto;text-align:center;padding:24px;background:#1e2235;border-radius:12px;border:1px solid #2d3154;">\n    <h2 style="color:#a78bfa;margin-bottom:16px;">《{am[\'title\']}》</h2>',
    'return HTMLResponse(_share_html(f"""\n<div style="max-width:400px;margin:100px auto;text-align:center;padding:24px;background:#1e2235;border-radius:12px;border:1px solid #2d3154;">\n    <h2 style="color:#a78bfa;margin-bottom:16px;">《{am[\'title\']}》</h2>'
)

# POST /shared/{share_token}
content = content.replace(
    'return HTMLResponse(_base_html(f"""\n<div style="max-width:400px;margin:100px auto;text-align:center;padding:24px;background:#1e2235;border-radius:12px;border:1px solid #2d3154;">\n    <h2 style="color:#f87171;margin-bottom:16px;">密碼錯誤</h2>',
    'return HTMLResponse(_share_html(f"""\n<div style="max-width:400px;margin:100px auto;text-align:center;padding:24px;background:#1e2235;border-radius:12px;border:1px solid #2d3154;">\n    <h2 style="color:#f87171;margin-bottom:16px;">密碼錯誤</h2>'
)

# Render article content
content = content.replace(
    "return HTMLResponse(_base_html(body, am['title']))",
    "return HTMLResponse(_share_html(body, am['title']))"
)

with open(FILE, "w") as f:
    f.write(content)

print("HTML isolation applied.")
