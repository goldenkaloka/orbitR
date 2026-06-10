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
    echo "PostgreSQL is up"
fi

# Celery workers do not serve static files; skip collectstatic to avoid races on staticfiles/
case "$1" in
  celery*) RUN_COLLECTSTATIC=0 ;;
esac

# Collect static files only for web server processes (not celery, not one-off manage.py commands)
if [ "${RUN_COLLECTSTATIC:-1}" = "1" ]; then
  if echo "$*" | grep -qE 'gunicorn|uvicorn|runserver'; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput 2>&1
  fi
fi

# Ensure logs and migrations directories are writable by the django user.
# The volume mount .:/app may overlay host directories owned by root,
# preventing the non-root django user from writing at import time.
mkdir -p /app/logs
chown django:django /app/logs
find /app -path '*/migrations' -type d -exec chown django:django {} +
find /app -path '*/migrations' -type f -exec chown django:django {} +

# Drop privileges to the django user (unless already root) and run the provided command
if [ "$(id -u)" = "0" ]; then
  exec gosu django "$@"
else
  exec "$@"
fi
