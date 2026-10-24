#!/usr/bin/env python3
"""PDF 번역기 (명령줄 버전). 웹 앱은 app.py.

  python translate.py paper.pdf --glossary glossary.csv --domain 교육 --style formal
  python translate.py paper.pdf --pages 1-5          # 일부만 먼저 시험
  python translate.py paper.pdf --extract-only       # API 없이 추출 결과만 확인
  python translate.py paper.pdf --dry-run            # API 없이 전체 흐름만 확인
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from translator.extract import extract_pdf                                   # noqa: E402
from translator.runner import (RunnerError, fmt_elapsed, load_glossary,      # noqa: E402,F401
                               translate_document)


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF 번역기 (코드 검수 포함)")
    ap.add_argument("pdf")
    ap.add_argument("--direction", choices=["en2ko", "ko2en"], default="en2ko")
    ap.add_argument("--domain", default="", help="분야 (예: 교육, 기독교, 법률, 논문)")
    ap.add_argument("--style", choices=["formal", "plain"], default="formal",
                    help="formal=~입니다/합니다, plain=~이다/한다 (en2ko에만 적용)")
    ap.add_argument("--glossary", help="용어집 CSV (열: en,ko)")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--profile", help="사용할 모델 프로필 (config.yaml의 profiles 이름, 기본은 active_profile)")
    ap.add_argument("--out-dir")
    ap.add_argument("--pages", help="예: 1-5,8 (일부만 번역해서 시험할 때)")
    ap.add_argument("--no-review", action="store_true", help="LLM 검수 생략 (비용 절약용 시험)")
    ap.add_argument("--extract-only", action="store_true", help="추출 결과만 출력하고 종료 (API 불필요)")
    ap.add_argument("--dry-run", action="store_true", help="API 없이 전체 흐름을 가짜 번역으로 실행")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if args.no_review:
        cfg["pipeline"]["llm_review"] = False

    if args.extract_only:
        print(f"PDF 읽는 중: {args.pdf}")
        segments, warnings = extract_pdf(args.pdf, cfg, llm=None, pages=args.pages, direction=args.direction)
        for w in warnings:
            print("  ! " + w)
        print(f"추출 완료: 문단 {sum(s.kind == 'text' for s in segments)}, 표 {sum(s.kind == 'table' for s in segments)}, "
              f"원문 유지 {sum(s.kind == 'keep' for s in segments)}")
        for s in segments:
            body = s.text if s.kind != "table" else f"(표 {len(s.rows)}행)"
            print(f"  [{s.page}쪽/{s.kind}] {body[:110]}")
        return 0

    stem = os.path.splitext(os.path.basename(args.pdf))[0]
    out_dir = args.out_dir or os.path.join("output", stem)
    try:
        res = translate_document(
            args.pdf, cfg, profile=args.profile, direction=args.direction, domain=args.domain,
            style=args.style, glossary=load_glossary(args.glossary, args.direction), pages=args.pages,
            out_dir=out_dir, dry_run=args.dry_run, progress=print)
    except RunnerError as exc:
        print(f"오류: {exc}")
        return 2

    for w in res.warnings:
        print("  ! " + w)
    s = res.stats
    print()
    print(f"완료 ({res.elapsed}) → {out_dir}")
    print(f"  품질 점수 {s['score']} / 확인 필요 {s['flagged']}개 / 자동 재번역 {s['retried']}개")
    print(f"  호출 {s['calls']}회, 토큰 입력 {s['tokens_in']:,} · 출력 {s['tokens_out']:,}")
    if s["cost_usd"] is not None:
        print(f"  예상 비용 ${s['cost_usd']:.3f}" + (f" (약 {s['cost_krw']:,.0f}원)" if s["cost_krw"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
