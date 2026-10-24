"""4단계 번역 파이프라인.

1. 분석   : 문서 전체를 훑어 요약·분야·문체·핵심 용어를 뽑는다
2. 번역   : 용어집 + 앞 문맥을 붙여 문단을 배치 단위로 번역한다
3. 검수   : 코드 검사 8가지 → 위반 시 자동 재번역(최대 N회) → 통과 문단 전부 LLM 검수
4. 통일   : 분석 단계의 핵심 용어를 모든 번역 호출에 같이 넘겨 용어를 통일하고,
           문체는 검사·재번역으로 통일한다
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Callable

from . import prompts
from .checks import (CHECK_NAMES, CheckContext, Violation, enabled_checks,
                     glossary_stats, run_checks, _term_in)
from .extract import Segment
from .llm import parse_json

_LETTER = re.compile(r"[A-Za-z가-힣]")


@dataclass
class Unit:
    uid: int
    seg_id: int
    page: int
    kind: str                       # text | cell
    src: str
    tgt: str = ""
    cell: tuple[int, int] | None = None
    initial: list[Violation] = field(default_factory=list)
    final: list[Violation] = field(default_factory=list)
    retries: int = 0
    review_issue: str = ""
    review_fixed: bool = False
    review_unresolved: bool = False
    gl_applicable: int = 0
    gl_applied: int = 0
    copied: bool = False            # 글자가 없는 표 셀 등 번역 없이 원문 유지

    @property
    def flagged(self) -> bool:
        return bool(self.final) or self.review_unresolved


@dataclass
class PipelineResult:
    segments: list[Segment]
    units: list[Unit]
    analysis: dict
    user_glossary: list[tuple[str, str]]
    auto_terms: list[tuple[str, str]]
    enabled: list[str]
    warnings: list[str] = field(default_factory=list)

    def units_of(self, seg_id: int) -> list[Unit]:
        return [u for u in self.units if u.seg_id == seg_id]

    def rendered(self) -> list[dict]:
        """출력용: 세그먼트별 원문/번역문/표시 여부."""
        by_seg: dict[int, list[Unit]] = {}
        for u in self.units:
            by_seg.setdefault(u.seg_id, []).append(u)
        out = []
        for seg in self.segments:
            us = by_seg.get(seg.id, [])
            flagged = any(u.flagged for u in us)
            notes = [str(v) for u in us for v in u.final]
            notes += [f"[LLM 검수] {u.review_issue}" for u in us if u.review_unresolved and u.review_issue]
            if seg.kind == "table":
                rows_tgt = [list(r) for r in (seg.rows or [])]
                for u in us:
                    r, c = u.cell
                    rows_tgt[r][c] = u.tgt
                out.append({"seg": seg, "kind": "table", "src": seg.rows, "tgt": rows_tgt,
                            "flagged": flagged, "notes": notes, "page": seg.page})
            elif seg.kind == "keep":
                out.append({"seg": seg, "kind": "keep", "src": seg.text, "tgt": seg.text,
                            "flagged": False, "notes": [], "page": seg.page})
            else:
                tgt = us[0].tgt if us else ""
                out.append({"seg": seg, "kind": "text", "src": seg.text, "tgt": tgt,
                            "flagged": flagged, "notes": notes, "page": seg.page})
        return out


# ------------------------------------------------------------------ 도우미

def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _gloss_hits(text: str, pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"src": s, "tgt": t} for s, t in pairs if s and _term_in(s, text)]


def _short(text: str, n: int = 400) -> str:
    return text if len(text) <= n else text[:n] + "…"


def _analysis_sample(segments: list[Segment]) -> str:
    texts = [s.text for s in segments if s.kind == "text"]
    joined = "\n\n".join(texts)
    if len(joined) <= 18000:
        return joined
    head = joined[:12000]
    n = len(joined)
    mids = [joined[int(n * q): int(n * q) + 2500] for q in (0.3, 0.55, 0.8)]
    return head + "\n\n[...]\n\n" + "\n\n[...]\n\n".join(mids)


def _parse_translations(raw: str) -> dict[int, str]:
    data = parse_json(raw)
    items = data.get("translations", data) if isinstance(data, dict) else data
    return {int(it["id"]): str(it["text"]) for it in items}


# ------------------------------------------------------------------ 본체

def run_pipeline(
    segments: list[Segment],
    llm,
    cfg: dict,
    direction: str = "en2ko",
    domain: str = "",
    style: str = "formal",
    user_glossary: list[tuple[str, str]] | None = None,
    progress: Callable[[str], None] = print,
) -> PipelineResult:
    pcfg = cfg.get("pipeline", {})
    user_glossary = user_glossary or []
    batch_size = int(pcfg.get("batch_size", 6))
    cell_batch = max(batch_size, 25)
    ctx_n = int(pcfg.get("context_paragraphs", 2))
    max_retry = int(pcfg.get("max_auto_retranslate", 2))
    do_review = bool(pcfg.get("llm_review", True))
    chunk_pages = int(pcfg.get("chunk_pages", 50))

    checks_cfg = cfg.get("checks", {})
    text_ctx = CheckContext(direction, user_glossary, style, checks_cfg)
    cell_cfg = copy.deepcopy(checks_cfg)
    for name in ("style", "length_ratio", "brackets"):
        cell_cfg.setdefault(name, {})["enabled"] = False
    cell_ctx = CheckContext(direction, user_glossary, style, cell_cfg)

    # ---- 번역 단위 만들기
    units: list[Unit] = []
    for seg in segments:
        if seg.kind == "text":
            units.append(Unit(len(units), seg.id, seg.page, "text", seg.text))
        elif seg.kind == "table":
            for r, row in enumerate(seg.rows or []):
                for c, cell in enumerate(row):
                    if _LETTER.search(cell or ""):
                        units.append(Unit(len(units), seg.id, seg.page, "cell", cell, cell=(r, c)))
    # 글자가 없는 표 셀(숫자·기호)은 번역 없이 원문 유지
    for seg in segments:
        if seg.kind == "table":
            have = {u.cell for u in units if u.seg_id == seg.id}
            for r, row in enumerate(seg.rows or []):
                for c, cell in enumerate(row):
                    if (r, c) not in have:
                        units.append(Unit(len(units), seg.id, seg.page, "cell", cell or "", tgt=cell or "", cell=(r, c), copied=True))
    todo = [u for u in units if not u.tgt and u.src]
    by_uid = {u.uid: u for u in units}

    # ---- 1단계: 분석
    analysis: dict = {}
    auto_terms: list[tuple[str, str]] = []
    if any(s.kind == "text" for s in segments):
        progress("[1/4] 문서 분석 중 (요약·분야·핵심 용어)")
        system, prompt = prompts.analyze_prompt(direction, domain, _analysis_sample(segments), user_glossary)
        try:
            analysis = parse_json(llm.generate("analyze", prompt, system=system, json_mode=True, task="analyze"))
        except Exception as exc:
            analysis = {}
            progress(f"      분석 실패({exc.__class__.__name__}) → 분석 없이 진행")
        known = {s.lower() for s, _ in user_glossary}
        for t in analysis.get("key_terms", []) or []:
            s, g = str(t.get("src", "")).strip(), str(t.get("tgt", "")).strip()
            if s and g and s.lower() not in known:
                auto_terms.append((s, g))

    all_terms = user_glossary + auto_terms
    system_translate = prompts.translate_system(direction, domain, style, analysis)

    # ---- 2단계: 번역 (+ 3단계 앞부분: 코드 검수와 자동 재번역)
    progress(f"[2/4] 번역 중 (문단 {sum(1 for u in todo if u.kind == 'text')}개, 표 셀 {sum(1 for u in todo if u.kind == 'cell')}개)")
    done_text: list[Unit] = []
    groups: list[list[Unit]] = []
    # 문서 순서를 지키면서 같은 종류끼리 배치로 묶는다
    cur: list[Unit] = []
    for u in todo:
        limit = batch_size if u.kind == "text" else cell_batch
        if cur and (cur[0].kind != u.kind or len(cur) >= limit):
            groups.append(cur)
            cur = []
        cur.append(u)
    if cur:
        groups.append(cur)

    def context_for() -> list[dict]:
        return [{"src": _short(u.src), "tgt": _short(u.tgt)} for u in done_text[-ctx_n:]] if ctx_n else []

    def ctx_of(u: Unit) -> CheckContext:
        return text_ctx if u.kind == "text" else cell_ctx

    err_state: dict = {"last": None, "streak": 0}
    abort_after = int(pcfg.get("abort_after_failed_batches", 3))

    def translate_units(batch: list[Unit]) -> None:
        text = "\n".join(u.src for u in batch)
        payload = [{"id": u.uid, "text": u.src} for u in batch]
        prompt = prompts.translate_prompt(payload, context_for(), _gloss_hits(text, all_terms))
        got: dict[int, str] = {}
        try:
            got = _parse_translations(llm.generate("translate", prompt, system=system_translate,
                                                   json_mode=True, task="translate"))
        except Exception as exc:
            err_state["last"] = exc
            got = {}
        missing = [u for u in batch if u.uid not in got]
        if missing and len(batch) > 1:
            # 개수·id가 맞지 않으면 한 문단씩 다시 번역 (누락 방지)
            for u in missing:
                try:
                    p = prompts.translate_prompt([{"id": u.uid, "text": u.src}], context_for(),
                                                 _gloss_hits(u.src, all_terms))
                    got.update(_parse_translations(llm.generate("translate", p, system=system_translate,
                                                                json_mode=True, task="translate")))
                except Exception as exc:
                    err_state["last"] = exc
        for u in batch:
            u.tgt = got.get(u.uid, "")

    def check_and_retry(u: Unit) -> None:
        vs = run_checks(u.src, u.tgt, ctx_of(u))
        u.initial = list(vs)
        best_tgt, best_vs = u.tgt, vs
        tries = 0
        while best_vs and tries < max_retry:
            tries += 1
            problems = [v.message for v in best_vs]
            prompt = prompts.retry_prompt({"id": u.uid, "text": u.src}, best_tgt, problems,
                                          _gloss_hits(u.src, all_terms), context_for())
            try:
                new = _parse_translations(llm.generate("translate", prompt, system=system_translate,
                                                       json_mode=True, task="retranslate")).get(u.uid, "")
            except Exception:
                break
            new_vs = run_checks(u.src, new, ctx_of(u))
            if len(new_vs) <= len(best_vs) and new.strip():
                best_tgt, best_vs = new, new_vs
        u.tgt, u.final, u.retries = best_tgt, best_vs, tries

    n_done = 0
    last_chunk = -1
    for batch in groups:
        page = batch[0].page
        chunk = (page - 1) // chunk_pages if chunk_pages else 0
        if chunk != last_chunk:
            progress(f"      -- {chunk * chunk_pages + 1}쪽부터 {chunk_pages}쪽 구간 처리 --")
            last_chunk = chunk
        translate_units(batch)
        if all(not u.tgt.strip() for u in batch):
            err_state["streak"] += 1
            if abort_after and err_state["streak"] >= abort_after:
                raise RuntimeError(f"번역 호출이 {abort_after}번 연속 실패해 중단합니다: {err_state['last']}")
        else:
            err_state["streak"] = 0
        for u in batch:
            check_and_retry(u)
            if u.kind == "text":
                done_text.append(u)
        n_done += len(batch)
        progress(f"      {n_done}/{len(todo)} 번역·검사 완료 (현재 {page}쪽)")

    if todo and all(not u.tgt.strip() for u in todo):
        raise RuntimeError(f"모든 번역 호출이 실패했습니다: {err_state['last']}")

    # ---- 3단계 뒷부분: LLM 검수 (코드 검사를 통과한 문단을 포함해 전부)
    reviewable = [u for u in todo if u.tgt.strip()]
    if do_review and reviewable:
        progress(f"[3/4] LLM 검수 중 ({len(reviewable)}개)")
        review_system = prompts.review_system(direction, style)
        rgroups: list[list[Unit]] = []
        cur = []
        for u in reviewable:
            limit = batch_size if u.kind == "text" else cell_batch
            if cur and (cur[0].kind != u.kind or len(cur) >= limit):
                rgroups.append(cur)
                cur = []
            cur.append(u)
        if cur:
            rgroups.append(cur)
        for batch in rgroups:
            items = [{"id": u.uid, "src": u.src, "tgt": u.tgt} for u in batch]
            text = "\n".join(u.src for u in batch)
            prompt = prompts.review_prompt(items, _gloss_hits(text, all_terms))
            try:
                data = parse_json(llm.generate("review", prompt, system=review_system,
                                               json_mode=True, task="review"))
                reviews = data.get("reviews", data) if isinstance(data, dict) else data
            except Exception as exc:
                for u in batch:
                    u.review_issue = f"검수 호출 실패({exc.__class__.__name__})"
                continue
            for rv in reviews:
                u = by_uid.get(int(rv.get("id", -1)))
                if u is None or rv.get("ok", True):
                    continue
                issue, fixed = str(rv.get("issue", "")), str(rv.get("fixed", "")).strip()
                u.review_issue = issue
                if fixed and fixed != u.tgt:
                    vs = run_checks(u.src, fixed, ctx_of(u))
                    if not vs:
                        u.tgt, u.final, u.review_fixed = fixed, [], True
                        continue
                    u.review_issue += " (수정안이 코드 검사에 걸려 미적용)"
                u.review_unresolved = True
    else:
        progress("[3/4] LLM 검수 건너뜀")

    # ---- 4단계: 마무리 (용어집 적용 통계)
    progress("[4/4] 마무리 (용어·문체 최종 점검)")
    for u in units:
        if u.src and u.tgt:
            a, b, _ = glossary_stats(u.src, u.tgt, text_ctx)
            u.gl_applicable, u.gl_applied = a, b

    return PipelineResult(segments, units, analysis, user_glossary, auto_terms,
                          enabled_checks(text_ctx))
