"""번역·비교 실행 로직. 명령줄(translate.py, compare.py)과 웹 앱(app.py)이 같이 쓴다."""
from __future__ import annotations

import csv
import io
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from .compare_report import ProfileRun, split_deepl_text, write_compare_html
from .extract import extract_pdf, parse_pages
from .llm import DryRunLLM, make_llm, profile_cfg, profile_ready, resolve_profile
from .outputs import write_docx, write_markdown
from .pipeline import run_pipeline
from .report import compute_stats, write_csv_report, write_html_report


class RunnerError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류."""


# ------------------------------------------------------------------ 도우미

def fmt_elapsed(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}분 {s}초" if m else f"{s}초"


def parse_glossary(text: str, direction: str) -> list[tuple[str, str]]:
    """CSV 텍스트(en,ko 열 또는 src,tgt 열) → [(원문어, 번역어)]."""
    pairs: list[tuple[str, str]] = []
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if "en" in row and "ko" in row:
            en, ko = row["en"], row["ko"]
            pairs.append((en, ko) if direction == "en2ko" else (ko, en))
        elif "src" in row and "tgt" in row:
            pairs.append((row["src"], row["tgt"]))
    return [(s, t) for s, t in pairs if s and t]


def load_glossary(path: str | None, direction: str) -> list[tuple[str, str]]:
    if not path:
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return parse_glossary(f.read(), direction)


def ready_profiles(cfg: dict) -> tuple[list[str], dict[str, str]]:
    """바로 쓸 수 있는 프로필 이름 목록과, 못 쓰는 프로필의 이유."""
    ok_names: list[str] = []
    skipped: dict[str, str] = {}
    for name, prof in (cfg.get("profiles") or {}).items():
        ok, why = profile_ready(prof)
        if ok:
            ok_names.append(name)
        else:
            skipped[name] = why
    return ok_names, skipped


def count_selected_pages(pdf_path: str, pages: str | None) -> tuple[int, int]:
    """(선택된 쪽 수, 전체 쪽 수)."""
    try:
        with pymupdf.open(pdf_path) as doc:
            return len(parse_pages(pages, len(doc))), len(doc)
    except Exception as exc:
        raise RunnerError(f"PDF를 열 수 없습니다: {exc}") from exc


def _check_page_limit(pdf_path: str, pages: str | None, max_pages: int | None) -> int:
    n, total = count_selected_pages(pdf_path, pages)
    if n == 0:
        raise RunnerError(f"선택한 쪽 범위에 해당하는 쪽이 없습니다 (전체 {total}쪽).")
    if max_pages and n > max_pages:
        raise RunnerError(f"한 번에 {max_pages}쪽까지만 처리합니다 (선택 {n}쪽). 쪽 범위를 나눠 주세요. 예: 1-{max_pages}")
    return n


def _direction_label(direction: str) -> str:
    return "영어 → 한국어" if direction == "en2ko" else "한국어 → 영어"


# ------------------------------------------------------------------ 단일 번역

@dataclass
class TranslateResult:
    stem: str
    out_dir: str
    profile: str
    model: str
    stats: dict
    elapsed: str
    files: dict[str, str] = field(default_factory=dict)   # docx, md, report, csv, json
    warnings: list[str] = field(default_factory=list)


def translate_document(
    pdf_path: str, cfg: dict, *, profile: str | None = None, direction: str = "en2ko",
    domain: str = "", style: str = "formal", glossary: list[tuple[str, str]] | None = None,
    pages: str | None = None, out_dir: str, dry_run: bool = False, max_pages: int | None = None,
    progress: Callable[[str], None] = print, llm=None,
) -> TranslateResult:
    _check_page_limit(pdf_path, pages, max_pages)
    os.makedirs(out_dir, exist_ok=True)
    if llm is None:
        try:
            llm = DryRunLLM(cfg) if dry_run else make_llm(cfg, profile)
        except RuntimeError as exc:
            raise RunnerError(str(exc)) from exc
    started = time.time()
    stem = os.path.splitext(os.path.basename(pdf_path))[0]

    progress("PDF 읽는 중")
    use_llm = llm if (not dry_run and getattr(llm, "supports_vision", False)) else None
    segments, warnings = extract_pdf(pdf_path, cfg, llm=use_llm, pages=pages, direction=direction, progress=progress)
    if not segments:
        raise RunnerError("번역할 내용이 없습니다. 스캔본(이미지 PDF)이면 아직 지원하지 않습니다(OCR은 3단계 예정).")
    progress(f"추출 완료: 문단 {sum(s.kind == 'text' for s in segments)}, 표 {sum(s.kind == 'table' for s in segments)}, "
             f"원문 유지 {sum(s.kind == 'keep' for s in segments)}")

    try:
        result = run_pipeline(segments, llm, cfg, direction=direction, domain=domain, style=style,
                              user_glossary=glossary or [], progress=progress)
    except RuntimeError as exc:
        raise RunnerError(str(exc)) from exc
    result.warnings = warnings
    pname = getattr(llm, "name", profile or "")
    stats = compute_stats(result, cfg if dry_run else profile_cfg(cfg, pname), usage=llm.usage)
    elapsed = fmt_elapsed(time.time() - started)

    lang = "ko" if direction == "en2ko" else "en"
    meta = {"direction": _direction_label(direction), "style": style,
            "model": llm.model + (" (DRY-RUN)" if dry_run else ""), "elapsed": elapsed}
    files = {
        "docx": os.path.join(out_dir, f"{stem}.{lang}.docx"),
        "md": os.path.join(out_dir, f"{stem}.{lang}.md"),
        "report": os.path.join(out_dir, "report.html"),
        "csv": os.path.join(out_dir, "report.csv"),
        "json": os.path.join(out_dir, "result.json"),
    }
    write_docx(result, files["docx"], stem)
    write_markdown(result, files["md"], stem)
    write_html_report(result, stats, files["report"], stem, meta)
    write_csv_report(result, files["csv"])
    with open(files["json"], "w", encoding="utf-8") as f:
        json.dump({"stats": stats, "analysis": result.analysis,
                   "units": [{"page": u.page, "kind": u.kind, "src": u.src, "tgt": u.tgt,
                              "flagged": u.flagged, "retries": u.retries} for u in result.units]},
                  f, ensure_ascii=False, indent=1)
    return TranslateResult(stem, out_dir, pname, llm.model, stats, elapsed, files, warnings)


# ------------------------------------------------------------------ 모델 비교

@dataclass
class CompareResult:
    stem: str
    out_dir: str
    compare_html: str
    runs: list[ProfileRun]
    run_files: dict[str, dict[str, str]]       # 프로필 → {docx, md, report, csv}
    failed: dict[str, str]
    warnings: list[str]
    deepl_note: str = ""


def compare_documents(
    pdf_path: str, cfg: dict, *, profiles: list[str] | None = None, direction: str = "en2ko",
    domain: str = "", style: str = "formal", glossary: list[tuple[str, str]] | None = None,
    pages: str | None = None, out_dir: str, dry_run: bool = False, max_pages: int | None = None,
    deepl_text: str | None = None, progress: Callable[[str], None] = print, llms: list | None = None,
) -> CompareResult:
    _check_page_limit(pdf_path, pages, max_pages)
    os.makedirs(out_dir, exist_ok=True)

    if llms is None:
        all_profiles = list((cfg.get("profiles") or {}).keys())
        if dry_run:
            names = profiles or all_profiles[:2] or ["dry-A", "dry-B"]
            llms = []
            for n in names:
                d = DryRunLLM(cfg, name=n)
                if n in all_profiles:
                    d.model = resolve_profile(cfg, n)[1].get("model", "dry-run")
                llms.append(d)
        else:
            if profiles:
                names = profiles
                for n in names:
                    ok, why = profile_ready(resolve_profile(cfg, n)[1])
                    if not ok:
                        raise RunnerError(f"프로필 '{n}'을 쓸 수 없습니다 — {why}")
            else:
                names, skipped = ready_profiles(cfg)
                for n, why in skipped.items():
                    progress(f"'{n}' 제외: {why}")
            if not names:
                raise RunnerError("비교할 수 있는 프로필이 없습니다. API 키(환경변수/Secrets)와 config.yaml을 확인하세요.")
            try:
                llms = [make_llm(cfg, n) for n in names]
            except RuntimeError as exc:
                raise RunnerError(str(exc)) from exc
    progress("비교 대상: " + ", ".join(f"{l.name}({l.model})" for l in llms))

    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    reader = next((l for l in llms if not dry_run and getattr(l, "supports_vision", False)), None)
    progress("PDF 읽는 중" + (f" (이미지 읽기: {reader.name})" if reader else ""))
    segments, warnings = extract_pdf(pdf_path, cfg, llm=reader, pages=pages, direction=direction, progress=progress)
    if not segments:
        raise RunnerError("번역할 내용이 없습니다. 스캔본(이미지 PDF)이면 아직 지원하지 않습니다(OCR은 3단계 예정).")

    runs: list[ProfileRun] = []
    run_files: dict[str, dict[str, str]] = {}
    failed: dict[str, str] = {}
    lang = "ko" if direction == "en2ko" else "en"
    for llm in llms:
        progress(f"===== {llm.name} ({llm.model}) =====")
        t0 = time.time()
        try:
            result = run_pipeline(segments, llm, cfg, direction=direction, domain=domain, style=style,
                                  user_glossary=glossary or [],
                                  progress=lambda m, n=llm.name: progress(f"[{n}] {m}"))
        except Exception as exc:     # 한 모델이 실패해도 나머지 비교는 계속
            failed[llm.name] = str(exc)
            progress(f"! {llm.name} 실패: {exc}")
            continue
        elapsed = fmt_elapsed(time.time() - t0)
        stats = compute_stats(result, cfg if dry_run else profile_cfg(cfg, llm.name), usage=llm.usage)
        sub = os.path.join(out_dir, llm.name)
        os.makedirs(sub, exist_ok=True)
        meta = {"direction": _direction_label(direction), "style": style, "model": llm.model, "elapsed": elapsed}
        files = {"docx": os.path.join(sub, f"{stem}.{lang}.docx"), "md": os.path.join(sub, f"{stem}.{lang}.md"),
                 "report": os.path.join(sub, "report.html"), "csv": os.path.join(sub, "report.csv")}
        write_docx(result, files["docx"], stem)
        write_markdown(result, files["md"], stem)
        write_html_report(result, stats, files["report"], stem, meta)
        write_csv_report(result, files["csv"])
        runs.append(ProfileRun(llm.name, llm.model, result, stats, elapsed))
        run_files[llm.name] = files
        progress(f"{llm.name}: 점수 {stats['score']} / 확인 필요 {stats['flagged']} / {elapsed}")
    if not runs:
        raise RunnerError("모든 모델이 실패했습니다: " + "; ".join(f"{k}: {v}" for k, v in failed.items()))

    deepl_paras = deepl_full = None
    note = ""
    if deepl_text and deepl_text.strip():
        paras = split_deepl_text(deepl_text)
        n_text = sum(1 for it in runs[0].result.rendered() if it["kind"] == "text")
        if len(paras) == n_text:
            deepl_paras = paras
        else:
            deepl_full = deepl_text
            note = (f"DeepL 문단 수({len(paras)})가 번역 단위({n_text})와 달라 자동으로 짝짓지 못했습니다. "
                    "비교 페이지 아래에 전체 텍스트로 붙였습니다.")
            progress(note)

    path = os.path.join(out_dir, "compare.html")
    write_compare_html(path, stem, runs, {"direction": _direction_label(direction), "style": style},
                       deepl_paras, deepl_full)
    return CompareResult(stem, out_dir, path, runs, run_files, failed, warnings, note)
