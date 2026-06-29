# Mwanachuoshop Backend

The backend for the Mwanachuoshop super app. It is a Django REST Framework service for marketplace, food delivery, payments, realtime logistics events, and operational admin workflows.

## Architecture
- Event-driven workflows use Django signals and Celery workers.
- Realtime client updates are published through Centrifugo.
- PostGIS powers geospatial delivery zones, restaurant locations, and marketplace proximity.
- Redis is used for cache, Celery broker/result backend, and Centrifugo support.

## Tech Stack
- Core: Django 5.0, Django REST Framework
- Database: PostgreSQL + PostGIS
- Realtime: Centrifugo
- Task queue: Celery + Redis
- Storage: MinIO / S3-compatible storage
- Admin: Django Unfold
- API docs: DRF Spectacular

## Getting Started
### Prerequisites
- Python 3.10+
- PostgreSQL with PostGIS extension
- Redis server
- MinIO for local media storage
- Centrifugo for realtime WebSocket delivery

### Installation
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Create `.env` from `.env.example`, then run:
```bash
python manage.py migrate
python seed_operational_zones.py
python manage.py runserver
```

For the full local stack, use Docker Compose:
```bash
docker compose up --build
```

## Testing
```bash
pytest
```

## Load Testing
```bash
locust -f loadtests/locustfile.py --host http://localhost:8000
```

Open `http://localhost:8089` and compare p95/p99 latency after each optimization.

## API Documentation
Once the server is running:
- Swagger UI: `http://localhost:8000/api/schema/swagger-ui/`
- Redoc: `http://localhost:8000/api/schema/redoc/`
- Raw OpenAPI schema: `http://localhost:8000/api/schema/`

## Project Structure
```text
backend/
|-- api/                # DRF routers, serializers, and API viewsets
|-- apps/               # Django apps and persistence models
|-- config/             # Settings, URLs, WSGI/ASGI, Celery app
|-- domain/             # Business services and workflow logic
|-- infrastructure/     # Gateway integrations and middleware
|-- loadtests/          # Locust load tests
|-- tests/              # Cross-app and e2e tests
`-- manage.py
```
