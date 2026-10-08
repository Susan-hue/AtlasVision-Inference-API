# AtlasVision Inference API

A job-queue API around the [`testatlasvision`](https://pypi.org/project/testatlasvision/0.1.0/)
SDK (multilingual visual question answering in English, Igbo, Yoruba and Hausa).

> Work in progress. The full documentation (architecture, endpoints, every setting) is
> written in Phase 5.

## Not for direct browser use

The API is meant to be called **server to server**, e.g. from a Vercel route that holds the API
key. Browsers should never see the key, so CORS is disabled by default (`CORS_ORIGINS` empty).

## Running locally

Only Docker (with Compose) is required; everything runs in Python 3.11 containers.

```bash
cp .env.example .env
# Set a key in .env (the API refuses to start without one):
#   API_KEYS=<output of: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
make up                  # start redis + api + fake-model worker in the background
curl -i localhost:8000/health
curl -s -H "X-API-Key: <your key>" localhost:8000/v1/model
make down
```

| Command | What it does |
|---|---|
| `make up` / `make down` | Start / stop the local stack |
| `make logs` | Follow container logs |
| `make test` | Run pytest in a container against the Compose Redis (DB 15) |
| `make lint` / `make format` | Ruff checks / auto-fix and format |
| `make lock` / `make upgrade` | Pin newly added requirements / re-resolve every pin |

Configuration is read from environment variables; `.env.example` documents each one.

## What the Vercel route must send

Every endpoint except `/health` needs an API key. All visitors share the Vercel route's key,
so the route also identifies the visitor. On **every** request to the API the route must:

1. **Send `X-API-Key: <key>`.** Read the key from a server-only environment variable (never a
   `NEXT_PUBLIC_` one) and never send it to the browser.
2. **Send `X-Client-Id: <visitor id>`.** An anonymous id the route generates once per visitor
   (e.g. `crypto.randomUUID()`) and keeps in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie.
   It must be 8-128 characters of letters, digits, `-` or `_`; anything else gets
   `400 invalid_client_id`. Never derive it from personal data.
3. **Set both headers itself.** Drop any `X-API-Key` or `X-Client-Id` arriving from the
   browser, so a visitor cannot pick someone else's id.
4. **Forward the chat upload as `multipart/form-data`** with `image`, `question`, `lang`,
   `question_lang` and optional `session_id`, under 4 MB in total (resize to about 1024 px).
5. **Pass through status codes and `Retry-After`** (`429`, `503`) so the UI can back off.

The API only trusts `X-Client-Id` on requests with a valid key. Without the header, the key
itself is treated as the client. Jobs and sessions belong to the key and client id that
created them; anyone else gets `404`, exactly as if they did not exist.

```ts
// app/api/atlas/[...path]/route.ts (sketch)
const headers = {
  "X-API-Key": process.env.ATLAS_API_KEY!,           // server-only
  "X-Client-Id": (await cookies()).get("atlas_client")!.value, // set on first visit
};
```

### Limits

| Limit | Default (per minute) | Applies to |
|---|---|---|
| `RATE_LIMIT_SUBMIT_PER_CLIENT` | 10 | `POST /v1/jobs/chat` per visitor |
| `RATE_LIMIT_SUBMIT_PER_KEY` | 120 | `POST /v1/jobs/chat` across all visitors |
| `RATE_LIMIT_READ_PER_CLIENT` | 120 | every other endpoint per visitor (polling every 2 s = 30) |
| `RATE_LIMIT_READ_PER_KEY` | 3000 | every other endpoint across all visitors |

Limits use a sliding window: the previous minute's count still applies, weighted by how much
of the current minute is left, so a burst at a minute boundary cannot double the limit.
Rejected requests count too. Over a limit: `429 rate_limited` with `Retry-After`, the number
of seconds until the next request would be accepted (up to 120). More than
`MAX_QUEUE_DEPTH` queued or running jobs: `503 queue_full` with `Retry-After`.

The API checks the key before reading any request body, so only valid key holders can make
it read an upload. Bodies over `MAX_IMAGE_MB` + 64 KB get `413 request_too_large`, rejected
unread when `Content-Length` declares it, otherwise as soon as the stream passes the limit.
`/docs` and `/openapi.json` also need the key.

`GET /v1/model` returns only `state` (`ready`, `loading`, `failed`, `unavailable`), `stage`
and `supported_languages`; full model details are in the worker log.

## Sessions (follow-up questions)

`POST /v1/sessions` returns a `session_id`; pass it as the `session_id` form field on
`POST /v1/jobs/chat`. Without it, each job runs with no memory.

- **Re-send the image with every question.** The model always needs an image; the session
  only stores the conversation text.
- `GET /v1/sessions/{id}` returns each turn as the user saw it (`question`, `question_lang`,
  `lang`, `answer`) and as the model saw it (`english_question`, `english_answer`), so a
  client can rebuild the conversation in the user's language.
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
| `unauthorized` | 401 | Missing or wrong `X-API-Key` |
| `invalid_client_id` | 400 | Malformed `X-Client-Id` |
| `rate_limited` | 429 | Over a rate limit; see `Retry-After` |
| `queue_full` | 503 | Too many jobs waiting; see `Retry-After` |
| `request_too_large` | 413 | Request body over the size limit |
| `invalid_request`, `invalid_question`, `question_too_long`, `invalid_question_lang` | 422 | Bad form fields |
| `unsupported_language` | 422 / job | Language not en, ig, yo or ha |
| `unsupported_media_type`, `image_too_large`, `invalid_image` | 415 / 413 / 422 / job | Bad image |
| `invalid_input` | job | The model rejected the input |
| `session_not_found`, `job_not_found` | 404 / job | Unknown or expired id |
| `session_busy` | 409 / job | Another question on this session is in progress |
| `model_unavailable` | 503 / job | No worker ready, or the model failed to load |
| `image_expired`, `job_expired` | job | The job waited too long |
| `job_interrupted`, `worker_lost`, `internal_error` | job / 500 | The job did not finish |
