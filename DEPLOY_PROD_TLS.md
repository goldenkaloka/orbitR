# Production Deploy (Docker + Nginx + TLS)

This stack serves `https://api.mwanachuoshop.com` with Nginx as the public entrypoint and keeps backend/minio/db/redis internal.

## 1) DNS

- Create an `A` record:
  - Host: `api`
  - Value: `<your_vps_public_ip>`

Wait until DNS resolves:

```bash
nslookup api.mwanachuoshop.com
```

## 2) Environment

Create/adjust `.env` in `backend` with production values:

- `DEBUG=False`
- `ALLOWED_HOSTS=api.mwanachuoshop.com`
- `CSRF_TRUSTED_ORIGINS=https://api.mwanachuoshop.com`
- `SECURE_SSL_REDIRECT=True`
- `SESSION_COOKIE_SECURE=True`
- `CSRF_COOKIE_SECURE=True`
- `MINIO_ENDPOINT=http://minio:9000`
- `MINIO_EXTERNAL_URL=https://api.mwanachuoshop.com`
- `MINIO_STORAGE_BUCKET_NAME=media`
- Strong secrets/passwords for DB/MinIO/SECRET_KEY

## 3) Start services (without HTTPS cert first)

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d db redis minio minio-init backend nginx
```

## 4) Issue first Let's Encrypt certificate

Run once:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm certbot certonly --webroot -w /var/www/certbot -d api.mwanachuoshop.com --email YOUR_EMAIL@example.com --agree-tos --no-eff-email
```

## 5) Reload Nginx with certs

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart nginx
```

## 6) Start renewer

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d certbot
```

## 7) Verify

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f --timestamps nginx backend
```

Open:

- `https://api.mwanachuoshop.com`

## Notes

- In production overlay, host port mappings are disabled for db/redis/minio/backend.
- Nginx is the only public ingress (`80/443`).
- MinIO media bucket is proxied via `/media/` on the same HTTPS domain.
