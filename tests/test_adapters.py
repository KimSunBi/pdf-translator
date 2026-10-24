import json
from types import SimpleNamespace as NS

import pytest
import yaml

from translator.compare_report import ProfileRun, split_deepl_text, write_compare_html
from translator.llm import (AnthropicLLM, GeminiLLM, OpenAICompatLLM, make_llm, parse_json,
                            profile_ready, resolve_profile)
from translator.pipeline import run_pipeline
from translator.extract import Segment
from translator.report import compute_stats

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)
CFG["generation"]["retry_base_seconds"] = 0
CFG["generation"]["max_retries"] = 1


# ---------------------------------------------------------------- 가짜 SDK 클라이언트

class FakeAnthropic:
    def __init__(self, text="{\"ok\": true}", stop="end_turn"):
        self.calls = []
        self.messages = self
        self._text, self._stop = text, stop

    def create(self, **kw):
        self.calls.append(kw)
        return NS(content=[NS(type="text", text=self._text)], stop_reason=self._stop,
                  usage=NS(input_tokens=11, output_tokens=7))


class FakeOpenAI:
    def __init__(self, reject_response_format=False):
        self.calls = []
        self.reject = reject_response_format
        self.chat = NS(completions=self)

    def create(self, **kw):
        self.calls.append(kw)
        if self.reject and "response_format" in kw:
            raise RuntimeError("response_format not supported")
        return NS(choices=[NS(message=NS(content='{"ok": true}'))], usage=NS(prompt_tokens=5, completion_tokens=3))


class FakeGemini:
    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        return NS(text='{"ok": true}', usage_metadata=NS(prompt_token_count=9, candidates_token_count=4,
                                                         thoughts_token_count=2))


# ---------------------------------------------------------------- 테스트

def test_parse_json_tolerates_fences_and_chatter():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go:\n{"a": [1, 2]}\nHope it helps!') == {"a": [1, 2]}
    with pytest.raises(Exception):
        parse_json("no json here")


def test_gemini_adapter_uses_profile_model_and_counts_tokens():
    fake = FakeGemini()
    llm = GeminiLLM(CFG, "gemini-3.1-pro", client=fake)
    out = llm.generate("translate", "hi", system="sys", json_mode=True, task="translate")
    assert json.loads(out) == {"ok": True}
    assert fake.calls[0][0] == "gemini-3.1-pro-preview"
    assert (llm.usage.total_input, llm.usage.total_output) == (9, 6)      # 생각 토큰 포함


def test_anthropic_adapter_builds_request_with_image_and_json_hint():
    fake = FakeAnthropic()
    llm = AnthropicLLM(CFG, "claude-sonnet", client=fake)
    llm.generate("vision", "read this", system="sys", images=[b"png"], json_mode=True, task="vision")
    kw = fake.calls[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["system"] == "sys"
    blocks = kw["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert "JSON" in blocks[-1]["text"]
    assert (llm.usage.total_input, llm.usage.total_output) == (11, 7)


def test_anthropic_max_tokens_stop_is_an_error():
    llm = AnthropicLLM(CFG, "claude-sonnet", client=FakeAnthropic(stop="max_tokens"))
    with pytest.raises(RuntimeError, match="호출 실패"):
        llm.generate("translate", "x", task="translate")


def test_openai_compat_falls_back_when_server_rejects_json_format():
    fake = FakeOpenAI(reject_response_format=True)
    prof = {"provider": "openai_compatible", "model": "m", "base_url": "http://x", "api_key_env": None}
    llm = OpenAICompatLLM(CFG, "oss", prof, client=fake)
    out = llm.generate("translate", "hi", system="s", json_mode=True, task="translate")
    assert json.loads(out) == {"ok": True}
    assert "response_format" in fake.calls[0] and "response_format" not in fake.calls[1]   # 재시도 때 제외
    assert llm.supports_vision is False


def test_openai_compat_ignores_images_when_vision_disabled():
    fake = FakeOpenAI()
    prof = {"provider": "openai_compatible", "model": "m", "base_url": "http://x", "vision": False}
    OpenAICompatLLM(CFG, "oss", prof, client=fake).generate("vision", "p", images=[b"png"])
    assert isinstance(fake.calls[0]["messages"][-1]["content"], str)


def test_profile_ready_and_factory(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert profile_ready(CFG["profiles"]["gemini-3.1-pro"])[0]
    ok, why = profile_ready(CFG["profiles"]["claude-sonnet"])
    assert not ok and "ANTHROPIC_API_KEY" in why
    ok, why = profile_ready(CFG["profiles"]["open-source"])        # 자리표시자(<...>) 상태
    assert not ok and "설정되지 않음" in why
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        make_llm(CFG, "claude-sonnet")
    with pytest.raises(RuntimeError, match="없습니다"):
        resolve_profile(CFG, "nope")


def test_legacy_config_without_profiles_still_resolves():
    name, prof = resolve_profile({"models": {"translate": "gemini-x"}}, None)
    assert prof["provider"] == "gemini" and prof["model"] == "gemini-x"


# ---------------------------------------------------------------- 비교 페이지

class _Scripted:
    """프로필마다 다른 번역을 돌려주는 가짜 LLM."""
    supports_vision = False

    def __init__(self, name, suffix):
        from translator.llm import UsageTracker
        self.name, self.model, self.suffix, self.usage = name, name + "-model", suffix, UsageTracker()

    def generate(self, role, prompt, system=None, images=None, json_mode=False, task=None):
        import re
        self.usage.add(task or role, 10, 5)
        if task == "analyze":
            return json.dumps({"summary": "s", "key_terms": []})
        payload = json.loads(re.search(r"<input>\s*(.*?)\s*</input>", prompt, re.S).group(1))
        if task == "review":
            return json.dumps({"reviews": [{"id": p["id"], "ok": True} for p in payload["paragraphs"]]})
        paras = payload.get("paragraphs") or [payload["paragraph"]]
        return json.dumps({"translations": [{"id": p["id"], "text": "번역문 " + self.suffix + "입니다."} for p in paras]})


def test_compare_html_has_columns_votes_and_deepl(tmp_path):
    segs = [Segment(0, "text", 1, "First paragraph of the text here."),
            Segment(1, "keep", 1, "[1] Reference"), Segment(2, "text", 2, "Second paragraph of the text here.")]
    runs = []
    for name, suf in (("alpha", "ALPHA"), ("beta", "BETA")):
        llm = _Scripted(name, suf)
        res = run_pipeline(segs, llm, CFG, progress=lambda m: None)
        runs.append(ProfileRun(name, llm.model, res, compute_stats(res, CFG, llm.usage), "1초"))
    path = tmp_path / "c.html"
    write_compare_html(str(path), "doc", runs, {"direction": "영어 → 한국어", "style": "formal"},
                       deepl_paras=["딥엘 하나", "딥엘 둘"])
    html = path.read_text(encoding="utf-8")
    assert "번역문 ALPHA" in html and "번역문 BETA" in html
    assert "딥엘 하나" in html and "딥엘 둘" in html
    assert "[1] Reference" not in html                      # keep 행은 비교에서 제외
    assert "번역 A" in html and "번역 B" in html and "data-row='0'" in html and "data-row='2'" in html
    assert html.count("이게 제일 좋음") == 2 * 3             # 2행 × (모델 2 + DeepL 1)


def test_split_deepl_text():
    assert split_deepl_text("a\nb\n\n\nc\n\n d ") == ["a b", "c", "d"]


def test_auth_errors_fail_fast_without_retrying():
    class Denied(FakeAnthropic):
        def create(self, **kw):
            self.calls.append(kw)
            err = RuntimeError("invalid x-api-key")
            err.status_code = 401
            raise err

    fake = Denied()
    cfg = json.loads(json.dumps(CFG))
    cfg["generation"]["max_retries"] = 4
    llm = AnthropicLLM(cfg, "claude-sonnet", client=fake)
    with pytest.raises(RuntimeError, match="호출 실패"):
        llm.generate("translate", "x", task="translate")
    assert len(fake.calls) == 1                      # 재시도 없이 즉시 실패

    class Flaky(FakeAnthropic):                      # 일시 오류(예: 429)는 재시도한다
        attempts = 0

        def create(self, **kw):
            self.attempts += 1
            if self.attempts < 3:
                err = RuntimeError("rate limited")
                err.status_code = 429
                raise err
            return super().create(**kw)

    flaky = Flaky()
    llm2 = AnthropicLLM(cfg, "claude-sonnet", client=flaky)
    llm2.retry_base = 0
    assert json.loads(llm2.generate("translate", "x", task="translate")) == {"ok": True}
    assert flaky.attempts == 3
