# === BUILDER STAGE ===
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils \
    libproj-dev \
    libgdal-dev \
    libpq-dev \
    gcc \
    gettext \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# === RUNTIME STAGE ===
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libpq5 \
    gosu \
    curl \
    postgresql-client \
    unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip \
    && unzip -j rclone-current-linux-amd64.zip */rclone -d /usr/local/bin/ \
    && rm rclone-current-linux-amd64.zip

# Copy the entire Python installation from builder (site-packages + binaries)
COPY --from=builder /usr/local /usr/local

WORKDIR /app

COPY . /app/

RUN addgroup --system django && adduser --system --ingroup django django && \
    chown -R django:django /app && \
    chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
