import os

import pymupdf
import yaml

from tests.test_extract import make_pdf
from tests.test_pipeline import ScriptedLLM
from translator.extract import extract_pdf
from translator.layout import build_layout_pdf, find_font, split_by_weights
from translator.pipeline import run_pipeline
from translator.sourcemd import apply_md, segments_to_md

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)
CFG["pipeline"]["use_vision_for_complex_pages"] = False

KO = {
    "Learning Outcomes in Higher Education": "고등교육의 학습 성과",
    "The study enrolled 1,200 students across ten universities. Results were collected at the end of each semester.":
        "이 연구에는 열 개 대학의 학생 1,200명이 참여했으며, 결과는 매 학기 말에 수집되었습니다.",
    "Overall satisfaction was reported at 84 percent, which was lower than the authors had expected before the study began.":
        "전체 만족도는 84퍼센트로 보고되었으며, 이는 연구를 시작하기 전에 저자들이 기대했던 것보다 낮은 수치였습니다.",
}


def _ok(p):
    return {"id": p["id"], "ok": True, "issue": "", "fixed": ""}


def _translated(tmp_path):
    pdf = str(tmp_path / "s.pdf")
    make_pdf(pdf)
    segs, _ = extract_pdf(pdf, CFG, with_layout=True, progress=lambda m: None)
    llm = ScriptedLLM(translate=lambda s: KO.get(s, s), retranslate=lambda s, p: KO.get(s, s), review=_ok)
    return pdf, segs, run_pipeline(segs, llm, CFG, progress=lambda m: None)


def test_extract_keeps_positions_and_merges_boxes_across_pages(tmp_path):
    _, segs, _ = _translated(tmp_path)
    texts = [s for s in segs if s.kind == "text"]
    assert all(s.boxes for s in texts)
    merged = next(s for s in texts if s.text.startswith("Overall satisfaction"))
    assert [b[0] for b in merged.boxes] == [0, 1]            # 1쪽과 2쪽에 걸친 문단은 상자 2개
    x0, y0, x1, y1, size = merged.boxes[0][1:]
    assert x1 > x0 and y1 > y0 and 8 < size < 14


def test_split_by_weights_keeps_all_words():
    text = "하나 둘 셋 넷 다섯 여섯 일곱 여덟"
    parts = split_by_weights(text, [3, 1])
    assert " ".join(parts) == text and len(parts[0].split()) > len(parts[1].split())
    assert split_by_weights("a b", [1]) == ["a b"]


def test_layout_pdf_replaces_original_text_with_korean(tmp_path):
    pdf, _, res = _translated(tmp_path)
    out = str(tmp_path / "out.pdf")
    rep = build_layout_pdf(pdf, res.rendered(), out)
    assert rep.placed >= 3 and not rep.overflow
    doc = pymupdf.open(out)
    page1 = doc[0].get_text()
    assert "고등교육의 학습 성과" in page1 and "열 개 대학" in page1
    assert "Learning Outcomes" not in page1                  # 원문은 지워졌다
    assert "Journal of Tests" in page1                       # 번역하지 않은 머리글은 그대로
    assert "[1] Smith" in doc[1].get_text()                  # 원문 유지(keep) 부분도 그대로
    assert os.path.getsize(out) < 2_000_000                  # 글꼴 전체가 아니라 쓴 글자만 들어간다


def test_font_prefers_hamchorom_when_present():
    path, label = find_font()
    assert path and os.path.exists(path)
    if os.path.exists("fonts/HCRBatang.ttf"):
        assert label == "함초롬바탕"


def test_source_md_roundtrip_and_edit(tmp_path):
    _, segs, _ = _translated(tmp_path)
    md = segments_to_md(segs, "s")
    assert "<!-- p:0 page:1 -->" in md and "<!-- k:" in md
    edited = md.replace("Learning Outcomes in Higher Education", "Learning Outcomes in Higher Ed.")
    changed, warns = apply_md(segs, edited)
    assert changed == 1 and not warns and segs[0].text == "Learning Outcomes in Higher Ed."
    assert apply_md(segs, edited) == (0, [])                  # 같은 수정본을 또 적용해도 변화 없음
    _, warns = apply_md(segs, "<!-- p:999 -->\nx")
    assert warns


def test_source_md_table_shape_is_protected():
    from translator.extract import Segment
    seg = Segment(0, "table", 1, rows=[["A", "B"], ["1", "2"]])
    md = segments_to_md([seg])
    assert "| A | B |" in md
    ok, w = apply_md([seg], md.replace("| A | B |", "| Aa | B |"))
    assert ok == 1 and seg.rows[0] == ["Aa", "B"]
    bad, w = apply_md([seg], "<!-- t:0 -->\n| only |\n")
    assert bad == 0 and w                                     # 줄·칸 개수가 달라지면 반영하지 않음
