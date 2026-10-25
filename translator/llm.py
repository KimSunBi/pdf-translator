"""모델 어댑터: Gemini / Claude(Anthropic) / OpenAI 호환(오픈소스 호스팅·로컬 서버).

파이프라인과 검수 코드는 모델을 몰라도 되도록, 모든 어댑터가 같은 generate()를 제공한다.
API 키는 환경변수에서만 읽는다 (profile의 api_key_env).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from collections import defaultdict


def parse_json(text: str):
    """모델이 ```json 펜스나 앞뒤 설명을 붙여도 JSON을 꺼내 파싱한다."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
        if not starts:
            raise
        start = min(starts)
        end = max(t.rfind("}"), t.rfind("]"))
        return json.loads(t[start:end + 1])


class UsageTracker:
    def __init__(self) -> None:
        self.by_task: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0})

    def add(self, task: str, inp: int, out: int) -> None:
        d = self.by_task[task]
        d["calls"] += 1
        d["input"] += inp
        d["output"] += out

    @property
    def total_input(self) -> int:
        return sum(d["input"] for d in self.by_task.values())

    @property
    def total_output(self) -> int:
        return sum(d["output"] for d in self.by_task.values())

    @property
    def total_calls(self) -> int:
        return sum(d["calls"] for d in self.by_task.values())


# ------------------------------------------------------------------ 프로필

def resolve_profile(cfg: dict, name: str | None = None) -> tuple[str, dict]:
    """config의 profiles에서 (이름, 설정)을 고른다. profiles가 없으면 옛 models 설정으로 만든다."""
    profiles = cfg.get("profiles")
    if not profiles:
        legacy = cfg.get("models", {}).get("translate", "gemini-3.1-pro-preview")
        return "gemini-legacy", {"provider": "gemini", "model": legacy,
                                 "api_key_env": "GEMINI_API_KEY", "vision": True}
    name = name or cfg.get("active_profile") or next(iter(profiles))
    if name not in profiles:
        raise RuntimeError(f"프로필 '{name}'이 config.yaml에 없습니다. 사용 가능: {', '.join(profiles)}")
    return name, profiles[name]


def profile_cfg(cfg: dict, name: str) -> dict:
    """프로필별 요금표를 반영한 config 사본 (보고서의 비용 계산용)."""
    import copy

    _, prof = resolve_profile(cfg, name)
    out = copy.deepcopy(cfg)
    if prof.get("pricing"):
        out["pricing"] = prof["pricing"]
    return out


def key_available(profile: dict) -> bool:
    env = profile.get("api_key_env")
    if not env:                      # 로컬 서버처럼 키가 필요 없는 프로필
        return True
    return bool(os.environ.get(env))


def profile_ready(profile: dict) -> tuple[bool, str]:
    """프로필을 바로 쓸 수 있는지 (ok, 안 되는 이유). compare.py가 자동으로 걸러낼 때 쓴다."""
    for key in ("model", "base_url"):
        val = profile.get(key)
        if (key == "model" and not val) or (isinstance(val, str) and val.startswith("<")):
            return False, f"{key} 값이 아직 설정되지 않음"
    if profile.get("provider") == "openai_compatible" and not profile.get("base_url"):
        return False, "base_url이 설정되지 않음"
    if not key_available(profile):
        return False, f"{profile.get('api_key_env')} 환경변수가 없음"
    return True, ""


def _read_key(profile: dict, label: str) -> str:
    env = profile.get("api_key_env")
    if not env:
        return "EMPTY"
    key = os.environ.get(env)
    if not key and env == "GEMINI_API_KEY":
        key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            f"{env} 환경변수가 없습니다 ({label}). 해당 서비스에서 API 키를 발급받아 "
            f"환경변수로 설정하세요 (README 참고).")
    return key


class _Base:
    supports_vision = False
    label = "LLM"

    def __init__(self, cfg: dict, name: str, profile: dict) -> None:
        gen = cfg.get("generation", {})
        self.name = name
        self.profile = profile
        self.model = profile["model"]
        self.supports_vision = bool(profile.get("vision", False))
        self.temperature = profile.get("temperature", gen.get("temperature"))
        self.max_retries = int(gen.get("max_retries", 4))
        self.retry_base = float(gen.get("retry_base_seconds", 2))
        self.max_output_tokens = int(profile.get("max_output_tokens", 16000))
        self.usage = UsageTracker()

    @staticmethod
    def _non_retryable(exc: Exception) -> bool:
        """키 오류(401/403)·모델 없음(404)은 기다려도 소용없으므로 바로 실패시킨다."""
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        return isinstance(status, int) and status in (401, 403, 404)

    def _retry(self, fn):
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except Exception as exc:     # 네트워크·쿼터·빈 응답 모두 재시도
                last = exc
                if self._non_retryable(exc):
                    break
                if attempt < self.max_retries:
                    time.sleep(min(60.0, self.retry_base * (2 ** attempt)))
        raise RuntimeError(f"{self.label} 호출 실패({self.model}): {last}")


# ------------------------------------------------------------------ Gemini

class GeminiLLM(_Base):
    label = "Gemini"

    def __init__(self, cfg: dict, name: str | None = None, profile: dict | None = None, client=None) -> None:
        if profile is None:
            name, profile = resolve_profile(cfg, name)
        super().__init__(cfg, name or "gemini", profile)
        from google.genai import types

        self._types = types
        if client is None:
            from google import genai

            client = genai.Client(api_key=_read_key(profile, "Gemini"))
        self.client = client

    def generate(self, role: str, prompt: str, system: str | None = None,
                 images: list[bytes] | None = None, json_mode: bool = False,
                 task: str | None = None) -> str:
        types = self._types
        kwargs: dict = {}
        if system:
            kwargs["system_instruction"] = system
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        config = types.GenerateContentConfig(**kwargs)
        contents: list = [prompt]
        for png in images or []:
            contents.append(types.Part.from_bytes(data=png, mime_type="image/png"))

        def call() -> str:
            resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
            text = resp.text or ""
            if not text.strip():
                raise RuntimeError("빈 응답")
            um = getattr(resp, "usage_metadata", None)
            inp = getattr(um, "prompt_token_count", 0) or 0
            out = (getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
            self.usage.add(task or role, inp, out)
            return text

        return self._retry(call)


# ------------------------------------------------------------------ Claude

class AnthropicLLM(_Base):
    label = "Claude"

    def __init__(self, cfg: dict, name: str | None = None, profile: dict | None = None, client=None) -> None:
        if profile is None:
            name, profile = resolve_profile(cfg, name)
        profile = {"vision": True, **profile}
        super().__init__(cfg, name or "claude", profile)
        if client is None:
            key = _read_key(profile, "Claude")           # 키 안내가 패키지 오류보다 먼저 나오게
            try:
                import anthropic
            except ImportError as exc:
                raise RuntimeError("anthropic 패키지가 없습니다. pip install anthropic") from exc
            client = anthropic.Anthropic(api_key=key)
        self.client = client

    def generate(self, role: str, prompt: str, system: str | None = None,
                 images: list[bytes] | None = None, json_mode: bool = False,
                 task: str | None = None) -> str:
        content: list[dict] = []
        for png in images or []:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}})
        text = prompt + ("\n\nRespond with a single valid JSON value and nothing else." if json_mode else "")
        content.append({"type": "text", "text": text})
        kwargs: dict = {"model": self.model, "max_tokens": self.max_output_tokens,
                        "messages": [{"role": "user", "content": content}]}
        if system:
            kwargs["system"] = system
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        def call() -> str:
            resp = self.client.messages.create(**kwargs)
            if getattr(resp, "stop_reason", "") == "max_tokens":
                raise RuntimeError("출력 길이 초과(max_output_tokens를 늘리거나 batch_size를 줄이세요)")
            out = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
            if not out.strip():
                raise RuntimeError("빈 응답")
            u = getattr(resp, "usage", None)
            self.usage.add(task or role, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)
            return out

        return self._retry(call)


# ------------------------------------------------------------------ OpenAI 호환 (오픈소스 호스팅·로컬)

class OpenAICompatLLM(_Base):
    """OpenAI 호환 API(chat.completions)를 쓰는 모든 서버: 오픈소스 모델 호스팅 서비스, Ollama, vLLM 등."""
    label = "OpenAI호환"

    def __init__(self, cfg: dict, name: str | None = None, profile: dict | None = None, client=None) -> None:
        if profile is None:
            name, profile = resolve_profile(cfg, name)
        super().__init__(cfg, name or "openai-compat", profile)
        self.json_format = bool(profile.get("json_response_format", True))
        if client is None:
            base = profile.get("base_url")
            if not base or str(base).startswith("<"):
                raise RuntimeError(f"프로필 '{self.name}'의 base_url이 설정되지 않았습니다 (config.yaml).")
            key = _read_key(profile, "OpenAI호환")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("openai 패키지가 없습니다. pip install openai") from exc
            client = OpenAI(api_key=key, base_url=base)
        self.client = client

    def generate(self, role: str, prompt: str, system: str | None = None,
                 images: list[bytes] | None = None, json_mode: bool = False,
                 task: str | None = None) -> str:
        user_content: object = prompt
        if images and self.supports_vision:
            user_content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(p).decode()}}
                for p in images]
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user_content}]

        def call() -> str:
            kwargs: dict = {"model": self.model, "messages": messages, "max_tokens": self.max_output_tokens}
            if self.temperature is not None:
                kwargs["temperature"] = self.temperature
            use_format = json_mode and self.json_format
            if use_format:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception:
                if use_format:             # 서버가 response_format을 지원하지 않을 수 있다
                    self.json_format = False
                raise
            out = (resp.choices[0].message.content or "")
            if not out.strip():
                raise RuntimeError("빈 응답")
            u = getattr(resp, "usage", None)
            self.usage.add(task or role, getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)
            return out

        return self._retry(call)


# ------------------------------------------------------------------ 팩토리

class GoogleWebLLM:
    """구글 번역(웹)으로 번역하는 어댑터. API 키가 필요 없다 (deep-translator 사용).

    LLM이 아니라서 문맥·용어집·문체 지시를 따르지 못한다. 그래서 파이프라인에서는 분석·LLM 검수·자동 재번역을
    건너뛰고(pipeline_overrides), 코드 검수(숫자·보존·누락·길이 등)만 돌려 문제 문단을 노란색으로 표시한다.
    구글의 비공식 웹 주소를 쓰므로 많이 쓰면 차단될 수 있고, 예고 없이 막힐 수 있다.
    """
    supports_vision = False
    MAX_CHARS = 4500
    pipeline_overrides = {"llm_review": False, "max_auto_retranslate": 0}
    checks_off = ("style",)

    def __init__(self, cfg: dict, name: str, profile: dict, translator=None) -> None:
        self.name = name
        self.model = profile.get("model", "google-translate-web")
        self.usage = UsageTracker()
        self.max_retries = int(profile.get("max_retries", 4))
        self.retry_base = float(profile.get("retry_base_seconds", 5))      # 요청 과다(429)는 오래 쉬어야 풀린다
        self.delay = float(profile.get("delay_seconds", 1.0))              # 요청 사이 최소 간격(초)
        self._translator = translator          # 테스트용: (text, src, tgt) -> str

    # --- 번역 한 건 -------------------------------------------------
    def _call(self, text: str, src: str, tgt: str) -> str:
        if self._translator is not None:
            return self._translator(text, src, tgt)
        try:
            from deep_translator import GoogleTranslator
        except ImportError as exc:
            raise RuntimeError("deep-translator 패키지가 없습니다. requirements.txt를 설치하세요.") from exc
        return GoogleTranslator(source=src, target=tgt).translate(text) or ""

    def _chunks(self, text: str) -> list[str]:
        if len(text) <= self.MAX_CHARS:
            return [text]
        parts, cur = [], ""
        for sent in re.split(r"(?<=[.!?。])\s+", text):
            if cur and len(cur) + len(sent) + 1 > self.MAX_CHARS:
                parts.append(cur)
                cur = sent
            else:
                cur = f"{cur} {sent}".strip()
        if cur:
            parts.append(cur)
        return parts

    def _translate_many(self, texts: list[str], src: str, tgt: str) -> list[str]:
        """여러 문단을 한 번의 요청으로 보내 요청 횟수를 줄인다 (구글은 요청 과다로 막히기 쉽다).
        문단 사이는 빈 줄로 구분하고, 돌아온 문단 수가 맞지 않으면 문단별로 다시 요청한다."""
        todo = [i for i, t in enumerate(texts) if re.search(r"[A-Za-z가-힣]", t)]
        out = list(texts)
        joined = "\n\n".join(texts[i] for i in todo)
        if len(todo) > 1 and len(joined) <= self.MAX_CHARS:
            res = self._translate(joined, src, tgt)
            parts = [p.strip() for p in re.split(r"\n\s*\n", res) if p.strip()]
            if len(parts) == len(todo):
                for i, p in zip(todo, parts):
                    out[i] = p
                return out
        for i in todo:
            out[i] = self._translate(texts[i], src, tgt)
        return out

    def _translate(self, text: str, src: str, tgt: str) -> str:
        if not re.search(r"[A-Za-z가-힣]", text):
            return text                                  # 숫자·기호뿐이면 그대로
        out = []
        for chunk in self._chunks(text):
            last: Exception | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    out.append(self._call(chunk, src, tgt))
                    self.usage.add("translate", len(chunk), 0)      # 토큰 대신 글자 수를 센다
                    last = None
                    break
                except Exception as exc:
                    last = exc
                    if attempt < self.max_retries:
                        time.sleep(min(60.0, self.retry_base * (2 ** attempt)))
            if last is not None:
                raise RuntimeError(f"구글 번역 호출 실패: {last.__class__.__name__}: {str(last)[:200]} "
                                   "(많이 쓰면 일시적으로 차단될 수 있습니다. 잠시 뒤 다시 시도하거나 다른 모델을 쓰세요)") from last
            if self.delay:
                time.sleep(self.delay)
        return " ".join(out)

    # --- LLM 인터페이스 ---------------------------------------------
    def generate(self, role, prompt, system=None, images=None, json_mode=False, task=None) -> str:
        if task == "analyze":
            return json.dumps({"summary": "", "doc_type": "", "domain": "", "tone": "", "key_terms": []})
        if task == "vision":
            return json.dumps({"items": []})
        payload = json.loads(re.search(r"<input>\s*(.*?)\s*</input>", prompt, re.S).group(1))
        if task == "review":
            return json.dumps({"reviews": [{"id": p["id"], "ok": True, "issue": "", "fixed": ""}
                                           for p in payload.get("paragraphs", [])]}, ensure_ascii=False)
        src, tgt = ("ko", "en") if system and "Korean-to-English" in system else ("en", "ko")
        paras = payload.get("paragraphs") or [payload["paragraph"]]
        texts = self._translate_many([p["text"] for p in paras], src, tgt)
        return json.dumps({"translations": [{"id": p["id"], "text": t} for p, t in zip(paras, texts)]},
                          ensure_ascii=False)


PROVIDERS = {"gemini": GeminiLLM, "anthropic": AnthropicLLM, "openai_compatible": OpenAICompatLLM,
             "google_translate_web": GoogleWebLLM}


def make_llm(cfg: dict, name: str | None = None):
    pname, prof = resolve_profile(cfg, name)
    provider = prof.get("provider", "gemini")
    if provider not in PROVIDERS:
        raise RuntimeError(f"알 수 없는 provider '{provider}' (프로필 {pname}). 사용 가능: {', '.join(PROVIDERS)}")
    return PROVIDERS[provider](cfg, pname, prof)


# ------------------------------------------------------------------ 테스트용

class DryRunLLM:
    """API 없이 파이프라인 전체를 돌려보는 가짜 LLM. 번역문은 '[DRY] 원문' 형태다."""
    supports_vision = False

    def __init__(self, cfg: dict | None = None, name: str = "dry-run") -> None:
        self.usage = UsageTracker()
        self.name = name
        self.model = "dry-run"

    def generate(self, role, prompt, system=None, images=None, json_mode=False, task=None) -> str:
        self.usage.add(task or role, 0, 0)
        delay = float(os.environ.get("DRYRUN_DELAY", "0") or 0)       # 진행 화면을 눈으로 확인할 때만 쓰는 인위적 지연(초)
        if delay:
            time.sleep(delay)
        if task == "analyze":
            return json.dumps({"summary": "(dry-run)", "doc_type": "document", "domain": "general",
                               "tone": "neutral", "key_terms": []}, ensure_ascii=False)
        if task == "vision":
            return json.dumps({"items": []})
        payload = json.loads(re.search(r"<input>\s*(.*?)\s*</input>", prompt, re.S).group(1))
        if task == "review":
            return json.dumps({"reviews": [{"id": p["id"], "ok": True, "issue": "", "fixed": ""}
                                           for p in payload["paragraphs"]]}, ensure_ascii=False)
        paras = payload.get("paragraphs") or [payload["paragraph"]]
        return json.dumps({"translations": [{"id": p["id"], "text": "[DRY] " + p["text"]} for p in paras]},
                          ensure_ascii=False)
