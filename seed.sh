#!/bin/bash

# This script seeds the marketplace categories in the Django backend running inside Docker.

echo "🚀 Seeding Marketplace Categories..."

# Check if docker-compose is installed
if ! command -v docker-compose &> /dev/null; then
    echo "❌ Error: docker-compose is not installed."
    exit 1
fi

# Run the seeding script inside the backend container
docker-compose exec backend python seed_marketplace.py

if [ $? -eq 0 ]; then
    echo "✅ Seeding completed successfully!"
else
    echo "❌ Seeding failed. Make sure the backend container is running."
    exit 1
fi
