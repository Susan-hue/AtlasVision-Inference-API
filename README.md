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

## Sessions (follow-up questions)

`POST /v1/sessions` returns a `session_id`; pass it as the `session_id` form field on
`POST /v1/jobs/chat`. Without it, each job runs with no memory.

- **Re-send the image with every question.** The model always needs an image; the session
  only stores the conversation text (in English, as the SDK does).
- **One question at a time per session.** While a job on a session is queued or running, a
  new job on the same session gets `409 session_busy` with the `job_id` still in progress.
  Clients should disable sending until that job is `done` or `failed`
  (`GET /v1/sessions/{id}` shows it as `active_job_id`).
- Only the last `MAX_SESSION_TURNS` turns are kept and sent to the model.
- Sessions expire after `SESSION_TTL` without use. `DELETE /v1/sessions/{id}` removes one at
  once; a job already running on it still returns its answer, but nothing is saved back.

## Errors

Every error body is `{"error": {"code": "...", "message": "..."}}`. Problems found while
validating a request are real 4xx/5xx responses. Problems inside a job are reported by
`GET /v1/jobs/{job_id}` as `200` with `"status": "failed"` and the same error object.

| Code | Where | Meaning |
|---|---|---|
| `invalid_request`, `invalid_question`, `question_too_long`, `invalid_question_lang` | 422 | Bad form fields |
| `unsupported_language` | 422 / job | Language not en, ig, yo or ha |
| `unsupported_media_type`, `image_too_large`, `invalid_image` | 415 / 413 / 422 / job | Bad image |
| `invalid_input` | job | The model rejected the input |
| `session_not_found`, `job_not_found` | 404 / job | Unknown or expired id |
| `session_busy` | 409 / job | Another question on this session is in progress |
| `model_unavailable` | 503 / job | No worker ready, or the model failed to load |
| `image_expired`, `job_expired` | job | The job waited too long |
| `job_interrupted`, `worker_lost`, `internal_error` | job / 500 | The job did not finish |
