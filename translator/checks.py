"""코드 기반 번역 검수 8가지.

LLM 판단에 맡기지 않고 규칙으로 잡을 수 있는 오류(숫자 누락, 보존 요소 훼손,
용어 불일치, 누락/중복, 미번역, 문체 혼용, 괄호 짝)를 검사한다.
각 검사는 config.yaml의 checks 항목으로 켜고 끄거나 기준값을 바꿀 수 있다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CHECK_NAMES = [
    "numbers",
    "preserve",
    "glossary",
    "paragraph_count",
    "length_ratio",
    "untranslated",
    "style",
    "brackets",
]

CHECK_LABELS_KO = {
    "numbers": "숫자·날짜 일치",
    "preserve": "URL·인용·수식·코드 보존",
    "glossary": "용어집 적용",
    "paragraph_count": "문단 누락·중복",
    "length_ratio": "길이 비율",
    "untranslated": "미번역 잔존",
    "style": "문체 일관성",
    "brackets": "괄호·따옴표 짝",
}


@dataclass
class Violation:
    check: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 표시용
        return f"[{self.check}] {self.message}"


@dataclass
class CheckContext:
    direction: str = "en2ko"                 # en2ko | ko2en
    glossary: list[tuple[str, str]] = field(default_factory=list)  # (원문어, 번역어)
    style: str = "formal"                    # formal | plain (en2ko 전용)
    cfg: dict = field(default_factory=dict)  # config.yaml의 checks 섹션


# ---------------------------------------------------------------- 공통 도우미

_PRESERVE_PATTERNS: list[tuple[str, str]] = [
    ("URL", r"https?://[^\s)\]>\"'”’]+"),
    ("이메일", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    ("인용", r"\[\d+(?:\s*[,\-–]\s*\d+)*\]"),
    ("수식", r"\$[^$\n]+\$"),
    ("코드", r"`[^`\n]+`"),
]
_TRAILING_PUNCT = ".,:;!?"


def find_preserved(text: str) -> list[tuple[str, str]]:
    """원문에서 반드시 그대로 보존돼야 하는 요소 [(종류, 값)]을 찾는다."""
    found: list[tuple[str, str]] = []
    rest = text
    for kind, pattern in _PRESERVE_PATTERNS:
        for m in re.finditer(pattern, rest):
            value = m.group(0)
            if kind in ("URL", "이메일"):
                value = value.rstrip(_TRAILING_PUNCT)
            if value:
                found.append((kind, value))
        rest = re.sub(pattern, " ", rest)  # 다른 패턴과 겹치지 않게 제거
    return found


def strip_preserved(text: str) -> str:
    for _, pattern in _PRESERVE_PATTERNS:
        text = re.sub(pattern, " ", text)
    return text


def _enabled(ctx: CheckContext, name: str) -> bool:
    return bool(ctx.cfg.get(name, {}).get("enabled", True))


def _opt(ctx: CheckContext, name: str, key: str, default):
    return ctx.cfg.get(name, {}).get(key, default)


def _has_latin(term: str) -> bool:
    return bool(re.search(r"[A-Za-z]", term))


def _term_in(term: str, text: str) -> bool:
    """용어가 텍스트에 있는지. 영어는 단어 경계, 한글은 부분 문자열."""
    if _has_latin(term):
        pat = r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])"
        return re.search(pat, text, re.IGNORECASE) is not None
    return term in text


# ---------------------------------------------------------------- 1. 숫자

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_EN_SCALE = re.compile(r"\s*(?:million|billion|trillion|thousand|hundred|k\b|m\b|bn\b)", re.I)
_EN_ORDINAL = re.compile(r"(?:st|nd|rd|th)\b", re.I)
_KO_SCALE = re.compile(r"\s*[만억조천백]")


def _numbers(text: str) -> tuple[list[str], bool]:
    """(숫자 목록, 단위 환산이 필요한 숫자가 있었는지). 환산 필요 숫자는 목록에서 제외."""
    clean = strip_preserved(text)
    nums: list[str] = []
    scaled = False
    for m in _NUM_RE.finditer(clean):
        raw = m.group(0).rstrip(",")
        after = clean[m.end():]
        if _EN_SCALE.match(after) or _KO_SCALE.match(after) or _EN_ORDINAL.match(after):
            scaled = True
            continue
        nums.append(raw.replace(",", ""))
    return nums, scaled


def check_numbers(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    s_nums, s_scaled = _numbers(src)
    t_nums, t_scaled = _numbers(tgt)
    out: list[Violation] = []
    t_set = set(t_nums)
    missing = [n for n in dict.fromkeys(s_nums) if n not in t_set]
    if missing:
        out.append(Violation("numbers", f"원문의 숫자가 번역문에 없음: {', '.join(missing[:6])}"))
    # 환산 표기(3 million ↔ 300만)가 섞인 문단은 추가 숫자 검사를 건너뛴다
    if not (s_scaled or t_scaled):
        s_set = set(s_nums)
        extra = [n for n in dict.fromkeys(t_nums) if n not in s_set]
        if extra:
            out.append(Violation("numbers", f"원문에 없는 숫자가 번역문에 있음: {', '.join(extra[:6])}"))
    return out


# ---------------------------------------------------------------- 2. 보존 요소


def check_preserve(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    out: list[Violation] = []
    for kind, value in find_preserved(src):
        if value not in tgt:
            shown = value if len(value) <= 40 else value[:37] + "..."
            out.append(Violation("preserve", f"{kind}가 그대로 보존되지 않음: {shown}"))
    return out


# ---------------------------------------------------------------- 3. 용어집


def glossary_stats(src: str, tgt: str, ctx: CheckContext) -> tuple[int, int, list[tuple[str, str]]]:
    """(적용 대상 수, 적용된 수, 누락 목록)."""
    applicable = applied = 0
    missing: list[tuple[str, str]] = []
    for s_term, t_term in ctx.glossary:
        if not s_term or not t_term:
            continue
        if _term_in(s_term, src):
            applicable += 1
            if _term_in(t_term, tgt):
                applied += 1
            else:
                missing.append((s_term, t_term))
    return applicable, applied, missing


def check_glossary(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    _, _, missing = glossary_stats(src, tgt, ctx)
    return [Violation("glossary", f"용어집 위반: '{s}' → '{t}'로 번역해야 함") for s, t in missing]


# ---------------------------------------------------------------- 4. 문단 수


def check_paragraph(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    out: list[Violation] = []
    if src.strip() and not tgt.strip():
        out.append(Violation("paragraph_count", "번역문이 비어 있음(누락)"))
    elif "\n\n" in tgt.strip() and "\n\n" not in src.strip():
        out.append(Violation("paragraph_count", "한 문단이 여러 문단으로 쪼개짐(중복·추가 의심)"))
    return out


def batch_count_ok(n_src: int, n_out: int) -> bool:
    """배치 단위 문단 수 일치 확인(파이프라인에서 사용)."""
    return n_src == n_out


# ---------------------------------------------------------------- 5. 길이 비율


def _plain_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def check_length_ratio(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    s_len = _plain_len(src)
    if s_len < _opt(ctx, "length_ratio", "min_chars_to_check", 20):
        return []
    bounds = _opt(ctx, "length_ratio", ctx.direction, None) or (
        {"min": 0.35, "max": 1.6} if ctx.direction == "en2ko" else {"min": 1.2, "max": 4.0}
    )
    ratio = _plain_len(tgt) / s_len
    if ratio < bounds["min"]:
        return [Violation("length_ratio", f"번역문이 너무 짧음(비율 {ratio:.2f}, 기준 {bounds['min']} 이상): 누락 의심")]
    if ratio > bounds["max"]:
        return [Violation("length_ratio", f"번역문이 너무 김(비율 {ratio:.2f}, 기준 {bounds['max']} 이하): 환각·반복 의심")]
    return []


# ---------------------------------------------------------------- 6. 미번역

_LATIN_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9'’\-]*$")


def _max_latin_run(text: str) -> int:
    best = run = 0
    for tok in text.split():
        core = tok.strip(".,;:!?()[]{}\"'“”‘’")
        if not core or not re.search(r"[A-Za-z가-힣]", core):
            continue  # 숫자·기호만 있는 토큰은 흐름을 끊지 않는다
        if _LATIN_TOKEN.match(core):
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def check_untranslated(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    body = strip_preserved(tgt)
    if ctx.direction == "en2ko":
        limit = _opt(ctx, "untranslated", "max_latin_run", 8)
        run = _max_latin_run(body)
        if run >= limit:
            return [Violation("untranslated", f"번역되지 않은 영어가 {run}단어 연속으로 남아 있음")]
    else:
        limit = _opt(ctx, "untranslated", "max_hangul_chars", 6)
        n = len(re.findall(r"[가-힣]", body))
        if n >= limit:
            return [Violation("untranslated", f"번역되지 않은 한글이 {n}자 남아 있음")]
    return []


# ---------------------------------------------------------------- 7. 문체

_SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")


def _last_hangul_word(sentence: str) -> str:
    s = sentence.strip().rstrip(".!?。…\"'”’)]} ")
    m = re.search(r"([가-힣]+)$", s)
    return m.group(1) if m else ""


def check_style(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    if ctx.direction != "en2ko":
        return []
    style = ctx.style or _opt(ctx, "style", "default", "formal")
    bad: list[str] = []
    for sent in _SENT_SPLIT.split(tgt):
        word = _last_hangul_word(sent)
        if len(word) < 2:
            continue
        ends_nida = word.endswith("니다")
        ends_da = word.endswith("다")
        ends_yo = word.endswith("요")
        if style == "formal":
            if (ends_da and not ends_nida) or ends_yo:
                bad.append(sent.strip())
        elif style == "plain":
            if ends_nida or ends_yo:
                bad.append(sent.strip())
    if not bad:
        return []
    example = bad[0] if len(bad[0]) <= 30 else bad[0][:27] + "..."
    wanted = "격식체(~입니다/합니다)" if style == "formal" else "평어체(~이다/한다)"
    return [Violation("style", f"{wanted}가 아닌 문장 {len(bad)}개: \"{example}\"")]


# ---------------------------------------------------------------- 8. 괄호·따옴표

_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {v: k for k, v in _OPEN.items()}


def _balanced(text: str) -> bool:
    stack: list[str] = []
    for ch in text:
        if ch in _OPEN:
            stack.append(ch)
        elif ch in _CLOSE:
            if not stack or stack.pop() != _CLOSE[ch]:
                return False
    if stack:
        return False
    if text.count("“") != text.count("”"):
        return False
    if text.count('"') % 2 != 0:
        return False
    return True


def check_brackets(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    # 원문 자체가 "1) 항목"처럼 짝이 안 맞으면 번역문도 기준으로 삼을 수 없다
    if _balanced(src) and not _balanced(tgt):
        return [Violation("brackets", "괄호 또는 따옴표의 짝이 맞지 않음")]
    return []


# ---------------------------------------------------------------- 실행

_CHECKS = {
    "numbers": check_numbers,
    "preserve": check_preserve,
    "glossary": check_glossary,
    "paragraph_count": check_paragraph,
    "length_ratio": check_length_ratio,
    "untranslated": check_untranslated,
    "style": check_style,
    "brackets": check_brackets,
}


def run_checks(src: str, tgt: str, ctx: CheckContext) -> list[Violation]:
    """켜져 있는 모든 검사를 실행해 위반 목록을 돌려준다."""
    violations: list[Violation] = []
    for name in CHECK_NAMES:
        if not _enabled(ctx, name):
            continue
        violations.extend(_CHECKS[name](src, tgt, ctx))
    return violations


def enabled_checks(ctx: CheckContext) -> list[str]:
    return [n for n in CHECK_NAMES if _enabled(ctx, n)]
