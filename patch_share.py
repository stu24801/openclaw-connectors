import os

FILES = [
    "/opt/rag-kb/main.py",
    "/opt/rag-kb/rag-knowledge-base/server/main.py"
]

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
  .btn{{display:inline-block;padding:8px 16px;border-radius:6px;font-size:0.9rem;cursor:pointer;text-align:center;border:none;color:#fff;text-decoration:none;transition:background .2s}}
  .btn-primary{{background:#4f46e5;color:#fff}}
  .btn-primary:hover{{background:#4338ca}}
</style>
</head>
<body>
<div style="padding: 24px; max-width: 800px; margin: 0 auto;">
{body}
</div>
</body>
</html>\"\"\"
"""

for file_path in FILES:
    if not os.path.exists(file_path):
        continue
    content = open(file_path, "r").read()
    
    # 注入 _share_html
    if "def _share_html" not in content:
        content = content.replace(
            'def _base_html(',
            STANDALONE_HTML_FUNC + '\ndef _base_html('
        )
    
    # 取代 GET /share/{article_id} 和 POST /share/{article_id} 內的 _base_html 為 _share_html
    # 分段找，避免取代錯地方
    # 取代：return HTMLResponse(_base_html(body, "文章未分享"))
    content = content.replace(
        'return HTMLResponse(_base_html(body, "文章未分享"))',
        'return HTMLResponse(_share_html(body, "文章未分享"))'
    )
    # 取代：return HTMLResponse(_base_html(body, "解鎖文章"))
    content = content.replace(
        'return HTMLResponse(_base_html(body, "解鎖文章"))',
        'return HTMLResponse(_share_html(body, "解鎖文章"))'
    )
    # 取代：return HTMLResponse(_base_html(body, "文章密碼錯誤"))
    content = content.replace(
        'return HTMLResponse(_base_html(body, "文章密碼錯誤"))',
        'return HTMLResponse(_share_html(body, "文章密碼錯誤"))'
    )
    # 取代：return HTMLResponse(_base_html(body, am['title']))
    # 這個在分享路由內有兩次，一次 GET 一次 POST，都在 share 邏輯下
    # 這裡有點危險，因為 _base_html(body, am['title']) 可能在別的地方也被用
    # 但檢視代碼，/articles/{article_id} 用的是 "文章庫 — RAG KB" 或 am['title'] 呢？
    # 其實 article_view 結尾是 `return HTMLResponse(_base_html(body, "文章庫 — RAG KB"))` 或 `return HTMLResponse(_base_html(body, am['title']))`？
    # 我需要確保只修改 share 的。
    
    lines = content.split('\n')
    in_share_route = False
    for i in range(len(lines)):
        if '@app.get("/share/{article_id}"' in lines[i] or '@app.post("/share/{article_id}"' in lines[i]:
            in_share_route = True
        elif lines[i].startswith('@app.get') or lines[i].startswith('@app.post'):
            # 遇到其他 route 就取消標記 (除了 share route 本身)
            if '/share/' not in lines[i]:
                in_share_route = False
                
        if in_share_route and '_base_html(body' in lines[i]:
            lines[i] = lines[i].replace('_base_html(', '_share_html(')

    new_content = '\n'.join(lines)
    
    with open(file_path, "w") as f:
        f.write(new_content)
    
    print(f"Patched {file_path}")

