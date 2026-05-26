.PHONY: help install dev test test-unit test-integration lint format \
        migrate migrate-create migrate-down docker-up docker-down \
        worker-gen worker-cal flower shell

# ── Config ────────────────────────────────────────────────────────────────────
PYTHON       := python3
PIP          := pip3
APP          := app.main:app
CELERY_APP   := app.workers.tasks.celery_app

# Colours
CYAN  := \033[36m
RESET := \033[0m

help: ## Show this help message
	@echo ""
	@echo "  social-mediamgr-service — Developer Commands"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────
install: ## Install all dependencies
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PIP) install aiosqlite watchfiles  # dev extras

setup: install ## Full first-time setup: install + copy .env + migrate
	@[ -f .env ] || (cp .env.example .env && echo "✅  .env created — fill in your API keys")
	@$(MAKE) migrate

# ── Dev server ────────────────────────────────────────────────────────────────
dev: ## Run API locally with hot reload
	python main.py

dev-log: ## Run API with verbose SQL logging
	DB_ECHO=true python main.py

# ── Workers ───────────────────────────────────────────────────────────────────
worker-pub: ## Start Celery worker for publisher queue (posts to Instagram)
	python -m celery -A $(CELERY_APP) worker --loglevel=info --queues=publisher \
	  --concurrency=2 --hostname=worker_pub@%h

worker-gen: ## Start Celery worker for generation queue
	python -m celery -A $(CELERY_APP) worker --loglevel=info --queues=generation \
	  --concurrency=4 --hostname=worker_gen@%h

worker-cal: ## Start Celery worker for calendar queue
	python -m celery -A $(CELERY_APP) worker --loglevel=info --queues=calendar \
	  --concurrency=2 --hostname=worker_cal@%h

beat: ## Start Celery beat scheduler
	python -m celery -A $(CELERY_APP) beat --loglevel=info

flower: ## Open Flower — Celery monitoring UI (http://localhost:5555)
	python -m celery -A $(CELERY_APP) flower --port=5555

# ── Database migrations ───────────────────────────────────────────────────────
migrate: ## Apply all pending migrations
	alembic upgrade head

migrate-down: ## Rollback last migration
	alembic downgrade -1

migrate-create: ## Create a new migration (usage: make migrate-create MSG="add_user_table")
	@[ "$(MSG)" ] || (echo "❌  Usage: make migrate-create MSG='describe change'"; exit 1)
	alembic revision --autogenerate -m "$(MSG)"

migrate-history: ## Show migration history
	alembic history --verbose

migrate-current: ## Show current migration version
	alembic current

# ── Tests ─────────────────────────────────────────────────────────────────────
test: ## Run all tests with coverage
	pytest tests/ -v --tb=short

test-unit: ## Run only unit tests (fast, no DB needed)
	pytest tests/unit/ -v -m unit --tb=short

test-integration: ## Run integration tests (requires DB)
	pytest tests/integration/ -v -m integration --tb=short

test-coverage: ## Run tests and open HTML coverage report
	pytest tests/ --cov=app --cov-report=html:htmlcov
	open htmlcov/index.html 2>/dev/null || xdg-open htmlcov/index.html

test-watch: ## Re-run tests on file change
	ptw tests/ -- -v -m unit

# ── Code quality ──────────────────────────────────────────────────────────────
lint: ## Run ruff linter
	ruff check app/ tests/

format: ## Auto-format with ruff and isort
	ruff format app/ tests/
	ruff check --fix app/ tests/

typecheck: ## Run mypy type checker
	mypy app/ --ignore-missing-imports --strict

check: lint typecheck ## Run all checks (lint + types)

# ── Docker ────────────────────────────────────────────────────────────────────
docker-up: ## Start full stack (postgres, redis, api, workers)
	docker compose up -d
	@echo "✅  Stack running:"
	@echo "   API:    http://localhost:8000/docs"
	@echo "   Flower: http://localhost:5555"
	@echo "   Mail:   http://localhost:8025"

docker-down: ## Stop and remove containers
	docker compose down

docker-rebuild: ## Rebuild containers from scratch
	docker compose down
	docker compose build --no-cache
	docker compose up -d

docker-logs: ## Tail all container logs
	docker compose logs -f

docker-logs-api: ## Tail API logs only
	docker compose logs -f api

docker-logs-worker: ## Tail worker logs
	docker compose logs -f worker_generation worker_calendar

# ── Utils ─────────────────────────────────────────────────────────────────────
shell: ## Open a Python shell with app context loaded
	PYTHONPATH=. $(PYTHON) -c "import asyncio; from app.db.session import AsyncSessionFactory; print('DB session ready. Use AsyncSessionFactory()')"

clean: ## Remove build artefacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov .coverage dist build *.egg-info
