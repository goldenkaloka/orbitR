#!/bin/sh
# Placed in /docker-entrypoint.d/ — runs before nginx starts.
# Creates required directories and generates a self-signed certificate
# if the Let's Encrypt cert doesn't exist yet.
# This allows nginx to start on first boot before certbot has run.
#
# Each entry is the cert directory name under /etc/letsencrypt/live/.
# www.mwanachuoshop.com deliberately shares the apex cert — one SAN
# certificate from certbot (-d mwanachuoshop.com -d www.mwanachuoshop.com)
# covers both, so only two names are listed here.

DOMAINS="api.mwanachuoshop.com mwanachuoshop.com"

mkdir -p /var/www/certbot

# A single Let's Encrypt run populates the archive dir for every name, so if
# it is present all certs are real and nothing needs generating.
if [ -d "/etc/letsencrypt/archive" ]; then
    echo "[init-ssl] Let's Encrypt certificates found. Skipping self-signed."
    exit 0
fi

for DOMAIN in $DOMAINS; do
    CERT_PATH="/etc/letsencrypt/live/$DOMAIN"

    if [ -f "$CERT_PATH/fullchain.pem" ]; then
        echo "[init-ssl] SSL certificate already exists at $CERT_PATH. Skipping."
        continue
    fi

    echo "[init-ssl] No SSL certificate found for $DOMAIN. Generating self-signed cert for first boot..."
    mkdir -p "$CERT_PATH"
    apk add --no-cache openssl > /dev/null 2>&1 || true
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "$CERT_PATH/privkey.pem" \
        -out "$CERT_PATH/fullchain.pem" \
        -subj "/CN=$DOMAIN" 2>/dev/null
    echo "[init-ssl] Self-signed certificate created for $DOMAIN. Nginx will start."
    echo "[init-ssl] Run certbot to replace with a real certificate."
done

exit 0
