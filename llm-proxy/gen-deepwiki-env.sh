#!/bin/bash
# Generate runtime env for DeepWiki: inject OPENAI_API_KEY from openclaw.json
# This is called by deepwiki-api.service ExecStartPre

OPENCLAW_JSON="$HOME/.openclaw/openclaw.json"
RUNTIME_ENV="/tmp/deepwiki-runtime.env"

GATEWAY_TOKEN=$(python3 -c "
import json, sys
try:
    d = json.load(open('$OPENCLAW_JSON'))
    print(d['gateway']['auth']['token'])
except Exception as e:
    sys.exit(1)
" 2>/dev/null)

if [ -z "$GATEWAY_TOKEN" ]; then
    echo "ERROR: Could not read gateway token from $OPENCLAW_JSON" >&2
    exit 1
fi

echo "OPENAI_API_KEY=$GATEWAY_TOKEN" > "$RUNTIME_ENV"
echo "Generated $RUNTIME_ENV with token from openclaw.json"
