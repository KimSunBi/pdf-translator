import json
import re

import yaml

from translator.extract import Segment
from translator.llm import UsageTracker
from translator.pipeline import run_pipeline
from translator.report import compute_stats, write_csv_report, write_html_report
from translator.outputs import write_docx, write_markdown

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)
CFG["pipeline"]["batch_size"] = 3


class ScriptedLLM:
    """task별로 응답을 스크립트로 정하는 테스트용 LLM."""

    def __init__(self, translate, retranslate=None, review=None, drop_in_batch=False):
        self.usage = UsageTracker()
        self._translate, self._retranslate, self._review = translate, retranslate, review
        self.drop_in_batch = drop_in_batch
        self.calls: list[str] = []

    def generate(self, role, prompt, system=None, images=None, json_mode=False, task=None):
        self.calls.append(task)
        self.usage.add(task, 100, 50)
        if task == "analyze":
            return json.dumps({"summary": "테스트 문서", "doc_type": "paper", "domain": "edu", "tone": "formal",
                               "key_terms": [{"src": "feedback", "tgt": "피드백"}]})
        payload = json.loads(re.search(r"<input>\s*(.*?)\s*</input>", prompt, re.S).group(1))
        if task == "translate":
            paras = payload["paragraphs"]
            if self.drop_in_batch and len(paras) > 1:
                paras = paras[:-1]          # 일부러 한 문단을 빼먹는다
            return json.dumps({"translations": [{"id": p["id"], "text": self._translate(p["text"])} for p in paras]})
        if task == "retranslate":
            p = payload["paragraph"]
            return json.dumps({"translations": [{"id": p["id"], "text": self._retranslate(p["text"], payload["problems_found"])}]})
        if task == "review":
            return json.dumps({"reviews": [self._review(p) for p in payload["paragraphs"]]})
        raise AssertionError(task)


def segs(*texts):
    return [Segment(i, "text", 1, t) for i, t in enumerate(texts)]


GOOD = {
    "The study enrolled 1,200 students.": "이 연구에는 학생 1,200명이 참여했습니다.",
    "Feedback helps learners improve.": "피드백은 학습자의 향상을 돕습니다.",
}


def ok_review(p):
    return {"id": p["id"], "ok": True, "issue": "", "fixed": ""}


def test_retranslate_fixes_violation():
    first = {"The study enrolled 1,200 students.": "이 연구에는 학생 1,500명이 참여했습니다."}
    llm = ScriptedLLM(translate=lambda s: first.get(s, GOOD[s]),
                      retranslate=lambda s, problems: GOOD[s], review=ok_review)
    res = run_pipeline(segs(*GOOD), llm, CFG, progress=lambda m: None)
    u = res.units[0]
    assert u.retries == 1 and not u.final and u.initial          # 1차 위반 → 재번역으로 해결
    assert u.tgt == GOOD["The study enrolled 1,200 students."]
    stats = compute_stats(res, CFG, llm.usage)
    assert stats["retried"] == 1 and stats["retried_resolved"] == 1 and stats["flagged"] == 0
    assert stats["score"] < 100            # 재번역 감점
    assert "피드백" in [t for _, t in res.auto_terms]


def test_unresolved_is_flagged_after_max_retries():
    llm = ScriptedLLM(translate=lambda s: "이 연구에는 학생 999명이 참여했습니다.",
                      retranslate=lambda s, p: "이 연구에는 학생 999명이 참여했습니다.", review=ok_review)
    res = run_pipeline(segs("The study enrolled 1,200 students."), llm, CFG, progress=lambda m: None)
    u = res.units[0]
    assert u.retries == CFG["pipeline"]["max_auto_retranslate"] and u.flagged
    assert llm.calls.count("retranslate") == 2
    assert compute_stats(res, CFG, llm.usage)["flagged"] == 1


def test_llm_review_accepts_clean_fix_and_rejects_bad_fix():
    def review(p):
        if "피드백" in p["tgt"]:
            return {"id": p["id"], "ok": False, "issue": "어색함", "fixed": "피드백은 학습자가 성장하도록 돕습니다."}
        return {"id": p["id"], "ok": False, "issue": "오역", "fixed": "이 연구에는 학생 1,500명이 참여했습니다."}  # 숫자 오류 수정안
    llm = ScriptedLLM(translate=lambda s: GOOD[s], retranslate=lambda s, p: GOOD[s], review=review)
    res = run_pipeline(segs(*GOOD), llm, CFG, progress=lambda m: None)
    a, b = res.units
    assert b.review_fixed and b.tgt.startswith("피드백은 학습자가 성장")
    assert a.review_unresolved and a.tgt == GOOD[a.src]        # 검사에 걸리는 수정안은 적용하지 않음
    stats = compute_stats(res, CFG, llm.usage)
    assert stats["review_fixed"] == 1 and stats["review_unresolved"] == 1 and stats["flagged"] == 1


def test_batch_with_missing_item_falls_back_to_single_calls():
    llm = ScriptedLLM(translate=lambda s: GOOD[s], retranslate=lambda s, p: GOOD[s], review=ok_review,
                      drop_in_batch=True)
    res = run_pipeline(segs(*GOOD), llm, CFG, progress=lambda m: None)
    assert all(u.tgt for u in res.units)       # 빠진 문단도 개별 호출로 채워짐


def test_keep_and_table_cells():
    seg_list = [
        Segment(0, "text", 1, "Feedback helps learners improve."),
        Segment(1, "keep", 1, "[1] Smith, J. (2020). Learning and teaching."),
        Segment(2, "table", 1, rows=[["Group", "Score"], ["Feedback", "84"]]),
    ]
    mapping = {"Feedback helps learners improve.": GOOD["Feedback helps learners improve."],
               "Group": "집단", "Score": "점수", "Feedback": "피드백"}
    llm = ScriptedLLM(translate=lambda s: mapping[s], retranslate=lambda s, p: mapping[s], review=ok_review)
    res = run_pipeline(seg_list, llm, CFG, progress=lambda m: None)
    out = res.rendered()
    assert out[1]["tgt"] == seg_list[1].text            # keep은 원문 그대로
    assert out[2]["tgt"] == [["집단", "점수"], ["피드백", "84"]]   # 숫자 셀은 그대로, 글자 셀만 번역
    assert not any(u.flagged for u in res.units)


def test_outputs_written(tmp_path):
    llm = ScriptedLLM(translate=lambda s: GOOD[s], retranslate=lambda s, p: GOOD[s], review=ok_review)
    res = run_pipeline(segs(*GOOD), llm, CFG, progress=lambda m: None)
    stats = compute_stats(res, CFG, llm.usage)
    write_markdown(res, str(tmp_path / "a.md"), "t")
    write_docx(res, str(tmp_path / "a.docx"), "t")
    write_html_report(res, stats, str(tmp_path / "r.html"), "t", {"direction": "x", "style": "formal", "model": "m", "elapsed": "1초"})
    write_csv_report(res, str(tmp_path / "r.csv"))
    for name in ("a.md", "a.docx", "r.html", "r.csv"):
        assert (tmp_path / name).stat().st_size > 100
    assert "품질 점수" in (tmp_path / "r.html").read_text(encoding="utf-8")
    assert stats["score"] == 100.0


def test_pipeline_aborts_when_every_call_fails():
    class AlwaysFail:
        def __init__(self):
            self.usage = UsageTracker()
            self.calls = 0

        def generate(self, role, prompt, system=None, images=None, json_mode=False, task=None):
            self.calls += 1
            raise RuntimeError("401 invalid key")

    import pytest
    many = segs(*[f"Paragraph number {i} of the document." for i in range(30)])
    llm = AlwaysFail()
    with pytest.raises(RuntimeError, match="연속 실패해 중단합니다"):
        run_pipeline(many, llm, CFG, progress=lambda m: None)
    assert llm.calls < 30                             # 끝까지 헛돌지 않고 일찍 멈춘다
