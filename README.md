# 🚀 Mwanachuoshop Backend

[![Django](https://img.shields.io/badge/Django-5.0+-092E20?style=for-the-badge&logo=django&logoColor=white)](https://www.djangoproject.com/)
[![DRF](https://img.shields.io/badge/DRF-3.16+-A51111?style=for-the-badge&logo=django&logoColor=white)](https://www.django-rest-framework.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-PostGIS-336791?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-Caching-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io/)
[![Celery](https://img.shields.io/badge/Celery-Tasks-37814A?style=for-the-badge&logo=celery&logoColor=white)](https://docs.celeryq.dev/)

The powerhouse backend for the **Mwanachuoshop Super App**. A high-concurrency, event-driven system built with Django, optimized for logistics, marketplace transactions, and real-time order tracking.

---

## 🏗️ Architecture
This project adheres to **Domain-Driven Design (DDD)** and **Event-Driven Architecture (EDA)** principles:
- **Event-Driven**: Decoupled modules communicating via internal signals and Celery workers.
- **Clean Architecture**: Strict separation between Domain logic, Infrastructure, and Application layers.
- **Geospatial Enabled**: Utilizing **PostGIS** for precise delivery routing and geofencing.

---

## 🌟 Key Features
- ** Marketplace & Food Delivery**: Multi-vendor support with complex order lifecycles.
- **Gateway Payments**: Integrated with **EasyPay** for secure production transactions.
- ** Real-time Tracking**: WebSockets via **Django Channels** for live rider tracking.
- ** Robust Security**: JWT Authentication, Idempotency keys for all financial operations, and strict validation.
- ** Notifications**: Multi-channel alerts via **OneSignal** (Push) and **Infobip** (SMS).
- ** Performance**: Redis-backed caching and asynchronous task processing.

---

## 🛠️ Tech Stack
- **Core**: Django 5.0, Django REST Framework
- **Database**: PostgreSQL + PostGIS
- **Real-time**: Django Channels + Daphne
- **Task Queue**: Celery + Redis
- **Storage**: MinIO / AWS S3
- **Admin**: Django Unfold (Teal-branded premium UI)
- **API Docs**: DRF-Spectacular (Swagger/Redoc)

---

## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- PostgreSQL with PostGIS extension
- Redis server
- MinIO (for local media storage)

### Installation
1. **Clone the repository**:
   ```bash
   git clone <repo-url>
   cd backend
   ```

2. **Set up Virtual Environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables**:
   Create a `.env` file in the root directory and fill in the required values (see `.env.example` if available).

4. **Run Migrations**:
   ```bash
   python manage.py migrate
   ```

5. **Start Development Server**:
   ```bash
   # Terminal 1: Django
   python manage.py runserver

   # Terminal 2: Celery Worker
   celery -A config worker -l info

   # Terminal 3: Redis
   redis-server
   ```

---

## 📁 Project Structure
```text
backend/
├── apps/               # Domain-specific applications (Users, Orders, Payments, etc.)
├── config/             # Project configuration (settings, urls, asgi/wsgi)
├── domain/             # Core business logic and entities (DDD)
├── infrastructure/     # External integrations and middleware
├── media/              # Local media storage
├── staticfiles/        # Collected static files
└── manage.py
```

---

## 🧪 Testing
Run the test suite using `pytest`:
```bash
pytest
```

### Load testing (Locust)
Use Locust to baseline p95/p99 latency on critical endpoints:
```bash
locust -f loadtests/locustfile.py --host http://localhost:8000
```
Then open [http://localhost:8089](http://localhost:8089), run 20-100 users, and compare p95 after each optimization.

### Performance logging
Set these environment variables to tune runtime observability:
- `PERF_SLOW_REQUEST_MS` (default `700`) for slow HTTP request warnings
- `PERF_REQUEST_LOG_LEVEL` (`WARNING`/`INFO`)
- `PERF_CELERY_LOG_LEVEL` (`INFO`/`WARNING`)

---

## 📖 API Documentation
Once the server is running, you can access the documentation at:
- **Swagger UI**: `http://localhost:8000/api/schema/swagger-ui/`
- **Redoc**: `http://localhost:8000/api/schema/redoc/`

---

## 🎨 Admin Panel
The backend features a premium, custom-branded admin panel accessible at `/admin/`. It uses the **Unfold** theme with a vibrant teal color palette optimized for logistics operations.

---
© 2026 Mwanachuo Team. All rights reserved.
