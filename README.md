# AtlasVision Inference API

A job-queue API around the [`testatlasvision`](https://pypi.org/project/testatlasvision/0.1.0/)
SDK (multilingual visual question answering in English, Igbo, Yoruba and Hausa).

> Work in progress. Phase 1 contains the project skeleton, configuration, Redis, and `/health`.
> The full documentation (architecture, endpoints, every setting) is written in Phase 5.

## Not for direct browser use

The API is meant to be called **server to server**, e.g. from a Vercel route that holds the API
key. Browsers should never see the key, so CORS is disabled by default (`CORS_ORIGINS` empty).

## Running locally

Only Docker (with Compose) is required; everything runs in Python 3.11 containers.

```bash
cp .env.example .env     # optional; every setting has a default
make up                  # start redis + api in the background
curl -i localhost:8000/health
make down
```

| Command | What it does |
|---|---|
| `make up` / `make down` | Start / stop the local stack |
| `make logs` | Follow container logs |
| `make test` | Run pytest in a container against the Compose Redis (DB 15) |
| `make lint` / `make format` | Ruff checks / auto-fix and format |
| `make lock` | Re-resolve `requirements/*.in` into pinned `requirements/*.txt` |

Configuration is read from environment variables; `.env.example` documents each one.
