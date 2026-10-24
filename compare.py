#!/usr/bin/env python3
"""같은 PDF를 여러 모델로 번역해서 한 페이지에서 나란히 비교한다 (명령줄 버전). 웹 앱은 app.py.

  python compare.py my.pdf --pages 1-3                       # 키가 설정된 모든 프로필 비교
  python compare.py my.pdf --pages 1-3 --profiles gemini-3.1-pro,claude-sonnet
  python compare.py my.pdf --pages 1-3 --deepl-text deepl.txt  # DeepL 결과(txt)도 같이 놓기
  python compare.py my.pdf --dry-run                          # API 없이 흐름만 확인
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from translator.runner import RunnerError, compare_documents, load_glossary   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="여러 모델 번역 비교")
    ap.add_argument("pdf")
    ap.add_argument("--profiles", help="쉼표로 구분한 프로필 이름 (기본: 키가 설정된 모든 프로필)")
    ap.add_argument("--direction", choices=["en2ko", "ko2en"], default="en2ko")
    ap.add_argument("--domain", default="")
    ap.add_argument("--style", choices=["formal", "plain"], default="formal")
    ap.add_argument("--glossary")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--out-dir")
    ap.add_argument("--pages", help="예: 1-3 (비교는 일부 쪽으로 먼저 하세요. 모델 수만큼 비용이 듭니다)")
    ap.add_argument("--no-review", action="store_true", help="LLM 검수 생략 (비용 절약)")
    ap.add_argument("--deepl-text", help="DeepL 번역 결과를 붙여넣어 저장한 txt (문단 사이 빈 줄)")
    ap.add_argument("--dry-run", action="store_true", help="API 없이 가짜 번역으로 흐름만 확인")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if args.no_review:
        cfg["pipeline"]["llm_review"] = False

    stem = os.path.splitext(os.path.basename(args.pdf))[0]
    out_dir = args.out_dir or os.path.join("output", f"{stem}-compare")
    deepl_text = None
    if args.deepl_text:
        with open(args.deepl_text, encoding="utf-8") as f:
            deepl_text = f.read()
    try:
        res = compare_documents(
            args.pdf, cfg, profiles=[n.strip() for n in args.profiles.split(",")] if args.profiles else None,
            direction=args.direction, domain=args.domain, style=args.style,
            glossary=load_glossary(args.glossary, args.direction), pages=args.pages,
            out_dir=out_dir, dry_run=args.dry_run, deepl_text=deepl_text, progress=print)
    except RunnerError as exc:
        print(f"오류: {exc}")
        return 2

    for w in res.warnings:
        print("  ! " + w)
    print(f"\n비교 페이지: {res.compare_html}")
    print("브라우저로 열어 모델 이름이 가려진 상태로 읽고, 문단마다 가장 좋은 번역에 투표하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
