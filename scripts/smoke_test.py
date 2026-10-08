#!/usr/bin/env python3
"""Smoke test for a deployed AtlasVision API running the real model.

    ATLAS_API_KEY=<key> python3 scripts/smoke_test.py --image photo.jpg [--url http://localhost:8000]

Waits until the model is ready, then sends one chat job per case (English question with a
Yoruba answer, a Yoruba, an Igbo and a Hausa question, a session follow-up), polls each, and prints
the answers and timings. Exits 1 if anything fails. Standard library only, so it runs on the GPU
host without installing anything.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    lang: str
    question_lang: str


CASES = [
    Case("English question, Yoruba answer", "What is in this picture?", "yo", "en"),
    # Sample questions in each language; replace them with your own if you prefer.
    Case("Yoruba question, Yoruba answer", "Kí ni ó wà nínú àwòrán yìí?", "yo", "yo"),
    Case("Igbo question, Igbo answer", "Kedu ihe dị na foto a?", "ig", "ig"),
    Case("Hausa question, Hausa answer", "Me ke cikin wannan hoton?", "ha", "ha"),
]
SESSION_QUESTIONS = ["What is in this picture?", "What colour is it?"]


class SmokeFailure(Exception):
    pass


class Api:
    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.headers = {"X-API-Key": key, "X-Client-Id": f"smoke-test-{uuid.uuid4().hex[:12]}"}

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
        auth: bool = True,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        headers = dict(self.headers) if auth else {}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {
                    "error": {"code": "non_json", "message": raw[:200].decode(errors="replace")}
                }
            return exc.code, payload, dict(exc.headers)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SmokeFailure(f"{method} {path}: cannot reach the API ({exc})") from exc

    def submit(self, image: Path, case: Case, session_id: str | None = None) -> str:
        fields = {"question": case.question, "lang": case.lang, "question_lang": case.question_lang}
        if session_id:
            fields["session_id"] = session_id
        body, content_type = multipart(fields, image)
        for _ in range(3):
            status, payload, headers = self.request("POST", "/v1/jobs/chat", body, content_type)
            if status == 202:
                return payload["job_id"]
            if status == 429:
                time.sleep(int(headers.get("Retry-After", "5")))
                continue
            raise SmokeFailure(f"submit returned {status}: {describe(payload)}")
        raise SmokeFailure("submit was rate limited three times")

    def wait_for_job(self, job_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, payload, headers = self.request("GET", f"/v1/jobs/{job_id}")
            if status == 429:
                time.sleep(int(headers.get("Retry-After", "5")))
                continue
            if status != 200:
                raise SmokeFailure(f"poll returned {status}: {describe(payload)}")
            if payload["status"] in ("done", "failed"):
                return payload
            time.sleep(2)
        raise SmokeFailure(f"job {job_id} did not finish within {timeout:.0f}s")


def multipart(fields: dict[str, str], image: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode()
            + b"\r\n"
        )
    mime = mimetypes.guess_type(image.name)[0] or "image/jpeg"
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
        f'filename="{image.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
        + image.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def describe(payload: dict[str, Any]) -> str:
    error = payload.get("error", {})
    return f"{error.get('code')}: {error.get('message')}" if error else json.dumps(payload)


def wait_until_ready(api: Api, timeout: float) -> str:
    status, payload, _ = api.request("GET", "/health", auth=False)
    if status != 200:
        raise SmokeFailure(f"/health returned {status}: {payload}")
    deadline = time.monotonic() + timeout
    last = None
    while True:
        status, payload, _ = api.request("GET", "/v1/model")
        if status != 200:
            raise SmokeFailure(f"/v1/model returned {status}: {describe(payload)}")
        state = payload["state"]
        if state == "ready":
            return str(payload.get("stage"))
        if state == "failed":
            raise SmokeFailure("the worker failed to load the model; check its logs")
        if state != last:
            print(f"  model is {state}; waiting (up to {timeout:.0f}s)...", flush=True)
            last = state
        if time.monotonic() > deadline:
            raise SmokeFailure(f"model still {state} after {timeout:.0f}s")
        time.sleep(5)


def run_case(
    api: Api, image: Path, case: Case, timeout: float, session_id: str | None = None
) -> bool:
    print(f"\n[{case.name}]  lang={case.lang} question_lang={case.question_lang}")
    print(f"  question:       {case.question}")
    started = time.monotonic()
    job_id = api.submit(image, case, session_id)
    job = api.wait_for_job(job_id, timeout)
    elapsed = time.monotonic() - started
    if job["status"] == "failed":
        print(f"  FAILED after {elapsed:.1f}s: {describe(job)}")
        return False
    result = job["result"]
    print(f"  answer:         {result['answer']}")
    print(f"  english answer: {result['english_answer']}")
    if case.question_lang != "en":
        print(f"  translated q:   {result['english_question']}")
    print(f"  time:           {elapsed:.1f}s (job {job_id})")
    ok = True
    if not result["answer"].strip() or not result["english_answer"].strip():
        print("  FAILED: empty answer")
        ok = False
    if result["language"] != case.lang:
        print(f"  FAILED: answer language is {result['language']}, expected {case.lang}")
        ok = False
    if case.lang != "en" and result["answer"].strip() == result["english_answer"].strip():
        print("  warning: the answer is identical to the English one; translation may have failed")
    return ok


def run_session(api: Api, image: Path, timeout: float) -> bool:
    print("\n[Session follow-up]")
    status, payload, _ = api.request("POST", "/v1/sessions")
    if status != 201:
        raise SmokeFailure(f"creating a session returned {status}: {describe(payload)}")
    session_id = payload["session_id"]
    try:
        ok = all(
            run_case(api, image, Case(f"turn {i}", q, "en", "en"), timeout, session_id)
            for i, q in enumerate(SESSION_QUESTIONS, start=1)
        )
        status, payload, _ = api.request("GET", f"/v1/sessions/{session_id}")
        turns = len(payload.get("history", [])) if status == 200 else None
        if turns != len(SESSION_QUESTIONS):
            print(f"  FAILED: session has {turns} turns, expected {len(SESSION_QUESTIONS)}")
            ok = False
        return ok
    finally:
        api.request("DELETE", f"/v1/sessions/{session_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", type=Path, required=True, help="JPEG, PNG or WebP under 4 MB")
    parser.add_argument("--url", default=os.environ.get("ATLAS_API_URL", "http://localhost:8000"))
    parser.add_argument("--key", default=os.environ.get("ATLAS_API_KEY"), help="or ATLAS_API_KEY")
    parser.add_argument(
        "--wait-ready", type=float, default=1800, help="seconds to wait for the model"
    )
    parser.add_argument("--job-timeout", type=float, default=600, help="seconds to wait per job")
    args = parser.parse_args()
    if not args.key:
        parser.error("set --key or ATLAS_API_KEY")
    if not args.image.is_file():
        parser.error(f"no such image: {args.image}")

    api = Api(args.url, args.key)
    started = time.monotonic()
    try:
        print(f"Checking {api.url} ...")
        stage = wait_until_ready(api, args.wait_ready)
        print(f"  model ready (stage {stage})")
        results = [run_case(api, args.image, case, args.job_timeout) for case in CASES]
        results.append(run_session(api, args.image, args.job_timeout))
    except SmokeFailure as exc:
        print(f"\nFAILED: {exc}")
        return 1

    passed = sum(results)
    print(f"\n{passed}/{len(results)} cases passed in {time.monotonic() - started:.1f}s")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
