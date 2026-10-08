from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Lang = Literal["en", "ig", "yo", "ha"]


class Owner(BaseModel):
    """Who created a job or session: an API key (by id, never the key) and its visitor."""

    model_config = ConfigDict(frozen=True)

    key_id: str
    client_id: str | None = None


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ChatParams(BaseModel):
    question: str
    lang: Lang
    question_lang: Lang
    max_new_tokens: int


class ChatOutput(BaseModel):
    answer: str
    english_answer: str
    english_question: str
    language: Lang


class JobError(BaseModel):
    code: str
    message: str


class JobRecord(BaseModel):
    job_id: str
    kind: Literal["chat"] = "chat"
    status: JobStatus
    owner: Owner | None = None
    session_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    params: ChatParams
    result: ChatOutput | None = None
    error: JobError | None = None


class TurnRecord(BaseModel):
    question: str
    question_lang: Lang
    lang: Lang
    answer: str


class TurnView(TurnRecord):
    english_question: str
    english_answer: str


class WorkerState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


class Heartbeat(BaseModel):
    worker_id: str
    state: WorkerState
    updated_at: float
    model: dict[str, Any] | None = None
    error_code: str | None = None
