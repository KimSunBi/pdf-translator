"""LLM에 보내는 프롬프트 모음. 품질 튜닝은 이 파일만 고치면 된다."""
from __future__ import annotations

import json

LANG = {"en2ko": ("English", "Korean"), "ko2en": ("Korean", "English")}


def _style_rule(direction: str, style: str) -> str:
    if direction != "en2ko":
        return "Use natural, fluent written English appropriate to the document type."
    if style == "plain":
        return ("Write in plain written Korean (평어체: sentences end with ~이다/~한다), "
                "as in academic papers. Never mix in ~습니다 or ~해요 endings.")
    return ("Write in formal polite Korean (격식체: sentences end with ~입니다/~합니다/~습니다). "
            "Never mix in ~이다/~한다 or ~해요 endings.")


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ 분석
def analyze_prompt(direction: str, domain: str, sample: str, user_glossary: list[tuple[str, str]]):
    src, tgt = LANG[direction]
    system = ("You are a senior translation project manager. You read a document before it is "
              "translated and prepare a brief for the translators.")
    prompt = f"""Source language: {src}. Target language: {tgt}. Declared domain: {domain or "(not specified)"}.

Read the document excerpts and return JSON with:
- "summary": 3-5 sentences in {tgt} describing what the document is about and who it is for
- "doc_type": e.g. research paper, contract, textbook, report
- "domain": the actual subject domain
- "tone": the register the translation should keep
- "key_terms": up to 40 terms/phrases that recur or are technical and must be translated consistently,
  each as {{"src": "...", "tgt": "...(your recommended {tgt} rendering)"}}.
  Do not include terms already in the user glossary below. Include proper nouns only if they need a fixed rendering.

User glossary (takes priority, do not contradict):
{_json([{"src": s, "tgt": t} for s, t in user_glossary]) if user_glossary else "(none)"}

<document_excerpts>
{sample}
</document_excerpts>

Return JSON only."""
    return system, prompt


# ------------------------------------------------------------------ 번역
def translate_system(direction: str, domain: str, style: str, analysis: dict) -> str:
    src, tgt = LANG[direction]
    brief = ""
    if analysis:
        brief = (f"\nDocument brief: {analysis.get('summary', '')}\n"
                 f"Document type: {analysis.get('doc_type', '')}; domain: {analysis.get('domain', domain)}; "
                 f"tone: {analysis.get('tone', '')}\n")
    return f"""You are a professional {src}-to-{tgt} translator producing publication-quality translations.
{brief}
Rules:
1. Translate meaning faithfully. Do not omit, add, summarize, or explain anything.
2. {_style_rule(direction, style)}
3. Use the provided glossary terms exactly as given whenever the source term appears (inflected forms included).
4. Keep URLs, e-mail addresses, citation markers like [12], inline math $...$ and `code` EXACTLY as in the source.
5. Keep every number, date, and unit accurate. Convert nothing.
6. Keep proper nouns, author names, and titles in their original form, or use the established {tgt} rendering when one exists.
7. Translate for the document as a whole: keep terminology consistent with the earlier context provided.
8. Output must be valid JSON in the requested shape and nothing else."""


def translate_prompt(units: list[dict], context: list[dict], glossary: list[dict]) -> str:
    payload = {
        "earlier_context": context,
        "glossary": glossary,
        "paragraphs": units,
    }
    return f"""Translate each paragraph in "paragraphs". "earlier_context" is for continuity only; do not translate it.

<input>
{_json(payload)}
</input>

Return JSON: {{"translations": [{{"id": <same id>, "text": "<translation>"}}, ...]}} with exactly one entry per input paragraph, same ids."""


def retry_prompt(unit: dict, previous: str, violations: list[str], glossary: list[dict], context: list[dict]) -> str:
    payload = {"earlier_context": context, "glossary": glossary, "paragraph": unit,
               "previous_attempt": previous, "problems_found": violations}
    return f"""Your previous translation of this paragraph failed automatic checks. Translate it again and fix every problem listed.

<input>
{_json(payload)}
</input>

Return JSON: {{"translations": [{{"id": {unit["id"]}, "text": "<corrected translation>"}}]}}"""


# ------------------------------------------------------------------ LLM 검수
def review_system(direction: str, style: str) -> str:
    src, tgt = LANG[direction]
    return f"""You are a meticulous bilingual reviewer ({src} → {tgt}). Compare each source paragraph with its translation.
Look for: mistranslation, omission, addition, wrong terminology, unnatural phrasing, and register errors.
{_style_rule(direction, style)}
Automatic checks have already verified numbers, URLs, citations, glossary terms, length and brackets, so focus on meaning and naturalness.
Only propose a fix when something is actually wrong or clearly awkward. If a fix is needed, "fixed" must be the complete corrected paragraph,
keeping URLs, citation markers, math, and code exactly as they are. Do not rewrite acceptable translations merely to change style."""


def review_prompt(items: list[dict], glossary: list[dict]) -> str:
    payload = {"glossary": glossary, "paragraphs": items}
    return f"""Review these translations.

<input>
{_json(payload)}
</input>

Return JSON: {{"reviews": [{{"id": <id>, "ok": true|false, "issue": "<short description or empty>", "fixed": "<corrected paragraph or empty>"}}, ...]}}
One entry per paragraph, same ids."""


# ------------------------------------------------------------------ 이미지로 읽기
def vision_prompt(direction: str):
    src, _ = LANG[direction]
    system = "You are a precise document layout reader. You convert a page image into clean structured text."
    prompt = f"""This is one page of a {src} PDF. Read it in natural reading order (for two-column pages: whole left column first, then the right column).

Return JSON: {{"items": [...]}} where each item is one of:
- {{"kind": "text", "text": "<one full paragraph or heading, line breaks and hyphenation removed>"}}
- {{"kind": "table", "rows": [["cell", "cell"], ...]}}  (one entry per table; keep cell text exact)
- {{"kind": "keep", "text": "..."}}  for formulas, code blocks, and reference/bibliography entries that must not be translated

Do NOT include running headers, footers, or page numbers. Do not translate anything. Do not invent text that is not on the page.
Return JSON only."""
    return system, prompt
