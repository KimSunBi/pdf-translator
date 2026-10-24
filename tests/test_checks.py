from translator.checks import CheckContext, run_checks, glossary_stats, batch_count_ok


def ctx(direction="en2ko", glossary=None, style="formal", cfg=None):
    return CheckContext(direction=direction, glossary=glossary or [], style=style, cfg=cfg or {})


def names(vs):
    return {v.check for v in vs}


# ---- 1. 숫자
def test_numbers_ok():
    src = "The study enrolled 1,200 students in 2024."
    tgt = "이 연구에는 2024년에 학생 1200명이 참여했습니다."
    assert "numbers" not in names(run_checks(src, tgt, ctx()))


def test_numbers_missing_and_extra():
    src = "The study enrolled 1,200 students in 2024."
    tgt = "이 연구에는 학생 1500명이 참여했습니다."
    vs = [v for v in run_checks(src, tgt, ctx()) if v.check == "numbers"]
    assert len(vs) == 2  # 누락 + 추가


def test_numbers_scale_words_skipped():
    src = "Revenue reached 3 million dollars."
    tgt = "매출은 300만 달러에 달했습니다."
    assert "numbers" not in names(run_checks(src, tgt, ctx()))


# ---- 2. 보존 요소
def test_preserve_url_and_citation():
    src = "See https://example.com/a for details [12]."
    bad = "자세한 내용은 예시 사이트를 참고하십시오."
    good = "자세한 내용은 https://example.com/a 를 참고하십시오 [12]."
    assert "preserve" in names(run_checks(src, bad, ctx()))
    assert "preserve" not in names(run_checks(src, good, ctx()))


# ---- 3. 용어집
def test_glossary():
    g = [("career education", "진로교육")]
    src = "Career education supports learners."
    assert "glossary" in names(run_checks(src, "경력 교육은 학습자를 돕습니다.", ctx(glossary=g)))
    tgt = "진로교육은 학습자를 돕습니다."
    assert "glossary" not in names(run_checks(src, tgt, ctx(glossary=g)))
    assert glossary_stats(src, tgt, ctx(glossary=g))[:2] == (1, 1)


# ---- 4. 문단
def test_empty_translation():
    assert "paragraph_count" in names(run_checks("Hello world, this is text.", "  ", ctx()))
    assert batch_count_ok(3, 3) and not batch_count_ok(3, 2)


# ---- 5. 길이 비율
def test_length_ratio():
    src = "This is a fairly long sentence about learning outcomes in higher education."
    assert "length_ratio" in names(run_checks(src, "학습 성과.", ctx()))
    assert "length_ratio" in names(run_checks(src, "가" * 400, ctx()))
    ok = "이것은 고등교육의 학습 성과에 관한 다소 긴 문장입니다."
    assert "length_ratio" not in names(run_checks(src, ok, ctx()))


def test_length_ratio_short_text_skipped():
    assert "length_ratio" not in names(run_checks("Yes.", "예.", ctx()))


# ---- 6. 미번역
def test_untranslated_english_run():
    src = "The results indicate that students who received feedback performed better overall."
    tgt = "결과: The results indicate that students who received feedback performed better overall."
    assert "untranslated" in names(run_checks(src, tgt, ctx()))


def test_untranslated_hangul_in_ko2en():
    src = "학생들은 피드백을 받은 후 더 나은 성과를 보였습니다."
    tgt = "Students showed better results after 피드백을 받은 후."
    assert "untranslated" in names(run_checks(src, tgt, ctx(direction="ko2en")))


# ---- 7. 문체
def test_style_formal_flags_plain():
    src = "Students improved. They learned quickly."
    mixed = "학생들의 성적이 향상되었습니다. 그들은 빠르게 배웠다."
    assert "style" in names(run_checks(src, mixed, ctx(style="formal")))
    clean = "학생들의 성적이 향상되었습니다. 그들은 빠르게 배웠습니다."
    assert "style" not in names(run_checks(src, clean, ctx(style="formal")))


def test_style_plain_flags_formal():
    src = "Students improved."
    assert "style" in names(run_checks(src, "학생들의 성적이 향상되었습니다.", ctx(style="plain")))
    assert "style" not in names(run_checks(src, "학생들의 성적이 향상되었다.", ctx(style="plain")))


# ---- 8. 괄호
def test_brackets():
    src = "The method (see Table 2) works well."
    assert "brackets" in names(run_checks(src, "이 방법(표 2 참조은 잘 작동합니다.", ctx()))
    assert "brackets" not in names(run_checks(src, "이 방법(표 2 참조)은 잘 작동합니다.", ctx()))


def test_brackets_unbalanced_source_not_flagged():
    assert "brackets" not in names(run_checks("1) First item is here today", "1) 첫 번째 항목입니다", ctx()))


# ---- 설정으로 끄기
def test_disable_check():
    c = ctx(cfg={"style": {"enabled": False}})
    src = "They learned quickly."
    assert "style" not in names(run_checks(src, "그들은 빠르게 배웠다.", c))
