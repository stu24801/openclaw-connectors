import os
import re

FILE = "/opt/rag-kb/main.py"
with open(FILE, "r") as f:
    content = f.read()

better_body = """    body = f'''
    <div class="card">
        <div style="margin-bottom:24px;border-bottom:1px solid #2d3154;padding-bottom:16px;">
            <h1 style="color:#e2e8f0;margin:0 0 8px 0;">{am['title']}</h1>
            <div style="color:#94a3b8;font-size:0.9rem;">✍️ 作者: {am.get('author','—')} &nbsp;·&nbsp; {am.get('uploaded_at','')}</div>
        </div>
        <div id="rendered"></div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
    <script>
        mermaid.initialize({{startOnLoad:false,theme:'dark'}});
        function renderMermaid(){{
          document.querySelectorAll('code.language-mermaid').forEach(function(code){{
            var pre=code.parentElement;
            if(pre.tagName==='PRE'){{
              var div=document.createElement('div');div.className='mermaid';div.textContent=code.textContent;
              pre.parentElement.replaceChild(div,pre);
            }}
          }});
          mermaid.init(undefined,document.querySelectorAll('.mermaid'));
        }}
        document.getElementById('rendered').innerHTML = marked.parse({content_escaped});
        renderMermaid();
    </script>
    '''
    return HTMLResponse(_share_html(body, am['title']))"""

old_body_pattern = re.compile(r"    body = f'''\n    <div style=\"margin-bottom:24px;.*?    return HTMLResponse\(_share_html\(body, am\['title'\]\)\)", re.DOTALL)
content = old_body_pattern.sub(better_body, content)

with open(FILE, "w") as f:
    f.write(content)
print("Updated shared article UI body")