#!/bin/bash
set -e

echo "🚀 Starting deployment for api.mwanachuoshop.com..."

# 1. Load production overrides
COMPOSE_ARGS="-f docker-compose.yml -f docker-compose.prod.yml"

# 2. Pull latest images and build
echo "📦 Building and pulling latest images..."
docker-compose $COMPOSE_ARGS build
docker-compose $COMPOSE_ARGS pull

# 3. Apply Migrations (using the backend container)
echo "📂 Running database migrations..."
docker-compose $COMPOSE_ARGS run --rm backend python manage.py migrate --noinput

# 4. Collect Static (using the backend container)
echo "🎨 Collecting static files..."
docker-compose $COMPOSE_ARGS run --rm backend python manage.py collectstatic --noinput

# 5. Seed Operational Zones if needed (optional)
# echo "🗺️ Seeding operational zones..."
# docker-compose $COMPOSE_ARGS run --rm backend python seed_operational_zones.py

# 6. Bring up all services
echo "🚢 Deploying services..."
docker-compose $COMPOSE_ARGS up -d

# 7. Health Probe
echo "🩺 Waiting for backend health..."
MAX_RETRIES=10
COUNT=0
until $(curl --output /dev/null --silent --head --fail https://api.mwanachuoshop.com/api/users/); do
    printf '.'
    sleep 3
    COUNT=$((COUNT+1))
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo "❌ Deployment failed: Backend health probe timed out."
        exit 1
    fi
done

echo "✅ Deployment Successful! api.mwanachuoshop.com is healthy."
