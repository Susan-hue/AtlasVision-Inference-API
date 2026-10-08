from __future__ import annotations

import pytest

from app.core.heartbeat import ModelState, accepts_jobs, model_state
from app.core.schemas import Heartbeat, WorkerState

NOW = 1_000_000.0


def beat(state: WorkerState, age: float = 0.0, worker: str = "w1") -> Heartbeat:
    return Heartbeat(worker_id=worker, state=state, updated_at=NOW - age)


@pytest.mark.parametrize(
    ("beats", "expected"),
    [
        ([], ModelState.UNAVAILABLE),
        ([beat(WorkerState.READY, age=31)], ModelState.UNAVAILABLE),
        ([beat(WorkerState.LOADING)], ModelState.LOADING),
        ([beat(WorkerState.FAILED)], ModelState.FAILED),
        ([beat(WorkerState.FAILED), beat(WorkerState.READY, worker="w2")], ModelState.READY),
        ([beat(WorkerState.FAILED), beat(WorkerState.LOADING, worker="w2")], ModelState.LOADING),
    ],
)
def test_model_state(beats: list[Heartbeat], expected: ModelState) -> None:
    assert model_state(beats, now=NOW, stale_after=30) is expected


def test_only_ready_or_loading_accept_jobs() -> None:
    assert {s for s in ModelState if accepts_jobs(s)} == {ModelState.READY, ModelState.LOADING}
