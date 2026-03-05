#!/usr/bin/env bash
# init-ssl.sh — Run ONCE on a fresh server to get the first certificate.
# Usage: bash init-ssl.sh
set -e

DOMAIN="rag.alex-stu24801.com"
EMAIL="your@email.com"          # ← 改成你的 email（Let's Encrypt 通知用）

echo "▶ Creating required directories..."
mkdir -p nginx/certbot/www nginx/certbot/conf

echo "▶ Downloading recommended TLS params..."
curl -fsSL https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf \
  -o nginx/certbot/conf/options-ssl-nginx.conf
curl -fsSL https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem \
  -o nginx/certbot/conf/ssl-dhparams.pem

echo "▶ Starting nginx (HTTP only) for ACME challenge..."
# Temporarily use an HTTP-only config
cat > nginx/conf.d/rag-init.conf <<'EOF'
server {
    listen 80;
    server_name rag.alex-stu24801.com;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 200 'ok'; }
}
EOF

docker compose up -d nginx

echo "▶ Requesting certificate..."
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  --email "$EMAIL" \
  --agree-tos --no-eff-email \
  -d "$DOMAIN"

echo "▶ Removing temporary config, switching to full config..."
rm nginx/conf.d/rag-init.conf

echo "▶ Starting all services..."
docker compose up -d

echo "✅ Done! Visit https://$DOMAIN"
