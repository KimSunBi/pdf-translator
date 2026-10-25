"""원문 Markdown: PDF에서 읽은 내용을 사람이 확인·수정할 수 있는 MD로 내보내고, 수정본을 다시 반영한다.

각 단락 앞의 `<!-- p:12 page:3 -->` 표시는 지우지 마세요. 표시 아래 글만 고치면 됩니다.
- p: 번역할 문단 (고치면 번역에 반영)
- k: 번역하지 않고 원문 유지하는 부분 (참고문헌, 수식 등) — 고쳐도 번역에는 영향 없음
- t: 표 (칸 글자를 고칠 수 있음, 줄·칸 개수는 그대로 두세요)
"""
from __future__ import annotations

import re

from .extract import Segment

_MARK = re.compile(r"^<!--\s*([pkt]):(\d+)(?:\s+page:\d+)?\s*-->\s*$", re.M)


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _split_row(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [p.replace("\\|", "|").strip() for p in parts]


def segments_to_md(segments: list[Segment], title: str = "") -> str:
    out = []
    if title:
        out.append(f"<!-- 원문: {title} — 표시(<!-- … -->)는 지우지 말고, 아래 글만 고치세요 -->\n")
    for seg in segments:
        if seg.kind == "table":
            rows = seg.rows or []
            if not rows:
                continue
            out.append(f"<!-- t:{seg.id} page:{seg.page} -->")
            width = max(len(r) for r in rows)
            for k, row in enumerate(rows):
                cells = [_cell(c) for c in row] + [""] * (width - len(row))
                out.append("| " + " | ".join(cells) + " |")
                if k == 0:
                    out.append("|" + " --- |" * width)
            out.append("")
        else:
            tag = "k" if seg.kind == "keep" else "p"
            out.append(f"<!-- {tag}:{seg.id} page:{seg.page} -->")
            out.append(seg.text.strip())
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def apply_md(segments: list[Segment], md: str) -> tuple[int, list[str]]:
    """수정된 MD를 세그먼트에 반영한다. (바뀐 곳 수, 경고)."""
    by_id = {s.id: s for s in segments}
    marks = list(_MARK.finditer(md))
    changed, warnings = 0, []
    for k, m in enumerate(marks):
        kind, sid = m.group(1), int(m.group(2))
        body = md[m.end(): marks[k + 1].start() if k + 1 < len(marks) else len(md)].strip()
        seg = by_id.get(sid)
        if seg is None:
            warnings.append(f"표시 {kind}:{sid}에 해당하는 단락이 없어 건너뜀")
            continue
        if kind == "p" and seg.kind == "text":
            new = re.sub(r"\s+", " ", re.sub(r"^<!--.*?-->\s*", "", body, flags=re.S)).strip()
            if new and new != seg.text:
                seg.text, changed = new, changed + 1
        elif kind == "t" and seg.kind == "table" and seg.rows:
            lines = [ln for ln in body.splitlines() if ln.strip().startswith("|")]
            lines = [ln for ln in lines if not re.fullmatch(r"\s*\|(\s*:?-{3,}:?\s*\|)+\s*", ln)]
            new_rows = [_split_row(ln) for ln in lines]
            shape_ok = len(new_rows) == len(seg.rows) and all(len(a) >= len(b) for a, b in zip(new_rows, seg.rows))
            if not shape_ok:
                warnings.append(f"표 {sid}: 줄·칸 개수가 달라 수정을 반영하지 않음")
                continue
            rebuilt = [[nr[c] for c in range(len(orig))] for nr, orig in zip(new_rows, seg.rows)]
            if rebuilt != seg.rows:
                seg.rows, changed = rebuilt, changed + 1
    return changed, warnings
