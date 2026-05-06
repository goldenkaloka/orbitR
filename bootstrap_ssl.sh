#!/bin/bash
set -e

DOMAIN="api.mwanachuoshop.com"
EMAIL="goldenkaloka@gmail.com"

echo "🔎 Checking DNS for $DOMAIN..."
IP=$(dig +short $DOMAIN | tail -n1)
if [ -z "$IP" ]; then
    echo "❌ ERROR: DNS for $DOMAIN not found."
    exit 1
fi
echo "✅ DNS points to: $IP"

echo "🧹 Cleaning up existing containers..."
docker compose -f docker-compose.yml -f docker-compose.prod.yml down --remove-orphans || true
docker rm -f temp_nginx || true

echo "🌐 Starting temporary Nginx for SSL challenge..."
# Note: We use a simple volume mapping so Certbot can place the challenge file
docker run -d --name temp_nginx \
  -v orbitr_certbot-webroot:/usr/share/nginx/html \
  -p 80:80 nginx:alpine

echo "🔐 Requesting SSL Certificate..."
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm certbot certonly \
  --webroot -w /usr/share/nginx/html \
  --email $EMAIL \
  --agree-tos \
  --no-eff-email \
  --non-interactive \
  -d $DOMAIN

echo "🛑 Stopping temporary Nginx..."
docker stop temp_nginx

echo "🚀 Starting full production stack (Backend + Nginx Proxy)..."
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

echo "🩺 Waiting for health check..."
sleep 10
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps

echo "✅ DONE - Your site should be live at https://$DOMAIN"
