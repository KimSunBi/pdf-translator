"""백그라운드 작업 관리.

Streamlit은 화면을 건드리거나 연결이 끊기면 실행 중이던 스크립트를 중단한다. 번역 같은 긴 작업을 그 안에서
직접 돌리면 같이 취소되므로, 작업은 별도 스레드에서 돌리고 화면은 진행 상황만 읽어 간다.
(서버 프로세스가 다시 시작되면 작업도 사라진다.)
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .runner import RunnerError

MAX_LOG_LINES = 3000
KEEP_FINISHED_SECONDS = 3 * 3600
MAX_JOBS = 30


@dataclass
class Job:
    id: str
    kind: str
    created: float = field(default_factory=time.time)
    status: str = "running"          # running | done | error
    log: list[str] = field(default_factory=list)
    times: list[float] = field(default_factory=list)      # log와 같은 순서의 '시작 후 경과 초'
    result: Any = None
    error: str = ""
    finished: float | None = None
    meta: dict = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.created


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def _cleanup_locked() -> None:
    now = time.time()
    for jid in [j.id for j in _JOBS.values() if j.finished and now - j.finished > KEEP_FINISHED_SECONDS]:
        del _JOBS[jid]
    if len(_JOBS) > MAX_JOBS:
        finished = sorted((j for j in _JOBS.values() if j.finished), key=lambda j: j.finished)
        for j in finished[: len(_JOBS) - MAX_JOBS]:
            del _JOBS[j.id]


def start_job(kind: str, work: Callable[[Callable[[str], None]], Any], meta: dict | None = None) -> Job:
    """work(progress) -> 결과. 스레드에서 실행하고 Job을 바로 돌려준다."""
    job = Job(id=uuid.uuid4().hex, kind=kind, meta=dict(meta or {}))

    def progress(msg: str) -> None:
        with _LOCK:
            job.log.append(str(msg))
            job.times.append(time.time() - job.created)
            if len(job.log) > MAX_LOG_LINES:
                del job.log[: len(job.log) - MAX_LOG_LINES]
                del job.times[: len(job.times) - MAX_LOG_LINES]

    def run() -> None:
        try:
            result = work(progress)
            with _LOCK:
                job.result, job.status = result, "done"
        except RunnerError as exc:
            with _LOCK:
                job.error, job.status = str(exc), "error"
        except Exception as exc:     # 예상 못 한 오류도 화면에 보여준다 (키 값은 출력하지 않음)
            with _LOCK:
                job.error, job.status = f"예상하지 못한 오류: {exc.__class__.__name__}: {exc}", "error"
        finally:
            with _LOCK:
                job.finished = time.time()

    with _LOCK:
        _cleanup_locked()
        _JOBS[job.id] = job
    threading.Thread(target=run, name=f"job-{kind}-{job.id[:6]}", daemon=True).start()
    return job


def get_job(job_id: str | None) -> Job | None:
    if not job_id:
        return None
    with _LOCK:
        return _JOBS.get(str(job_id))


def log_tail(job: Job, n: int = 12) -> list[str]:
    with _LOCK:
        return list(job.log[-n:])


def snapshot(job: Job) -> tuple[list[str], list[float], float]:
    """(진행 기록, 각 줄의 경과 초, 지금까지 걸린 초) — 화면에서 그래프를 그릴 때 쓴다."""
    with _LOCK:
        return list(job.log), list(job.times), job.elapsed
