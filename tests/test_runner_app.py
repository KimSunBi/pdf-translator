import os

import pytest
import yaml

from tests.test_extract import make_pdf
from translator.runner import (RunnerError, compare_documents, parse_glossary, ready_profiles,
                               translate_document)

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)
CFG["pipeline"]["use_vision_for_complex_pages"] = False


@pytest.fixture()
def pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    make_pdf(str(p))
    return str(p)


# ---------------------------------------------------------------- 용어집

def test_parse_glossary_both_directions_and_bom():
    text = "﻿en,ko\ncareer education,진로교육\n,빈칸\nleadership,리더십\n"
    assert parse_glossary(text, "en2ko") == [("career education", "진로교육"), ("leadership", "리더십")]
    assert parse_glossary(text, "ko2en")[0] == ("진로교육", "career education")
    assert parse_glossary("src,tgt\na,b\n", "en2ko") == [("a", "b")]
    assert parse_glossary("foo,bar\n1,2\n", "en2ko") == []


# ---------------------------------------------------------------- 단일 번역

def test_translate_document_dry_run_writes_all_files(pdf, tmp_path):
    msgs = []
    res = translate_document(pdf, CFG, dry_run=True, out_dir=str(tmp_path / "o"), progress=msgs.append)
    assert set(res.files) == {"docx", "md", "report", "csv", "json", "source_md"}
    assert all(os.path.getsize(p) > 50 for p in res.files.values())
    assert res.stats["total"] == 4 and any("번역" in m for m in msgs)


def test_translate_document_page_limit_and_range_errors(pdf, tmp_path):
    with pytest.raises(RunnerError, match="2쪽까지만"):
        translate_document(pdf, CFG, dry_run=True, out_dir=str(tmp_path / "o"), max_pages=2)
    with pytest.raises(RunnerError, match="해당하는 쪽이 없습니다"):
        translate_document(pdf, CFG, dry_run=True, pages="40-50", out_dir=str(tmp_path / "o"))


def test_translate_document_missing_key_is_friendly(pdf, tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RunnerError, match="GEMINI_API_KEY"):
        translate_document(pdf, CFG, profile="gemini-3.1-pro", out_dir=str(tmp_path / "o"))


def test_not_a_pdf_is_friendly(tmp_path):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(RunnerError, match="열 수 없습니다"):
        translate_document(str(bad), CFG, dry_run=True, out_dir=str(tmp_path / "o"))


# ---------------------------------------------------------------- 비교

def test_compare_documents_dry_run_with_deepl_alignment(pdf, tmp_path):
    deepl = "하나\n\n둘\n\n셋\n\n넷"        # 번역 단위 4개와 일치
    res = compare_documents(pdf, CFG, dry_run=True, out_dir=str(tmp_path / "c"), deepl_text=deepl,
                            progress=lambda m: None)
    assert len(res.runs) == 2 and not res.failed and res.deepl_note == ""
    html = open(res.compare_html, encoding="utf-8").read()
    assert "하나" in html and "넷" in html
    assert all(os.path.exists(f["docx"]) for f in res.run_files.values())


def test_compare_documents_deepl_count_mismatch_note(pdf, tmp_path):
    res = compare_documents(pdf, CFG, dry_run=True, out_dir=str(tmp_path / "c"), deepl_text="하나\n\n둘",
                            progress=lambda m: None)
    assert "짝짓지 못했습니다" in res.deepl_note
    assert "DeepL 전체 번역문" in open(res.compare_html, encoding="utf-8").read()


def test_compare_documents_without_ready_profiles(pdf, tmp_path, monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    names, skipped = ready_profiles(CFG)
    assert names == [] and set(skipped) == set(CFG["profiles"])
    with pytest.raises(RunnerError, match="비교할 수 있는 프로필이 없습니다"):
        compare_documents(pdf, CFG, out_dir=str(tmp_path / "c"), progress=lambda m: None)


def test_compare_one_model_failing_does_not_stop_others(pdf, tmp_path):
    class Boom:
        supports_vision = False
        name, model = "boom", "boom-model"

        def __init__(self):
            from translator.llm import UsageTracker
            self.usage = UsageTracker()

        def generate(self, *a, **k):
            raise RuntimeError("서버 오류")

    from translator.llm import DryRunLLM
    res = compare_documents(pdf, CFG, llms=[Boom(), DryRunLLM(CFG, name="ok")], dry_run=True,
                            out_dir=str(tmp_path / "c"), progress=lambda m: None)
    assert [r.name for r in res.runs] == ["ok"]          # 모든 호출이 실패한 모델은 빈 결과 대신 실패로 처리
    assert "boom" in res.failed and "모든 번역 호출이 실패" in res.failed["boom"]


# ---------------------------------------------------------------- 웹 앱 잠금 (Streamlit AppTest)

def _app(monkeypatch, password=None):
    from streamlit.testing.v1 import AppTest
    monkeypatch.delenv("ALLOW_NO_PASSWORD", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    at = AppTest.from_file(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app.py")), default_timeout=30)
    if password:
        at.secrets["APP_PASSWORD"] = password
    return at


def test_app_locked_without_password(monkeypatch):
    at = _app(monkeypatch).run()
    assert not at.exception
    assert any("APP_PASSWORD" in e.value for e in at.error)
    assert not at.tabs                         # 본문(탭)은 렌더링되지 않는다


def test_app_wrong_then_right_password(monkeypatch):
    at = _app(monkeypatch, "s3cret").run()
    assert at.text_input and not at.tabs
    at.text_input[0].set_value("wrong")
    at.button[0].click().run()
    assert any("맞지 않습니다" in e.value for e in at.error) and not at.tabs
    at.text_input[0].set_value("s3cret")
    at.button[0].click().run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["번역", "모델 비교"]


def test_app_locks_after_five_failures(monkeypatch):
    at = _app(monkeypatch, "s3cret").run()
    for _ in range(5):
        at.text_input[0].set_value("nope")
        at.button[0].click().run()
    at.run()
    assert any("여러 번 틀렸습니다" in e.value for e in at.error)


def test_secrets_never_committed():
    ignore = open(".gitignore", encoding="utf-8").read()
    assert ".streamlit/secrets.toml" in ignore and "output/" in ignore
    assert not os.path.exists(".streamlit/secrets.toml")
    assert "streamlit" in open("requirements.txt").read()
