#!/usr/bin/env python3
"""Update banana-slides settings DB inside Docker container via docker exec."""
import sys
import subprocess

if len(sys.argv) != 3:
    print("Usage: update-banana-db.py <token> <api_base>")
    sys.exit(1)

token = sys.argv[1]
api_base = sys.argv[2]

script = f"""
import sqlite3
conn = sqlite3.connect('/app/backend/instance/database.db')
cur = conn.cursor()
cur.execute(
    "UPDATE settings SET api_key=?, api_base_url=?, updated_at=datetime('now') WHERE id=1",
    ('{token}', '{api_base}')
)
conn.commit()
print('  ✓ DB updated (' + str(cur.rowcount) + ' row)')
conn.close()
"""

result = subprocess.run(
    ['docker', 'exec', 'banana-slides-backend', 'python3', '-c', script],
    capture_output=True, text=True
)
print(result.stdout, end='')
if result.returncode != 0:
    print(f"  ⚠ DB update failed: {result.stderr[:200]}", file=sys.stderr)
