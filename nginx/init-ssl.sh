#!/bin/sh
# Placed in /docker-entrypoint.d/ — runs before nginx starts.
# Generates a self-signed certificate if the Let's Encrypt cert doesn't exist yet.
# This allows nginx to start on first boot before certbot has run.

DOMAIN="api.mwanachuoshop.com"
CERT_PATH="/etc/letsencrypt/live/$DOMAIN"

if [ ! -f "$CERT_PATH/fullchain.pem" ]; then
    echo "[init-ssl] No SSL certificate found for $DOMAIN. Generating self-signed cert for first boot..."
    mkdir -p "$CERT_PATH"
    apk add --no-cache openssl > /dev/null 2>&1 || true
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "$CERT_PATH/privkey.pem" \
        -out "$CERT_PATH/fullchain.pem" \
        -subj "/CN=$DOMAIN" 2>/dev/null
    echo "[init-ssl] Self-signed certificate created. Nginx will start."
    echo "[init-ssl] Run certbot to replace with a real certificate."
else
    echo "[init-ssl] SSL certificate already exists at $CERT_PATH. Skipping."
fi
