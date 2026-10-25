import threading
import time

import pytest

from translator import jobs
from translator.runner import RunnerError


def wait_done(job, timeout=5):
    end = time.time() + timeout
    while job.status == "running" and time.time() < end:
        time.sleep(0.01)
    return job


def test_job_runs_in_background_and_reports_progress():
    gate = threading.Event()

    def work(progress):
        progress("시작")
        gate.wait(5)
        progress("끝")
        return {"ok": 1}

    job = jobs.start_job("t", work)
    assert job.status == "running"                    # start_job은 기다리지 않고 바로 돌아온다
    assert jobs.get_job(job.id) is job                # 다른 화면(세션)에서도 id로 찾을 수 있다
    time.sleep(0.05)
    assert jobs.log_tail(job) == ["시작"]
    gate.set()
    wait_done(job)
    assert job.status == "done" and job.result == {"ok": 1} and job.finished
    assert jobs.log_tail(job) == ["시작", "끝"]


def test_job_errors_are_captured_not_raised():
    def runner_err(progress):
        raise RunnerError("사용자에게 보여줄 오류")

    def odd_err(progress):
        raise ValueError("boom")

    a = wait_done(jobs.start_job("t", runner_err))
    b = wait_done(jobs.start_job("t", odd_err))
    assert a.status == "error" and a.error == "사용자에게 보여줄 오류"
    assert b.status == "error" and "ValueError" in b.error and "boom" in b.error


def test_unknown_job_and_cleanup():
    assert jobs.get_job(None) is None and jobs.get_job("nope") is None
    old = wait_done(jobs.start_job("t", lambda p: 1))
    old.finished -= jobs.KEEP_FINISHED_SECONDS + 10
    jobs.start_job("t", lambda p: 2)                  # 새 작업을 시작하면서 오래된 완료 작업을 치운다
    assert jobs.get_job(old.id) is None


def test_log_is_capped():
    def work(progress):
        for i in range(jobs.MAX_LOG_LINES + 50):
            progress(str(i))

    job = wait_done(jobs.start_job("t", work))
    assert len(job.log) == jobs.MAX_LOG_LINES and job.log[-1] == str(jobs.MAX_LOG_LINES + 49)


def test_progress_parsing_stages_percent_and_eta():
    from translator.progress import fmt_eta, parse, split_by_model
    log = ["PDF 읽는 중", "추출 완료: 문단 8, 표 1, 원문 유지 2", "[1/4] 문서 분석 중 (요약·분야·핵심 용어)",
           "[2/4] 번역 중 (문단 8개, 표 셀 4개)", "      6/12 번역·검사 완료 (현재 1쪽)"]
    times = [0, 2, 3, 8, 28]
    p = parse(log, times, with_pdf=True)
    assert (p.stage, p.done, p.total) == (2, 6, 12)
    assert 10 < p.percent < 60 and not p.finished
    assert round(p.eta_seconds(30)) == round((12 - 6) * (30 - 8) / 6)         # 번역 시작 이후의 속도로 추정
    assert fmt_eta(None) == "계산 중" and fmt_eta(75) == "약 1분 15초"
    done = parse(log + ["[3/4] LLM 검수 중 (12개)", "[4/4] 마무리", "원본 모양 PDF: 9곳 배치"], times + [40, 45, 50], with_pdf=True)
    assert done.finished and done.percent == 100.0


def test_progress_split_by_model():
    from translator.progress import split_by_model
    log = ["PDF 읽는 중", "===== a (m) =====", "[a] [1/4] 문서 분석 중", "[b] [2/4] 번역 중 (문단 3개, 표 셀 0개)"]
    groups = split_by_model(log, [0, 1, 2, 3])
    assert set(groups) == {"", "a", "b"} and groups["a"][0] == ["[1/4] 문서 분석 중"]
