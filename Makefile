# Convenience targets. `make help` lists them.
.PHONY: help install sandbox-image db seed api worker test test-fast test-docker lint format compose-up compose-down

SANDBOX_IMAGE ?= online-judge-sandbox:latest

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package with dev dependencies
	pip install -e ".[dev]"

sandbox-image:  ## Build the Docker sandbox image
	docker build -t $(SANDBOX_IMAGE) sandbox/

db:  ## Create the database schema
	python -m app.db

seed:  ## Load the example problems
	python -m scripts.seed

api:  ## Run the API (auto-reload)
	uvicorn app.main:app --reload

worker:  ## Run a pool of grading workers
	python -m app.worker --workers 2

test:  ## Run the full test suite
	pytest

test-fast:  ## Run only the fast tests (no Docker)
	pytest -m "not docker"

test-docker:  ## Run only the Docker-backed integration tests
	pytest -m docker

lint:  ## Lint with ruff
	ruff check .

format:  ## Auto-format with ruff
	ruff format app tests scripts sandbox

compose-up:  ## Bring the whole system up with Docker Compose
	docker compose up --build

compose-down:  ## Tear down Docker Compose (and volumes)
	docker compose down -v
