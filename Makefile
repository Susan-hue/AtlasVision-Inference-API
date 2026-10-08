# All commands run in Docker so the host Python version doesn't matter.
PY311   := python:3.11-slim
COMPILE := uv pip compile -q --python-version 3.11 --upgrade

.PHONY: lock up down logs test lint format

lock:  ## Re-resolve requirements/*.in into fully pinned requirements/*.txt
	docker run --rm -v "$(CURDIR)":/src -w /src \
		-e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore $(PY311) sh -c '\
		pip install -q "uv==0.12.*" && \
		$(COMPILE) requirements/api.in -o requirements/api.txt && \
		$(COMPILE) requirements/dev.in -o requirements/dev.txt && \
		chown $(shell id -u):$(shell id -g) requirements/*.txt'

up:  ## Start the local stack
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

test:  ## Run pytest in the dev image against the compose Redis
	docker compose run --rm --build tests pytest

lint:
	docker compose run --rm --build tests sh -c 'ruff check . && ruff format --check .'

format:
	docker compose run --rm --build tests sh -c 'ruff check --fix . && ruff format .'
