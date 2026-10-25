"""진행 기록(문자열)을 읽어 단계별 진행률과 그래프용 숫자로 바꾼다. 화면(app.py)과 분리해 테스트하기 쉽게 했다."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

STAGES = ["PDF 읽기", "문서 분석", "번역·검사", "LLM 검수", "마무리", "PDF 만들기"]
WEIGHTS = [5, 5, 60, 20, 5, 5]              # 전체 100 기준 (번역이 가장 오래 걸린다)

_DONE_RE = re.compile(r"^\s*(\d+)/(\d+) 번역·검사 완료")
_EXTRACT_RE = re.compile(r"추출 완료: 문단 (\d+), 표 (\d+)")
_MODEL_RE = re.compile(r"^\[(?!\d/4\])([^\]]+)\] (.*)$", re.S)


@dataclass
class Progress:
    stage: int = 0                       # 지금 하는 단계 (STAGES 인덱스), 끝나면 len(STAGES)
    stage_frac: float = 0.0              # 현재 단계 안에서의 진행 (0~1)
    done: int = 0                        # 번역·검사를 끝낸 단위 수
    total: int = 0
    points: list[tuple[float, int]] = field(default_factory=list)   # (경과 초, 끝낸 수) — 그래프용
    finished: bool = False
    with_pdf: bool = False

    @property
    def percent(self) -> float:
        n = len(STAGES) if self.with_pdf else len(STAGES) - 1
        w = WEIGHTS[:n]
        total = sum(w)
        done = sum(w[: min(self.stage, n)])
        if self.stage < n:
            done += w[self.stage] * self.stage_frac
        return 100.0 if self.finished else min(99.0, 100.0 * done / total)

    def eta_seconds(self, elapsed: float) -> float | None:
        """번역 단계의 속도로 남은 시간을 추정한다. 단계가 아직 번역에 들어가지 않았으면 None."""
        if self.stage != 2 or self.done < 2 or not self.total or elapsed <= 0:
            return None
        t0 = self.points[0][0] if self.points else 0.0
        spent = max(1e-6, elapsed - t0)
        return max(0.0, (self.total - self.done) * spent / self.done)


def parse(log: list[str], times: list[float] | None = None, with_pdf: bool = False) -> Progress:
    """한 모델(또는 단일 번역)의 진행 기록 → Progress."""
    p = Progress(with_pdf=with_pdf)
    times = times or [0.0] * len(log)
    for line, t in zip(log, times):
        if line.startswith("PDF 읽는 중"):
            p.stage, p.stage_frac = 0, 0.2
        elif (m := _EXTRACT_RE.search(line)):
            p.total = int(m.group(1))                     # 문단 수(표 셀은 [2/4] 줄에서 더한다)
            p.stage, p.stage_frac = 1, 0.0
        elif line.startswith("[1/4]"):
            p.stage, p.stage_frac = 1, 0.3
        elif line.startswith("[2/4]"):
            p.stage, p.stage_frac = 2, 0.0
            nums = [int(x) for x in re.findall(r"(\d+)개", line)]
            if nums:
                p.total = sum(nums)
            p.points.append((t, 0))
        elif (m := _DONE_RE.match(line)):
            p.done, p.total = int(m.group(1)), int(m.group(2))
            p.stage, p.stage_frac = 2, p.done / max(1, p.total)
            p.points.append((t, p.done))
        elif line.startswith("[3/4]"):
            p.stage, p.stage_frac = 3, 0.3
        elif line.startswith("[4/4]"):
            p.stage, p.stage_frac = 4, 0.3
        elif line.startswith("원본 모양 PDF"):
            p.stage, p.stage_frac, p.finished = 5, 1.0, True
    return p


def split_by_model(log: list[str], times: list[float]) -> dict[str, tuple[list[str], list[float]]]:
    """모델 비교 기록을 모델별로 나눈다. 접두어가 없는 줄(PDF 읽기 등)은 '' 항목에 모은다."""
    out: dict[str, tuple[list[str], list[float]]] = {}
    for line, t in zip(log, times):
        m = _MODEL_RE.match(line)
        key, text = (m.group(1), m.group(2)) if m else ("", line)
        lines, ts = out.setdefault(key, ([], []))
        lines.append(text)
        ts.append(t)
    return out


def fmt_eta(sec: float | None) -> str:
    if sec is None:
        return "계산 중"
    m, s = divmod(int(sec), 60)
    return f"약 {m}분 {s}초" if m else f"약 {s}초"
