#!/bin/bash
# ---------------------------------------------------------------------------
# Mwanachuoshop — SSL Bootstraper (Direct Docker Mode)
# ---------------------------------------------------------------------------
set -e

# --- Configuration ---
DOMAIN="api.mwanachuoshop.com"
EMAIL="goldenkaloka@gmail.com"

# The project name used for volume prefixing
# On your VPS, this is likely 'orbitr' (based on your folder structure)
PROJECT="orbitr"

echo "🔎 [1/6] Checking DNS for $DOMAIN..."
IP=$(dig +short $DOMAIN | tail -n1)
if [ -z "$IP" ]; then
    echo "❌ ERROR: DNS for $DOMAIN not found. Please wait for propagation."
    exit 1
fi
echo "✅ DNS found: $IP"

echo "🧹 [2/6] Cleaning up existing containers..."
docker compose -f docker-compose.yml -f docker-compose.prod.yml down --remove-orphans || true
docker rm -f temp_nginx temp_certbot || true

echo "🌐 [3/6] Starting temporary Nginx for SSL challenge..."
# Map the shared certbot volume to Nginx's default public directory
docker run -d --name temp_nginx \
  -v ${PROJECT}_certbot-webroot:/usr/share/nginx/html \
  -p 80:80 nginx:alpine

echo "🔐 [4/6] Requesting SSL Certificate (Direct Mode)..."
# We run certbot directly to avoid any entrypoint conflicts in docker-compose
docker run --rm --name temp_certbot \
  -v ${PROJECT}_certbot-etc:/etc/letsencrypt \
  -v ${PROJECT}_certbot-var:/var/lib/letsencrypt \
  -v ${PROJECT}_certbot-webroot:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  --email $EMAIL --agree-tos --no-eff-email --non-interactive \
  -d $DOMAIN

echo "🛑 [5/6] Stopping temporary Nginx..."
docker stop temp_nginx || true

echo "🚀 [6/6] Launching Production Stack..."
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

echo "✅ ALL DONE! Your site should now be live at https://$DOMAIN"
