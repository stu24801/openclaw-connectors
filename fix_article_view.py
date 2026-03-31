import re

FILE = "/opt/rag-kb/main.py"
with open(FILE, "r") as f:
    content = f.read()

JS_CODE = """
<script>
async function shareArticle(id) {
    const pwd = prompt("設定分享密碼 (留空則不加密):");
    if (pwd === null) return;
    const res = await fetch(`/articles/${id}/share`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({password: pwd})
    });
    const data = await res.json();
    if (data.url) {
        const fullUrl = window.location.origin + data.url;
        prompt("分享網址已產生！", fullUrl);
    } else {
        alert("分享失敗");
    }
}
</script>
"""

# Let's ensure the JS is correctly placed at the end of article_view body.
# We will find `return HTMLResponse(_base_html(body, f"{am['title']} — 文章庫"))`
# And replace it.
content = content.replace("return HTMLResponse(_base_html(body, f\"{am['title']} — 文章庫\"))",
                          "body += \"\"\"" + JS_CODE + "\"\"\"\n    return HTMLResponse(_base_html(body, f\"{am['title']} — 文章庫\"))")

with open(FILE, "w") as f:
    f.write(content)
print("done")
