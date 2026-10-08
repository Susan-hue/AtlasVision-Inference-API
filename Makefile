PY311   := python:3.11-slim
COMPILE := uv pip compile -q --python-version 3.11

.PHONY: lock upgrade up down logs test lint format

define compile
	docker run --rm -v "$(CURDIR)":/src -w /src -v atlasvision-pip-cache:/root/.cache \
		-e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore $(PY311) sh -c '\
		pip install -q "uv==0.12.*" && \
		$(COMPILE) $(1) requirements/base.in -o requirements/base.txt && \
		$(COMPILE) $(1) requirements/api.in -o requirements/api.txt && \
		$(COMPILE) $(1) requirements/dev.in -o requirements/dev.txt && \
		chown $(shell id -u):$(shell id -g) requirements/*.txt'
endef

lock:  ## Pin newly added requirements, keeping existing pins
	$(call compile,)

upgrade:  ## Re-resolve every pin to the latest allowed version
	$(call compile,--upgrade)

up:  ## Start the local stack
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

test:  ## Run pytest in the dev image against the compose Redis
	docker compose run --rm --build --user "$(shell id -u):$(shell id -g)" tests pytest

lint:
	docker compose run --rm --build --user "$(shell id -u):$(shell id -g)" tests sh -c 'ruff check . && ruff format --check .'

format:
	docker compose run --rm --build --user "$(shell id -u):$(shell id -g)" tests sh -c 'ruff format . && ruff check --fix .'
