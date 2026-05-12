#!/bin/sh

set -e

# Wait for PostgreSQL
if [ "$DB_HOST" = "db" ]; then
    echo "Waiting for PostgreSQL at $DB_HOST:$DB_PORT..."
    python << END
import socket
import time
import os

db_host = os.environ.get('DB_HOST', 'db')
db_port = int(os.environ.get('DB_PORT', 5432))

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
while True:
    try:
        s.connect((db_host, db_port))
        s.close()
        break
    except socket.error:
        time.sleep(0.1)
END
    echo "PostgreSQL is up - executing migrations"
fi

# Collect static files and rebuild the manifest
# Necessary whenever package static files change (e.g. django-unfold upgrades)
echo "Collecting static files..."
python manage.py collectstatic --noinput --clear 2>&1

# Run the provided command
exec "$@"
