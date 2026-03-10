---
name: medium
description: >
  Read Medium articles and publish or draft posts via the Medium API.
  Use when the user wants to fetch/summarize/extract content from a Medium article URL,
  publish a new post to Medium, save a draft to Medium, or manage Medium posts.
  Requires MEDIUM_TOKEN env var for publishing.
---

# Medium Skill

## Reading Articles

Use `web_fetch` with `extractMode: "markdown"` to read any Medium article URL.
- Member-only articles may be truncated — mention this to the user if content seems cut off.
- Summarize, extract key points, or translate as requested.

```
web_fetch(url: "<medium article url>", extractMode: "markdown")
```

## Publishing / Drafting Posts

See `references/api.md` for full API details.

### Quick flow

1. Get author ID:
   ```
   GET https://api.medium.com/v1/me
   Authorization: Bearer <MEDIUM_TOKEN>
   ```

2. Create post:
   ```
   POST https://api.medium.com/v1/users/{authorId}/posts
   Authorization: Bearer <MEDIUM_TOKEN>
   Content-Type: application/json
   ```

Use `exec` with `curl` for API calls:
```bash
TOKEN="${MEDIUM_TOKEN}"
ME=$(curl -s -H "Authorization: Bearer $TOKEN" https://api.medium.com/v1/me)
AUTHOR_ID=$(echo $ME | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])")

curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "My Title",
    "contentFormat": "markdown",
    "content": "# Hello\n\nContent here.",
    "publishStatus": "draft"
  }' \
  "https://api.medium.com/v1/users/$AUTHOR_ID/posts"
```

### Key rules
- `publishStatus`: `"draft"` (save only) | `"public"` (publish now) | `"unlisted"`
- Max 5 tags
- `contentFormat`: `"markdown"` or `"html"`
- Always confirm with user before `publishStatus: "public"` — default to `"draft"` unless explicitly asked to publish

## Config / Token
- Token env var: `MEDIUM_TOKEN`
- If not set, ask user: "Please set your Medium integration token as `MEDIUM_TOKEN` in your environment."
- Get token at: https://medium.com/me/settings → Integration tokens
