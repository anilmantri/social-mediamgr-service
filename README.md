# social-mediamgr-service

Backend API for the Social Media Manager platform.  
**Stack:** Python 3.12 · FastAPI · PostgreSQL · Redis · Celery · Groq · OpenAI

---

## Run locally

## To run the docker-compose.infra.yaml 
podman machine start
podman compose -f docker-compose.infra.yml up -d

**Requirements:** Python 3.12+, PostgreSQL, Redis

### Step 1 — Clone and enter the project
```bash
git clone <repo-url>
cd social-mediamgr-service
```

### Step 2 — Create a virtual environment
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

### Step 3 — Install dependencies
```bash
pip install -r requirements.txt
```

### Step 4 — Set up environment
```bash
cp .env.example .env
```

Open `.env` and fill in the three required values:
```
SECRET_KEY=any-random-32-char-string
GROQ_API_KEY=gsk_...
OPENAI_API_KEY=sk-...
```

### Step 5 — Create the database
```bash
psql -U postgres -c "CREATE DATABASE social_mediamgr;"
alembic upgrade head
```

### Step 6 — Start the API
```bash

python main.py
```

API is live at **http://127.0.0.1:8000/docs**

---

### Run workers (optional — needed for AI generation and post publishing)

Open separate terminals for each worker:

```bash
# Terminal 2 — AI generation worker
python -m celery -A app.workers.tasks.celery_app worker \
  --loglevel=info --queues=generation --concurrency=2

# Terminal 3 — Calendar batch worker  
python -m celery -A app.workers.tasks.celery_app worker \
  --loglevel=info --queues=calendar --concurrency=1

# Terminal 4 — Instagram publisher worker
python -m celery -A app.workers.tasks.celery_app worker \
  --loglevel=info --queues=publisher --concurrency=2

# Terminal 5 — Beat scheduler (triggers timed tasks)
python -m celery -A app.workers.tasks.celery_app beat --loglevel=info
```

---

### Run tests
```bash
pytest                          # all tests
pytest tests/unit/              # unit tests only (no DB needed)
pytest tests/integration/       # integration tests
pytest --cov=app                # with coverage
```

---

## Deploy to server (Docker)

### Step 1 — Clone on your server
```bash
git clone <repo-url>
cd social-mediamgr-service
cp .env.example .env            # fill in all values
```

### Step 2 — Start everything
```bash
docker compose up -d --build
docker compose exec api alembic upgrade head    # first deploy only
```

### Step 3 — Check it's running
```bash
docker compose ps
curl http://localhost:8000/health
```

### Useful Docker commands
```bash
docker compose logs -f api              # tail API logs
docker compose logs -f worker_generation # tail worker logs
docker compose down                     # stop everything
docker compose up -d --build api        # redeploy API only after code change
```

---

## Project structure

```
social-mediamgr-service/
├── main.py                     ← entry point: python main.py
├── app/
│   ├── api/v1/endpoints/       # Route handlers (one file per module)
│   ├── core/                   # Config, security, cache
│   ├── db/                     # DB engine + session
│   ├── models/                 # SQLAlchemy ORM models
│   ├── schemas/                # Pydantic schemas
│   ├── services/               # Business logic
│   ├── workers/                # Celery tasks
│   └── main.py                 # FastAPI app factory
├── alembic/                    # DB migrations
├── tests/
│   ├── unit/                   # Fast, no DB
│   └── integration/            # Requires DB
├── .env.example
├── docker-compose.yml          # server deployment
├── Dockerfile                  # server deployment
└── requirements.txt
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | Yes | JWT signing key, min 32 chars |
| `GROQ_API_KEY` | Yes | Caption generation |
| `OPENAI_API_KEY` | Yes | Image generation + embeddings |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `REDIS_URL` | Yes | Redis (default: `redis://localhost:6379/0`) |
| `INSTAGRAM_APP_ID` | Module 2 | Meta app ID for OAuth |
| `INSTAGRAM_APP_SECRET` | Module 2 | Meta app secret |
| `S3_ACCESS_KEY_ID` | For images | AWS or Cloudflare R2 |
| `S3_SECRET_ACCESS_KEY` | For images | AWS or Cloudflare R2 |
| `SENTRY_DSN` | No | Error tracking (optional) |

See `.env.example` for all variables with descriptions.

---

## API reference

### Module 1 — Content Engine
```
POST   /api/v1/content/generate              Generate caption + image
GET    /api/v1/content                       List drafts
POST   /api/v1/content/{id}/submit           Submit for review
POST   /api/v1/content/{id}/approve          Approve
POST   /api/v1/content/{id}/reject           Reject
PATCH  /api/v1/content/{id}/edit             Edit content
POST   /api/v1/content/{id}/regenerate       Re-generate
POST   /api/v1/content/calendar/plan         30-day calendar
GET    /api/v1/jobs/{id}                     Poll job status
PUT    /api/v1/brand-profile/{ws_id}         Brand voice profile
```

### Module 2 — Calendar & Scheduler
```
GET    /api/v1/instagram/auth-url            OAuth URL
GET    /api/v1/instagram/account/{ws_id}     Connected account
POST   /api/v1/schedule                      Schedule a post
DELETE /api/v1/schedule/{id}                 Unschedule
PATCH  /api/v1/schedule/{id}/reschedule      Move to new time
GET    /api/v1/schedule                      List scheduled posts
GET    /api/v1/calendar/{ws_id}/{year}/{month} Month view
GET    /api/v1/optimal-time/{ws_id}          Best time suggestions
GET    /api/v1/evergreen/{ws_id}             Top posts for recycling
```

Full docs at **http://127.0.0.1:8000/docs**

---

## Module roadmap

| Module | Status |
|---|---|
| Module 1: AI Content Engine | ✅ Complete |
| Module 2: Calendar & Scheduler | ✅ Complete |
| Module 3: Overview Dashboard | 🔜 Next |
| Module 4: Analyzer Dashboard | 🔜 |
| Module 5: Act on Analysis | 🔜 |
| Module 6: Engagement & Community | 🔜 |
