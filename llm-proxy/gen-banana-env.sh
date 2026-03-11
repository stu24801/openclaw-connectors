#!/bin/bash
# Sync OpenClaw gateway token → Banana Slides .env AND SQLite DB (via container)
# Called by banana-slides.service ExecStartPre

OPENCLAW_JSON="$HOME/.openclaw/openclaw.json"
ENV_FILE="/opt/banana-slides/.env"

# Read gateway token
GATEWAY_TOKEN=$(python3 -c "
import json, sys
try:
    d = json.load(open('$OPENCLAW_JSON'))
    print(d['gateway']['auth']['token'])
except Exception as e:
    print('ERROR: ' + str(e), file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)

if [ -z "$GATEWAY_TOKEN" ]; then
    echo "ERROR: Could not read gateway token from $OPENCLAW_JSON" >&2
    exit 1
fi

# Detect banana-slides docker network gateway IP
DOCKER_GW=$(python3 -c "
import subprocess, json
try:
    out = subprocess.check_output(
        ['docker', 'network', 'inspect', 'banana-slides_banana-slides-network'],
        stderr=subprocess.DEVNULL
    )
    d = json.loads(out)
    print(d[0]['IPAM']['Config'][0]['Gateway'])
except Exception:
    print('172.18.0.1')
" 2>/dev/null)

API_BASE="http://${DOCKER_GW}:9000/v1"
echo "Updating token: ${GATEWAY_TOKEN:0:16}... gateway=$DOCKER_GW"

# 1. Update .env (for new containers)
sed -i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=$GATEWAY_TOKEN|" "$ENV_FILE"
sed -i "s|^OPENAI_API_BASE=.*|OPENAI_API_BASE=$API_BASE|" "$ENV_FILE"
echo "  ✓ .env updated"

# 2. Update SQLite DB inside container (DB owned by root inside Docker)
if docker ps --format '{{.Names}}' | grep -q '^banana-slides-backend$'; then
    python3 /opt/llm-proxy/update-banana-db.py "$GATEWAY_TOKEN" "$API_BASE"
else
    echo "  ⚠ container not running, DB will sync after container starts"
fi

echo "Done."
