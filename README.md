# AtlasVision Inference API

A production API around the [`testatlasvision`](https://pypi.org/project/testatlasvision/0.1.0/)
SDK: visual question answering in English, Igbo, Yoruba and Hausa (SigLIP2 vision, N-ATLaS
language model, AtlasVision projector/LoRA weights).

The API takes an image and a question, queues the work for a GPU worker, and lets the client
poll for the answer. It supports follow-up questions (sessions), per-visitor ownership, rate
limits and overload protection. Locally, everything runs in Docker with a **fake model**, so
no GPU is needed for development.

**Building a frontend?** Read [docs/frontend-api.md](docs/frontend-api.md): requests, responses,
polling, sessions, every error code and image preparation.

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Make commands](#make-commands)
- [Configuration](#configuration)
- [API overview](#api-overview)
- [Security](#security)
- [Operations](#operations)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Architecture

```
 browser ──► Vercel route ──HTTPS──► API (FastAPI, no GPU) ──────┐
            (holds the key)          validates, stores image,    │
                                     enqueues, reads results     ▼
                                                             ┌────────┐
                                                             │ Redis  │ queue, jobs, images,
                                                             └────────┘ sessions, heartbeats,
                                                                  ▲     rate-limit counters
                                     GPU worker (arq) ───────────┘
                                     loads the model once, runs one job at a time
```

Three processes:

| Process | Does | Needs |
|---|---|---|
| **API** (`app/api`) | Authenticates, validates the request and image, stores the image in Redis, enqueues a job, serves job status and sessions. Safe to run as several replicas. | Redis. No GPU, no torch, no `HF_TOKEN`. |
| **Redis** | Job queue (arq), job records and results, image bytes, sessions and their locks, worker heartbeats, rate-limit counters. Everything has a TTL; nothing needs to be persisted. | |
| **Worker** (`app/worker`) | Loads `TestAtlasVision` once at startup, then runs chat jobs one at a time. | A GPU (or the fake model), Redis, `HF_TOKEN`. |

### A question, step by step

1. The client `POST`s `/v1/jobs/chat` with the image and question.
2. The API checks the key, rate limits, fields, worker availability, queue depth and the image
   (Pillow must fully decode it), then stores the image bytes (`IMAGE_TTL`) and a `queued` job
   record, enqueues the job id, and replies `202` with the `job_id`.
3. The worker marks the job `running`, loads the session if there is one, calls
   `chat(image_bytes, ...)` in a thread, and saves the result (`RESULT_TTL`), or a `failed`
   status with a safe error code. It always deletes the image.
4. The client polls `GET /v1/jobs/{job_id}` until `done` or `failed`.

### Design choices

- **Job queue and polling, not one long request.** A non-English question is up to three
  model generations, longer than proxies and serverless functions like to wait.
- **arq** fits a single GPU: asyncio-native, Redis-only, `max_jobs=1`, and no forking after
  CUDA is initialised. Job payloads are JSON (not arq's default pickle), and only
  `app/core/queue.py` and the worker entry point know the queue is arq. arq is in
  maintenance mode upstream; swapping it means changing those two places.
- **Blocking SDK calls run in `asyncio.to_thread`**, so the worker's event loop keeps running
  its heartbeat while the GPU works.
- **Heartbeat on a plain asyncio task** (not an arq cron job, which would wait behind the
  running job). The worker publishes `loading`, `ready` or `failed` every
  `HEARTBEAT_INTERVAL` seconds. The API accepts jobs only while a worker is `ready` or
  `loading` and its heartbeat is younger than `HEARTBEAT_STALE_AFTER`.
- **Sessions** store the SDK's English history plus what the user actually asked and saw,
  trimmed to `MAX_SESSION_TURNS`. A per-session lock (atomic Lua scripts) allows one question
  at a time; a second one gets `409 session_busy`.
- **Errors:** one catalog (`app/core/errors.py`) holds every client-facing code, its HTTP
  status and its fixed message, and maps SDK exceptions to codes. Varying details (which
  field, which limit) go in extra fields of the error body; internal details go to the logs
  only. A test fails if code uses an error code missing from the catalog.
- **Fake model:** `FakeAtlasVision` has the SDK's exact method signatures (checked by a test),
  gives deterministic answers, simulates 1-3 generations and can raise any SDK error on demand.

### Redis keys

| Key | Holds | Lifetime |
|---|---|---|
| `arq:queue`, `arq:*` | arq's queue and bookkeeping | arq-managed |
| `atlas:job:{id}` | Job record (status, owner, params, result or error) | until `RESULT_TTL` after finishing |
| `atlas:job:{id}:image` | Uploaded image bytes | `IMAGE_TTL`; deleted when the job ends |
| `atlas:session:{id}` | Session history, per-turn details, owner | `SESSION_TTL`, refreshed on use |
| `atlas:session:{id}:inflight` | Job id holding the session lock | while a job is queued or running |
| `atlas:workers` | Hash of worker heartbeats | entries pruned after 1 hour |
| `atlas:rl:*` | Rate-limit counters | 2 minutes |

## Quick start

### Prerequisites

- Git, GNU Make.
- Docker Engine 24+ with the Compose plugin **2.24+** (`docker compose version`). On
  macOS/Windows, Docker Desktop. On Windows, run the commands in WSL2.
- About 3 GB of disk for images. No local Python, Redis or GPU needed.

### Run it

```bash
git clone https://github.com/Susan-hue/AtlasVision-Inference-API.git
cd AtlasVision-Inference-API

cp .env.example .env
KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")   # or any long random string
sed -i "s|^API_KEYS=.*|API_KEYS=$KEY|" .env      # macOS: sed -i '' "s|...|...|" .env
cp .env.worker.example .env.worker               # optional: worker-only settings

make up        # builds and starts redis, api and the fake-model worker
```

The first build downloads Python packages and can take several minutes on a slow connection
(see [Troubleshooting](#the-docker-build-is-slow-or-seems-stuck)). Later builds take seconds.

### Check it works

```bash
curl -s localhost:8000/health                                  # {"status":"ok","redis":"ok"}
curl -s -H "X-API-Key: $KEY" localhost:8000/v1/model           # {"state":"ready",...}

# Ask a question about any JPEG/PNG/WebP under 4 MB
curl -s -H "X-API-Key: $KEY" -X POST localhost:8000/v1/jobs/chat \
  -F image=@photo.jpg -F question="What is in this picture?" -F lang=yo -F question_lang=en
# → {"job_id":"<id>","status":"queued"}

curl -s -H "X-API-Key: $KEY" localhost:8000/v1/jobs/<id>      # → "status":"done" and the answer
```

The fake model answers in a recognisable format, e.g.
`"[en->yo] Fake answer 1 about a 640x480 image: What is in this picture?"`.

```bash
make test      # run the test suite
make down      # stop everything
```

## Make commands

| Command | What it does |
|---|---|
| `make up` | Build and start Redis, the API (port 8000) and the fake-model worker in the background. |
| `make down` | Stop and remove the containers. |
| `make logs` | Follow all container logs (`docker compose logs -f worker` for one service). |
| `make test` | Run pytest in the `tests` container against the Compose Redis (database 15). |
| `make lint` | `ruff check` and `ruff format --check`. |
| `make format` | Format with ruff, then apply ruff's automatic fixes. |
| `make lock` | Pin requirements newly added to `requirements/*.in`, keeping existing pins. |
| `make upgrade` | Re-resolve every pin to the newest allowed version. |

## Configuration

Settings come from environment variables, read by
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) in
`app/core/config.py`. Two files hold them locally, each documented by its `.example`:

| File | Read by | Holds |
|---|---|---|
| `.env` | API and worker | Shared and API settings ([below](#shared-api-and-worker)) |
| `.env.worker` | worker only | Worker settings, including `HF_TOKEN` ([below](#worker-only)) |

Keeping worker settings in `.env.worker` means the Hugging Face token never reaches the API;
the API logs a warning if `HF_TOKEN` is in its environment anyway. Compose always sets
`REDIS_URL` for its containers and forces `USE_FAKE_MODEL=true` for its worker. Durations are
in seconds.

### Shared (API and worker)

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL. |
| `IMAGE_TTL` | `900` | How long an uploaded image is kept. Jobs still queued after this expire. |
| `RESULT_TTL` | `3600` | How long a finished job can be polled. |
| `SESSION_TTL` | `86400` | Session lifetime since last use. |
| `JOB_TIMEOUT` | `300` | Longest a job may run. Size for the slowest case: a non-English question is 3 generations. |
| `MAX_SESSION_TURNS` | `8` | Conversation turns kept per session and sent to the model. |
| `HEARTBEAT_INTERVAL` | `10` | How often the worker publishes its state. |
| `HEARTBEAT_STALE_AFTER` | `30` | Heartbeat age after which the worker counts as gone. Must exceed `HEARTBEAT_INTERVAL`. |
| `LOG_CONTENT` | `false` | Log questions and answers. Off by default: user content may be personal. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

### API only

| Variable | Default | Description |
|---|---|---|
| `API_KEYS` | (none, **required**) | Comma-separated client keys for `X-API-Key`. The API refuses to start without one. |
| `CORS_ORIGINS` | empty (CORS off) | Comma-separated browser origins. Leave empty: the API is called server to server. |
| `MAX_IMAGE_MB` | `4` | Largest image. Requests over this + 64 KB are rejected before being read. |
| `MAX_NEW_TOKENS` | `256` | Token budget per generation (the translated answer gets 2x). Bounds job latency. |
| `MAX_QUESTION_CHARS` | `1000` | Longest question. |
| `MAX_QUEUE_DEPTH` | `100` | Queued + running jobs before new ones get `503 queue_full`. |
| `RATE_LIMIT_SUBMIT_PER_CLIENT` | `10` | `POST /v1/jobs/chat` per visitor per minute. |
| `RATE_LIMIT_SUBMIT_PER_KEY` | `120` | `POST /v1/jobs/chat` per key (all visitors) per minute. |
| `RATE_LIMIT_READ_PER_CLIENT` | `120` | Other requests per visitor per minute (polling every 2 s uses 30). |
| `RATE_LIMIT_READ_PER_KEY` | `3000` | Other requests per key per minute. |
| `RETRY_AFTER_MODEL_UNAVAILABLE` | `30` | `Retry-After` sent with `503 model_unavailable`. |
| `RETRY_AFTER_QUEUE_FULL` | `30` | `Retry-After` sent with `503 queue_full`. |
| `RETRY_AFTER_SERVICE_UNAVAILABLE` | `5` | `Retry-After` sent with `503 service_unavailable` (Redis unreachable). |

### Worker only

Put these in `.env.worker`.

| Variable | Default | Description |
|---|---|---|
| `HF_TOKEN` | (none) | Hugging Face token with access to the gated `NCAIR1/N-ATLaS`. **Required unless `USE_FAKE_MODEL=true`.** Never set it on the API. |
| `ATLAS_STAGE` | `2b` | Checkpoint: `2b` (recommended), `2`, or `1` (captioner). |
| `LOAD_IN_4BIT` | unset | `true`/`false`; unset lets the SDK decide (4-bit on GPUs under 20 GB). |
| `USE_FAKE_MODEL` | `false` | Use `FakeAtlasVision` instead of the real model. `.env.worker.example` and Compose set it to `true`. |
| `FAKE_LOAD_SECONDS` | `2` | Fake model: simulated load time. |
| `FAKE_GENERATION_SECONDS` | `0.5` | Fake model: time per simulated generation. |
| `FAKE_LOAD_ERROR` | unset | Fake model: fail at startup with `AuthenticationError`, `GatedModelAccessError` or `ModelLoadError`. |

With the fake model, a question containing `__raise_<ErrorName>__` (for example
`__raise_ImageLoadError__`, `__raise_RuntimeError__`) makes that job raise the error, to test
error handling end to end.

## API overview

Full details, examples and error handling: [docs/frontend-api.md](docs/frontend-api.md).

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/jobs/chat` | key | Submit an image and question (multipart). `202` with `job_id`. |
| `GET /v1/jobs/{job_id}` | key | Job status; the answer when `done`, the error when `failed`. |
| `POST /v1/sessions` | key | Start a conversation. `201` with `session_id`. |
| `GET /v1/sessions/{id}` | key | Conversation history and any job in progress. |
| `DELETE /v1/sessions/{id}` | key | Delete a conversation. `204`. |
| `GET /v1/model` | key | Worker state (`ready`, `loading`, `failed`, `unavailable`), stage, languages. |
| `GET /health` | none | API and Redis liveness: `200` or `503`. |
| `GET /docs`, `/openapi.json` | key | Interactive docs and schema (send `X-API-Key`, e.g. with a browser extension). |

Every error is `{"error": {"code": "...", "message": "..."}}`, sometimes with extra detail
fields such as `field` or `job_id`. Validation problems are real 4xx/5xx responses; failures
inside a job come back in a `200` poll with `"status": "failed"`.

## Security

- **API keys:** required on everything but `/health`, compared in constant time (SHA-256
  digests against every key). Keys sit behind a small `KeyStore` interface
  (`app/api/auth.py`), so a database can replace `API_KEYS` later. Jobs and sessions store a
  key id derived from the key, never the key itself.
- **Key before body:** the API checks the key before reading any request body, and never
  reads more than `MAX_IMAGE_MB` + 64 KB.
- **Visitors:** the Vercel route sends an anonymous `X-Client-Id`, trusted only with a valid
  key. Jobs and sessions belong to their key and client id; anyone else gets `404`.
- **Not for browsers:** call the API from a server route that holds the key. CORS is off by
  default.
- **`HF_TOKEN`** lives in `.env.worker`, which only the worker reads. It is never logged (the
  SDK's own token log line is silenced) and never returned; the API warns if it finds the
  token in its own environment.
- **No remote images:** the API accepts uploads only. The worker builds the client with
  `allow_remote_images=False` and passes image **bytes** to the SDK, never a path or URL.
- **User content** (questions, answers) is logged only with `LOG_CONTENT=true`.
- **Queue payloads** are JSON, so data read back from Redis can never run code.

## Operations

- **Scaling:** one worker per GPU (`max_jobs=1`; the SDK serialises generation anyway). Add
  GPUs by adding workers; add API replicas freely.
- **Health:** `/health` for load balancers (API + Redis); `GET /v1/model` for model readiness.
  The worker logs the full model details when ready, and the reason if loading fails (then it
  exits with code 1; the container restarts it).
- **Timeouts:** `JOB_TIMEOUT` fails a job as `job_interrupted`, but a generation already
  running on the GPU cannot be stopped; it finishes in the background and the next job waits
  for it.
- **Redis memory:** images are held in Redis while queued. Worst case is about
  `MAX_QUEUE_DEPTH` x `MAX_IMAGE_MB` (100 x 4 MB = 400 MB); size Redis accordingly or lower
  either setting. Redis needs no persistence.
- **Redis version:** developed and tested against Redis 7.4.

## Development

### Layout

```
app/
  core/      shared, no torch: config, schemas, errors, Redis helpers, job store,
             sessions, heartbeat reader, queue wrapper, image check, rate limiter
  api/       FastAPI app: routes, auth, rate-limit dependencies, middleware, error handlers
  worker/    arq worker: entry point, job function, model loading, heartbeat, FakeAtlasVision
tests/       pytest suite (runs against a real Redis and a real arq worker, with the fake model)
docker/      api.Dockerfile (runtime + dev targets), worker.Dockerfile (fake target)
docs/        frontend-api.md
requirements/
  base.in    shared by API and worker (arq, redis, pillow, pydantic-settings)
  api.in     base + FastAPI/uvicorn
  dev.in     api + pytest, httpx, ruff
  sdk.txt    testatlasvision, hash-pinned, installed with --no-deps
```

### Dependencies

Each `requirements/*.in` lists direct dependencies; `make lock` compiles it to a fully pinned
`*.txt` with [uv](https://github.com/astral-sh/uv) inside a Python 3.11 container. Never edit
the `*.txt` files by hand.

The SDK declares torch as a dependency, but the modules the API imports never load it, so the
API and fake worker install it with `--no-deps` from the hash-pinned `requirements/sdk.txt`. A
test checks that importing the API and worker never imports torch. The GPU worker image (with
torch and the SDK's full dependencies) is not built yet.

### Tests

`make test` runs the suite in the `tests` container against the Compose Redis, **database
15**, which the tests flush before every test. Never point the test container at a Redis
that holds real data. The tests use the fake model and drive a real arq worker in burst mode,
covering the full submit-run-poll loop, sessions, errors, auth, ownership and limits.

### CI

`.github/workflows/ci.yml` runs `make lint` and `make test` on every push and pull request,
using the same Docker setup as local development.

## Troubleshooting

### The Docker build is slow or seems stuck

The first `make up` or `make test` builds images and downloads Python packages from PyPI. On a
slow connection this takes several minutes (we measured 3-4 minutes per image at about
200 kB/s), and piping the output to `tail` or similar hides all progress until the end.

- **Watch progress:** `docker compose --profile test build --progress=plain tests` (or `api`,
  `worker`).
- **Let it finish once.** Pip downloads are kept in a BuildKit cache between builds, so later
  builds, even after a requirements change, only download what changed.
- **"failed to solve ... pip install ... exit code"** is usually a network timeout. Run the
  command again: pip already retries (10 times, 60 s timeout), and finished downloads are
  cached.
- **Pre-pull the base images** while you do something else:
  `docker pull python:3.11-slim && docker pull redis:7.4-alpine`.
- **BuildKit must be on** for the cache (the default since Docker 23). If builds complain
  about `--mount`, set `DOCKER_BUILDKIT=1` or update Docker.
- `docker builder prune` clears the pip cache, so the next build downloads everything again.
- **`make lock` is slow too:** it installs uv in a fresh container each time; downloads are
  cached in the `atlasvision-pip-cache` volume.

### Other problems

| Symptom | Cause and fix |
|---|---|
| API log warns `HF_TOKEN is set in the API's environment` | Move `HF_TOKEN` from `.env` to `.env.worker`. |
| `api` container exits; log says `API_KEYS is empty` | Set `API_KEYS` in `.env`, then `make up`. |
| `401 unauthorized` | Missing or wrong `X-API-Key`. It must match one of `API_KEYS` exactly. |
| `401` on `/docs` | The docs need the key too. Use a browser extension that adds `X-API-Key`, or fetch `/openapi.json` with curl. |
| `503 model_unavailable` | No ready worker. Check `docker compose ps` and `docker compose logs worker`; `GET /v1/model` shows the state. |
| Worker keeps restarting | The model failed to load; the log line `Model failed to load (...)` says why. With the real model, check `HF_TOKEN` and access to `NCAIR1/N-ATLaS`. |
| `port is already allocated` | Something else uses port 8000 or 6379. Stop it, or change the host port in `docker-compose.yml`. |
| `env_file ... required` or similar Compose error | Compose is older than 2.24. Update Docker / Compose. |
| Files created by containers are owned by root | Run `make lock`, `make format` etc. through `make`, which runs containers as your user. |
| Jobs fail with `job_expired` or `image_expired` | Jobs waited longer than `IMAGE_TTL`; the worker is overloaded or was down. |
