# Use official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies for PostGIS and psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils \
    libproj-dev \
    gdal-bin \
    libgdal-dev \
    python3-gdal \
    libpq-dev \
    gcc \
    gettext \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . /app/

# Create a non-root user and switch to it
RUN addgroup --system django && adduser --system --group django
RUN chown -R django:django /app
USER django

# Copy and set permissions for entrypoint
# (Switch back to root briefly to set permissions, then back to django or handle via COPY)
USER root
RUN chmod +x /app/docker-entrypoint.sh
USER django

# Expose port
EXPOSE 8000

# Entrypoint
ENTRYPOINT ["/app/docker-entrypoint.sh"]
