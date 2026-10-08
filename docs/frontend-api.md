# AtlasVision API: guide for the frontend

This guide is for whoever builds the website and its Vercel server route. It covers what to send,
what comes back, and how to handle every outcome. The API answers questions about an image in
English, Igbo, Yoruba and Hausa.

- [How it works](#how-it-works)
- [The Vercel route](#the-vercel-route)
- [Asking a question](#asking-a-question)
- [Polling for the answer](#polling-for-the-answer)
- [Conversations (sessions)](#conversations-sessions)
- [Other endpoints](#other-endpoints)
- [Errors](#errors)
- [Rate limits and Retry-After](#rate-limits-and-retry-after)
- [Preparing the image](#preparing-the-image)
- [Checklist](#checklist)

## How it works

Answering takes a few seconds (longer for Igbo, Yoruba and Hausa), so the API never makes the
browser wait on one long request:

```
browser ──► Vercel route ──POST /v1/jobs/chat──► API ──► 202 {"job_id": "..."}
browser ──► Vercel route ──GET /v1/jobs/{id}───► API ──► {"status": "queued" | "running" | ...}
            ... every 2 seconds ...
browser ──► Vercel route ──GET /v1/jobs/{id}───► API ──► {"status": "done", "result": {...}}
```

1. Submit the question and image. The API checks them and replies `202` with a `job_id`.
2. Poll `GET /v1/jobs/{job_id}` every 2 seconds.
3. Stop when `status` is `done` (show `result`) or `failed` (show `error`).

All requests go through your Vercel server route. The browser never talks to the API directly,
and never sees the API key.

## The Vercel route

### Headers on every request

| Header | Value |
|---|---|
| `X-API-Key` | The API key, from a server-only environment variable (never a `NEXT_PUBLIC_` one). |
| `X-Client-Id` | An anonymous id for the visitor, kept in a cookie (see below). |

- **`X-Client-Id`** keeps visitors apart: each visitor only sees their own jobs and
  conversations, and rate limits apply per visitor. It must be 8-128 characters of letters,
  digits, `-` or `_` (a UUID from `crypto.randomUUID()` is ideal). Generate it once per
  visitor and store it in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie. Never derive it from
  personal data (email, IP address, name).
- **Set both headers in the route.** Ignore any `X-API-Key` or `X-Client-Id` the browser sends,
  so a visitor cannot borrow someone else's id.
- A visitor who clears their cookies gets a new id. Their old conversations then become
  unreachable (404) and simply expire.

### Example (Next.js App Router)

A sketch to adapt. It forwards only the fields the API expects and passes through the status
code and the `Retry-After` header.

```ts
// lib/atlas.ts
import { cookies } from "next/headers";

const API_URL = process.env.ATLAS_API_URL!; // e.g. https://atlas-api.example.com
const API_KEY = process.env.ATLAS_API_KEY!; // server-only

export async function atlasHeaders(): Promise<Record<string, string>> {
  const jar = await cookies();
  let clientId = jar.get("atlas_client")?.value;
  if (!clientId) {
    clientId = crypto.randomUUID();
    jar.set("atlas_client", clientId, {
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
      maxAge: 60 * 60 * 24 * 365,
    });
  }
  return { "X-API-Key": API_KEY, "X-Client-Id": clientId };
}

export async function atlasFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { ...(await atlasHeaders()) },
    cache: "no-store",
  });
  const headers = new Headers({ "Content-Type": "application/json" });
  const retryAfter = res.headers.get("Retry-After");
  if (retryAfter) headers.set("Retry-After", retryAfter);
  return new Response(res.status === 204 ? null : res.body, { status: res.status, headers });
}
```

```ts
// app/api/atlas/chat/route.ts
import { atlasFetch } from "@/lib/atlas";

const FIELDS = ["question", "lang", "question_lang", "session_id"] as const;

export async function POST(req: Request) {
  const incoming = await req.formData();
  const form = new FormData();
  const image = incoming.get("image");
  if (image instanceof Blob) form.append("image", image, "photo.jpg");
  for (const name of FIELDS) {
    const value = incoming.get(name);
    if (typeof value === "string") form.append(name, value);
  }
  return atlasFetch("/v1/jobs/chat", { method: "POST", body: form });
}
```

```ts
// app/api/atlas/jobs/[id]/route.ts
import { atlasFetch } from "@/lib/atlas";

export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return atlasFetch(`/v1/jobs/${encodeURIComponent(id)}`);
}
```

Session routes (`POST /v1/sessions`, `GET` and `DELETE /v1/sessions/{id}`) follow the same
pattern as the job route.

## Asking a question

`POST /v1/jobs/chat` with `multipart/form-data`.

| Field | Required | Value |
|---|---|---|
| `image` | yes | JPEG, PNG or WebP, at most 4 MB. Send the image with **every** question, including follow-ups. |
| `question` | yes | 1-1000 characters. |
| `lang` | yes | Language of the answer: `en`, `ig`, `yo` or `ha` (names such as `Yoruba` also work). |
| `question_lang` | yes | Language the question is written in. Must be `en` or the same as `lang`. |
| `session_id` | no | A conversation id from `POST /v1/sessions`, for follow-up questions. Leave it out (or empty) for a one-off question. |

The whole request must be under about 4 MB (Vercel's own limit is 4.5 MB), so
[resize images first](#preparing-the-image).

### `question_lang` rules

The model itself works in English. For other languages the API translates around it:

| The user... | `lang` | `question_lang` | Steps | Speed |
|---|---|---|---|---|
| asks in English, wants English | `en` | `en` | answer | fastest |
| asks in English, wants Yoruba | `yo` | `en` | answer, translate answer | about 2x |
| asks in Yoruba, wants Yoruba | `yo` | `yo` | translate question, answer, translate answer | about 3x |
| asks in Hausa, wants Yoruba | | | **not allowed**: `422 invalid_question_lang` | |

If the user can type in their own language, set `question_lang` to the same value as `lang`.
If your UI only accepts English questions, always send `question_lang=en`.

### Request

```bash
curl -X POST https://atlas-api.example.com/v1/jobs/chat \
  -H "X-API-Key: $ATLAS_API_KEY" -H "X-Client-Id: 3f2b8c1e-9d4a-4e6b-a1c2-7f0e9d8b6a5c" \
  -F image=@photo.jpg -F question="What is in this picture?" -F lang=yo -F question_lang=en
```

### Response: `202 Accepted`

```json
{"job_id": "4b19d57942c64b17a421f4f798f24022", "status": "queued"}
```

The `Location` header also points at `/v1/jobs/4b19d57942c64b17a421f4f798f24022`.

Before accepting, the API checks, in this order: the key and the request size; the rate limits;
the fields and languages; that the model is available and the queue is not full; the image;
and finally the conversation (it must exist, belong to this visitor, and not be busy). The first
failed check decides the [error](#errors) you get.

## Polling for the answer

`GET /v1/jobs/{job_id}` every **2 seconds** until `status` is `done` or `failed`.

| `status` | Meaning |
|---|---|
| `queued` | Waiting for the model. |
| `running` | The model is working on it. |
| `done` | `result` holds the answer. Stop polling. |
| `failed` | `error` says why. Stop polling. |

A poll always returns `200` while the job exists, **including when the job failed**: check
`status`, not just the HTTP code.

### While waiting

```json
{
  "job_id": "4b19d57942c64b17a421f4f798f24022",
  "status": "running",
  "session_id": null,
  "created_at": "2026-10-08T20:37:55.467578Z",
  "finished_at": null,
  "result": null,
  "error": null
}
```

### Done

```json
{
  "job_id": "4b19d57942c64b17a421f4f798f24022",
  "status": "done",
  "session_id": null,
  "created_at": "2026-10-08T20:37:55.467578Z",
  "finished_at": "2026-10-08T20:37:58.897544Z",
  "result": {
    "answer": "(the answer, in Yoruba)",
    "english_answer": "A red car parked in front of a house.",
    "english_question": "What is in this picture?",
    "language": "yo"
  },
  "error": null
}
```

| Field | Show it as |
|---|---|
| `answer` | The answer, in the language the user chose (`language`). |
| `english_answer` | The English answer, e.g. as a secondary line or "show in English". |
| `english_question` | What the model was actually asked (the question, translated to English if needed). |

### Failed

```json
{
  "job_id": "4b19d57942c64b17a421f4f798f24022",
  "status": "failed",
  "session_id": null,
  "created_at": "2026-10-08T20:37:55.467578Z",
  "finished_at": "2026-10-08T20:37:56.120013Z",
  "result": null,
  "error": {"code": "invalid_image", "message": "The image could not be read."}
}
```

### How long to keep polling

A job waits at most 15 minutes in the queue and runs at most 5 minutes; after that the poll
reports `failed` (`job_expired`, `job_interrupted` or `worker_lost`), so you will always get a
final status. Finished jobs can be polled for 1 hour, then return `404 job_not_found`.

```ts
// Browser side: poll your own route until the job finishes.
async function waitForAnswer(jobId: string, signal?: AbortSignal) {
  for (;;) {
    const res = await fetch(`/api/atlas/jobs/${jobId}`, { signal });
    if (res.status === 429 || res.status === 503) {
      await sleep(Number(res.headers.get("Retry-After") ?? 5) * 1000);
      continue;
    }
    const body = await res.json();
    if (!res.ok) throw body.error;          // e.g. 404 job_not_found
    if (body.status === "done") return body.result;
    if (body.status === "failed") throw body.error;
    await sleep(2000);
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
```

## Conversations (sessions)

A session lets the user ask follow-up questions about the same picture ("What colour is it?").

1. **Start a conversation:** `POST /v1/sessions` → `201 {"session_id": "..."}`.
2. **Ask:** send `session_id` with each `POST /v1/jobs/chat`, **and the image every time**. The
   session remembers the conversation text, not the picture.
3. **One question at a time:** disable the send button while a question in this conversation
   is queued or running. If a second question arrives anyway, the API replies `409
   session_busy` with the `job_id` still in progress: keep polling that job instead.
4. **New chat:** `DELETE /v1/sessions/{id}` (`204`), then start a new session. Deleting while a
   question is still being answered is fine: that answer still arrives through its job, it is
   just not saved to the deleted conversation.

Only the last 8 exchanges are remembered. A session expires after 24 hours without use; after
that, using it returns `404 session_not_found`, so start a new one.

### `409 session_busy`

```json
{
  "error": {
    "code": "session_busy",
    "message": "This conversation is still answering your previous question. Please wait for that answer before asking another.",
    "job_id": "31ed0b8dd79a4fc386e54c60a119860c"
  }
}
```

### Restoring a conversation

`GET /v1/sessions/{id}` returns the conversation so you can redraw it, for example after a page
reload. `active_job_id` is the job still being answered, if any: resume polling it and keep
sending disabled.

```json
{
  "session_id": "2be2b89b48d94c439d9c4a869a8b2d10",
  "history": [
    {
      "question": "(the question, in Yoruba)",
      "question_lang": "yo",
      "lang": "yo",
      "answer": "(the answer, in Yoruba)",
      "english_question": "What is in this picture?",
      "english_answer": "A red car parked in front of a house."
    },
    {
      "question": "What colour is the door?",
      "question_lang": "en",
      "lang": "yo",
      "answer": "(the answer, in Yoruba)",
      "english_question": "What colour is the door?",
      "english_answer": "The door is blue."
    }
  ],
  "active_job_id": null
}
```

`question` and `answer` are what the user typed and saw; the `english_` fields are what the
model saw.

## Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /v1/model` | `{"state": "ready" \| "loading" \| "failed" \| "unavailable", "stage": "2b", "supported_languages": {"en": "English", ...}}`. Useful to show "the service is starting" instead of an error. Not needed for normal use. |
| `GET /health` | `{"status": "ok", "redis": "ok"}` (or `503` with `"degraded"`). For uptime monitors; needs no key. |

Questions can be submitted while `state` is `loading`; they wait until the model is ready.

## Errors

Every error has the same shape, with an English `message` meant for developers and logs:

```json
{"error": {"code": "rate_limited", "message": "Too many requests. Please slow down and try again shortly."}}
```

Use `code` to decide what to do and what to show. The suggested messages below are written for
end users; translate them as you need.

**Where it comes from:** "submit" means the `POST /v1/jobs/chat` response; "poll" means a
`failed` job in a `200` poll response; "any" means any endpoint.

### Problems with the request

| Code | HTTP | Where | Suggested message | What to do |
|---|---|---|---|---|
| `invalid_question` | 422 | submit | "Please type a question." | Block blank questions in the UI. (A completely empty `question` field comes back as `invalid_request` instead.) |
| `question_too_long` | 422 | submit | "Your question is too long. Please shorten it." | Limit the input to 1000 characters. |
| `unsupported_language` | 422 | submit, poll | "That language isn't supported yet." | Only offer en, ig, yo, ha. |
| `invalid_question_lang` | 422 | submit | "Please ask in English or in the language you chose." | Send `question_lang` = `en` or `lang`. |
| `invalid_request` | 422 | submit | "Something went wrong. Please try again." | A field is missing, empty or malformed; `message` names it (e.g. `"image: Field required"`). |
| `unsupported_media_type` | 415 | submit | "Please choose a JPEG, PNG or WebP picture." | Only accept those types. |
| `image_too_large` | 413 | submit | "This picture is too large. Please choose a smaller one." | Resize before upload. |
| `request_too_large` | 413 | submit | "This picture is too large. Please choose a smaller one." | Resize before upload. |
| `invalid_image` | 422 | submit, poll | "We couldn't read this picture. Please try another one." | The file is damaged or not really an image. |
| `invalid_input` | 422 | poll | "We couldn't answer that. Please rephrase your question." | The model rejected the input. |

### Conversations and jobs

| Code | HTTP | Where | Suggested message | What to do |
|---|---|---|---|---|
| `session_busy` | 409 | submit, poll | "Still answering your previous question…" | Keep polling `error.job_id`; keep sending disabled. |
| `session_not_found` | 404 | any session call, submit, poll | "This conversation has expired. Please start a new one." | Create a new session. |
| `job_not_found` | 404 | poll | "This answer is no longer available. Please ask again." | Stop polling. |
| `image_expired` | (poll) | poll | "That took too long. Please ask again." | Resubmit with the image. |
| `job_expired` | (poll) | poll | "The service is very busy. Please ask again." | Resubmit, perhaps after a short wait. |
| `job_interrupted` | (poll) | poll | "Something went wrong while answering. Please ask again." | Resubmit. |
| `worker_lost` | (poll) | poll | "Something went wrong while answering. Please ask again." | Resubmit. |

### Availability and limits

| Code | HTTP | Where | Suggested message | What to do |
|---|---|---|---|---|
| `rate_limited` | 429 | any | "You're asking very quickly. Please wait a moment." | Wait `Retry-After` seconds. |
| `queue_full` | 503 | submit | "We're very busy right now. Please try again in a moment." | Wait `Retry-After` seconds. |
| `model_unavailable` | 503 | submit, poll | "The service is starting up or unavailable. Please try again shortly." | Wait `Retry-After` seconds; check `GET /v1/model`. |
| `service_unavailable` | 503 | any | "The service is temporarily unavailable. Please try again shortly." | Wait `Retry-After` seconds. |
| `internal_error` | 500, poll | any | "Something went wrong. Please try again." | Retry once; report if it persists. |

### Problems in the route (should never reach users)

| Code | HTTP | Meaning |
|---|---|---|
| `unauthorized` | 401 | Missing or wrong `X-API-Key`. Check the route's environment variable. |
| `invalid_client_id` | 400 | `X-Client-Id` is not 8-128 letters, digits, `-` or `_`. |
| `not_found`, `method_not_allowed` | 404, 405 | Wrong URL or HTTP method. |

Show users the generic "Something went wrong. Please try again." for these, and log them.

Errors are deliberately vague about other people's data: a job or conversation that belongs to
another visitor returns the same `404` as one that never existed.

## Rate limits and Retry-After

| Limit | Per minute | Counts |
|---|---|---|
| Questions per visitor | 10 | `POST /v1/jobs/chat` |
| Questions, all visitors together | 120 | `POST /v1/jobs/chat` |
| Other requests per visitor | 120 | polls, sessions, `/v1/model` |
| Other requests, all visitors together | 3000 | polls, sessions, `/v1/model` |

(These are the defaults; the API operator can change them.) Polling one job every 2 seconds uses
30 requests a minute, well inside the per-visitor limit.

- Limits use a sliding one-minute window, and rejected requests count too, so retrying in a
  tight loop only extends the wait.
- `429`, `503 queue_full`, `503 model_unavailable` and `503 service_unavailable` include a
  `Retry-After` header: the number of seconds to wait (for `429`, up to 120).
- **Polls:** after a `429`, wait `Retry-After` seconds and continue polling.
- **Questions:** don't resubmit automatically. Show the message, and let the user try again
  once the wait is over (for example by disabling the send button for `Retry-After` seconds).

## Preparing the image

The model looks at a small, 224 x 224 pixel version of the picture, so large photos only make
uploads slower. Before uploading:

- **Resize** so the longest side is about **1024 px**.
- **Re-encode as JPEG** (quality about 0.85). This also strips EXIF metadata, which can contain
  the photo's GPS location.
- **Respect the photo's orientation** so portrait phone photos aren't sideways.
- Keep the file **well under 4 MB** (a 1024 px JPEG is typically 100-400 KB).

```ts
// Browser side: resize to at most 1024 px and re-encode as JPEG.
async function prepareImage(file: File, maxSide = 1024): Promise<Blob> {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d")!.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("encode failed"))), "image/jpeg", 0.85),
  );
}
```

## Checklist

- [ ] The API key lives only in a server-side environment variable.
- [ ] Every request sends `X-API-Key` and `X-Client-Id`, set by the route, never by the browser.
- [ ] The visitor id is a random UUID in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie.
- [ ] Images are resized to about 1024 px and re-encoded as JPEG before upload.
- [ ] `question_lang` is `en` or equal to `lang`.
- [ ] The image is sent with every question, including follow-ups.
- [ ] Polling every 2 seconds stops on `done` or `failed`, and checks `status`, not just HTTP 200.
- [ ] Sending is disabled while a conversation has a question in progress; `409` resumes polling `error.job_id`.
- [ ] `Retry-After` is honoured for `429` and `503`.
- [ ] Every error `code` maps to a friendly message.
