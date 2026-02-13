# GitHub Rate Limit Checker

## Description
Check the current GitHub API rate limit status for the current IP or token. Use this when you encounter "API rate limit exceeded" errors to see how many requests are remaining and when the limit resets.

## Usage
Run the following command to check the rate limit:

```bash
curl -s -H "Accept: application/vnd.github.v3+json" https://api.github.com/rate_limit | jq .resources.core
```

If you have a GitHub token (optional), you can include it to check the authenticated limit:
```bash
# Replace YOUR_TOKEN with the actual token
curl -s -H "Authorization: token YOUR_TOKEN" -H "Accept: application/vnd.github.v3+json" https://api.github.com/rate_limit | jq .resources.core
```

## Output Explanation
- `limit`: The maximum number of requests you can make per hour.
- `remaining`: The number of requests remaining in the current window.
- `reset`: The time at which the current rate limit window resets (in UTC epoch seconds).

To convert the reset time to a readable format:
```bash
date -d @<reset_timestamp>
```
