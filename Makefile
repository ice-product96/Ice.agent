.PHONY: dev-api dev-ui test lint build up down

dev-api:
	cd backend && uvicorn app.main:app --reload

dev-ui:
	cd frontend && npm run dev

test:
	cd backend && pytest

lint:
	cd backend && ruff check .
	cd frontend && npm run build

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down
