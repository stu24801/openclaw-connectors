import os

FILE = "/opt/rag-kb/main.py"

with open(FILE, "r") as f:
    content = f.read()

# We want to keep all the beautiful CSS of _base_html, but just not render the topbar and the sidebar in the <body>
# So _share_html will just be a simplified version.

new_share_html = '''def _share_html(body: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}}
  .container{{max-width:1100px;margin:32px auto;padding:0 16px}}
  .card{{background:#1a1d2e;border:1px solid #2d3154;border-radius:12px;padding:24px;margin-bottom:24px}}
  .card h2{{font-size:1rem;font-weight:600;color:#a78bfa;margin-bottom:18px;display:flex;align-items:center;gap:8px}}
  label{{font-size:.9rem;color:#94a3b8;display:block;margin-bottom:6px}}
  input[type=text],input[type=password],input[type=file],select,textarea{{
    width:100%;padding:12px 14px;background:#0f1117;border:1px solid #2d3154;
    border-radius:8px;color:#e2e8f0;font-size:16px;outline:none;transition:.2s
  }}
  select option{{background:#1a1d2e}}
  input:focus,select:focus,textarea:focus{{border-color:#a78bfa}}
  .btn{{display:inline-flex;align-items:center;justify-content:center;padding:12px 22px;background:#7c3aed;color:#fff;
    border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:500;transition:.2s;min-height:44px;min-width:44px}}
  .btn:hover{{background:#6d28d9}}
  .btn-sm{{padding:8px 14px;font-size:.82rem;border-radius:6px;min-height:44px}}
  .btn-danger{{background:#dc2626}}.btn-danger:hover{{background:#b91c1c}}
  .btn-ghost{{background:transparent;border:1px solid #374151;color:#94a3b8}}
  .btn-ghost:hover{{background:#1f2937;color:#e2e8f0}}
  .alert{{padding:12px 16px;border-radius:8px;font-size:.85rem;margin-bottom:16px}}
  .alert-error{{background:#450a0a;border:1px solid #7f1d1d;color:#fca5a5}}
  .alert-success{{background:#052e16;border:1px solid #14532d;color:#86efac}}
  .table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem;min-width:480px}}
  th{{text-align:left;padding:10px 12px;background:#0f1117;color:#64748b;font-weight:500;border-bottom:1px solid #2d3154}}
  td{{padding:10px 12px;border-bottom:1px solid #1e2235;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#1e2235}}
  .badge{{display:inline-block;padding:3px 8px;border-radius:99px;font-size:.75rem;font-weight:500}}
  .badge-purple{{background:#3b0764;color:#c4b5fd}}
  .badge-blue{{background:#1e3a5f;color:#93c5fd}}
  .badge-green{{background:#052e16;color:#86efac}}
  .empty{{color:#4b5563;text-align:center;padding:32px;font-size:.9rem}}
  .form-row{{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}}
  .form-row>*{{flex:1;min-width:140px}}
  .form-row .btn{{flex:0 0 auto}}
  .stat{{display:inline-flex;align-items:center;gap:6px;background:#0f1117;
    border:1px solid #2d3154;border-radius:8px;padding:8px 16px;font-size:.85rem}}
  .stat-val{{font-size:1.2rem;font-weight:700;color:#a78bfa}}
  .article-card{{display:none}}
  @media(max-width:768px){{
    .desktop-only{{display:none}}
    .article-card{{display:block;background:#1a1d2e;border:1px solid #2d3154;border-radius:10px;padding:16px;margin-bottom:12px}}
    .article-card-title{{color:#a78bfa;font-size:.95rem;font-weight:600;margin-bottom:8px;text-decoration:none;display:block}}
    .article-card-meta{{color:#64748b;font-size:.8rem;margin-bottom:12px}}
    .article-card-actions{{display:flex;gap:8px;flex-wrap:wrap}}
    .layout-flex{{flex-direction:column;padding:0}}
    .sidebar-panel{{display:none;width:100%;margin-bottom:24px;border-right:none;padding-right:0}}
    .sidebar-panel.active{{display:block}}
    .content-wrap{{width:100%;padding:0 16px}}
    #art-wrap{{padding:16px 16px 40px 16px!important}}
    #chat-panel{{position:static!important;width:100%!important;height:400px!important;border-left:none!important;border-top:1px solid #2d3154;margin-top:24px}}
    #toc-panel{{display:none}}
    .container{{padding:0}}
  }}
  @media(min-width:769px){{
    .layout-flex{{display:flex;gap:24px}}
    .sidebar-panel{{width:220px;flex-shrink:0}}
    .content-wrap{{flex:1;min-width:0}}
  }}
  #rendered{{line-height:1.8;color:#cbd5e1;font-size:.95rem;word-break:break-word;overflow-wrap:break-word;padding:12px 0;}}
  #rendered h1,#rendered h2,#rendered h3{{color:#a78bfa;margin:1.2em 0 .5em;word-break:break-word}}
  #rendered p{{margin-bottom:.9em}}
  #rendered code{{background:#0f1117;padding:2px 6px;border-radius:4px;font-size:.85em;color:#86efac;word-break:break-all}}
  #rendered pre{{background:#0f1117;border:1px solid #2d3154;border-radius:8px;padding:14px;overflow-x:auto;margin-bottom:1em;max-width:100%}}
  #rendered pre code{{background:none;padding:0;word-break:normal}}
  #rendered blockquote{{border-left:3px solid #a78bfa;padding-left:14px;color:#94a3b8;margin-bottom:1em}}
  #rendered a{{color:#60a5fa;word-break:break-all}}
  #rendered ul,#rendered ol{{padding-left:1.5em;margin-bottom:.9em}}
  #rendered img{{max-width:100%;height:auto;border-radius:8px;display:block}}
  #rendered hr{{border:none;border-top:1px solid #2d3154;margin:1.5em 0}}
  #rendered table{{width:100%;border-collapse:collapse;font-size:.85rem;display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}}
  #rendered th{{text-align:left;padding:8px 10px;background:#0f1117;color:#64748b;border-bottom:1px solid #2d3154}}
  #rendered td{{padding:8px 10px;border-bottom:1px solid #1e2235;vertical-align:top}}
</style>
</head>
<body>
<div class="container">
{body}
</div>
</body>
</html>\"\"\"'''

# 找出原有的 _share_html 並取代掉
import re
pattern = re.compile(r'def _share_html\(.*?</html>"""', re.DOTALL)
content = pattern.sub(new_share_html, content)

with open(FILE, "w") as f:
    f.write(content)

print("Updated _share_html CSS.")
