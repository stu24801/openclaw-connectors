# Medium API Reference

Base URL: `https://api.medium.com/v1`

## Auth
All requests need: `Authorization: Bearer <MEDIUM_TOKEN>`
Token source: env var `MEDIUM_TOKEN` or config.

## Endpoints

### Get current user
```
GET /me
```
Returns: `id`, `username`, `name`, `url`, `imageUrl`

### Create post
```
POST /users/{authorId}/posts
Content-Type: application/json
```

Body:
```json
{
  "title": "Post title",
  "contentFormat": "html",   // "html" or "markdown"
  "content": "<h1>...</h1>",
  "tags": ["tag1", "tag2"],  // optional, max 5
  "publishStatus": "draft",  // "draft" | "public" | "unlisted"
  "canonicalUrl": "...",     // optional
  "notifyFollowers": true    // optional, default true
}
```

Returns: post object with `id`, `url`, `authorId`, `publishStatus`, etc.

## Read Articles
Medium has no official read API. Use web_fetch to fetch article content:
- Direct URL: `https://medium.com/@username/article-slug`
- Use `web_fetch` with `extractMode: "markdown"` for clean content
- For member-only articles, content may be truncated

## Tips
- `publishStatus: "draft"` → saves to drafts (not published)
- `publishStatus: "public"` → publishes immediately
- Max tags: 5
- HTML content: use standard HTML; Medium renders it properly
- Markdown content: use standard Markdown
