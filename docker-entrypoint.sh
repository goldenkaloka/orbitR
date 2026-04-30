#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

# Wait for Database
echo "Waiting for PostgreSQL at $DB_HOST:$DB_PORT..."
python << END
import socket
import time
import os

host = os.environ.get('DB_HOST', 'db')
port = int(os.environ.get('DB_PORT', 5432))

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
while True:
    try:
        s.connect((host, port))
        s.close()
        break
    except socket.error as ex:
        print(f"Waiting for database... {ex}")
        time.sleep(1)
END
echo "PostgreSQL is up - executing migrations"

# Apply database migrations
python manage.py migrate

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Start server
echo "Starting server with Daphne..."
exec daphne -b 0.0.0.0 -p 8000 config.asgi:application
