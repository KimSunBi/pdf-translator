import pymupdf

from translator.extract import extract_pdf, parse_pages

CFG = {
    "pipeline": {"use_vision_for_complex_pages": False},
    "extract": {"skip_references": True, "remove_headers_footers": True,
                "header_footer_margin": 0.07, "min_paragraph_chars": 2},
}


def _box(page, x0, y0, x1, y1, text, size=11):
    rc = page.insert_textbox(pymupdf.Rect(x0, y0, x1, y1), text, fontsize=size, fontname="helv")
    assert rc >= 0, "테스트 PDF 텍스트가 상자에 안 들어감"


def make_pdf(path):
    doc = pymupdf.open()
    # 1쪽: 머리글, 제목, 문단 2개(마지막은 다음 쪽으로 이어짐), 쪽번호
    p1 = doc.new_page()
    _box(p1, 72, 20, 520, 40, "Journal of Tests 2026", 9)
    _box(p1, 72, 100, 520, 130, "Learning Outcomes in Higher Education", 16)
    _box(p1, 72, 160, 520, 230,
         "The study enrolled 1,200 students across ten universities. Results were "
         "collected at the end of each semester.")
    _box(p1, 72, 260, 520, 330,
         "Overall satisfaction was reported at 84 percent, which was")
    _box(p1, 290, 800, 310, 820, "1", 9)
    # 2쪽: 이어지는 문장, 참고문헌
    p2 = doc.new_page()
    _box(p2, 72, 20, 520, 40, "Journal of Tests 2026", 9)
    _box(p2, 72, 100, 520, 160, "lower than the authors had expected before the study began.")
    _box(p2, 72, 200, 520, 225, "References", 14)
    _box(p2, 72, 240, 520, 300, "[1] Smith, J. (2020). Learning and teaching. Journal of Education, 12(3), 1-20.")
    _box(p2, 290, 800, 310, 820, "2", 9)
    # 3쪽: 2단 편집
    p3 = doc.new_page()
    _box(p3, 50, 100, 285, 170, "LEFT ONE first paragraph of the left column goes here.")
    _box(p3, 50, 200, 285, 270, "LEFT TWO second paragraph of the left column goes here.")
    _box(p3, 310, 100, 545, 170, "RIGHT ONE first paragraph of the right column goes here.")
    _box(p3, 310, 200, 545, 270, "RIGHT TWO second paragraph of the right column goes here.")
    doc.save(path)


def test_parse_pages():
    assert parse_pages("1-3,5", 10) == [0, 1, 2, 4]
    assert parse_pages(None, 3) == [0, 1, 2]
    assert parse_pages("9-12", 10) == [8, 9]


def test_extract_structure(tmp_path):
    pdf = tmp_path / "t.pdf"
    make_pdf(str(pdf))
    segs, warnings = extract_pdf(str(pdf), CFG, llm=None, progress=lambda m: None)
    texts = [s.text for s in segs]
    joined = "\n".join(texts)

    # 머리글·쪽번호 제거
    assert "Journal of Tests" not in joined
    assert not any(t.strip() in ("1", "2") for t in texts)
    # 쪽을 넘어 이어지는 문단 병합
    merged = [t for t in texts if t.startswith("Overall satisfaction")]
    assert merged and "lower than the authors had expected" in merged[0]
    # 참고문헌 이하는 keep
    ref = [s for s in segs if s.text.startswith("[1] Smith")]
    assert ref and ref[0].kind == "keep"
    assert [s for s in segs if s.text == "References"][0].kind == "text"


def test_two_column_reading_order(tmp_path):
    pdf = tmp_path / "t.pdf"
    make_pdf(str(pdf))
    segs, _ = extract_pdf(str(pdf), CFG, llm=None, pages="3", progress=lambda m: None)
    order = [s.text.split()[0] + " " + s.text.split()[1] for s in segs]
    assert order == ["LEFT ONE", "LEFT TWO", "RIGHT ONE", "RIGHT TWO"]


def test_scanned_page_warning(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    pdf = tmp_path / "blank.pdf"
    doc.save(str(pdf))
    segs, warnings = extract_pdf(str(pdf), CFG, llm=None, progress=lambda m: None)
    assert segs == [] and warnings and "OCR" in warnings[0]
