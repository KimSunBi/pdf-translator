"""PDF에서 번역 단위(Segment)를 뽑아낸다.

기본은 PyMuPDF 텍스트 추출이고, 표나 2단 편집이 애매한 페이지만 Gemini가
페이지 이미지를 읽어 구조화한다(config의 use_vision_for_complex_pages).

규칙
- 표: 셀 단위로 번역할 수 있게 rows로 보관
- 수식·코드·참고문헌: kind="keep" (번역하지 않고 원문 유지)
- 머리글·바닥글·쪽번호: 제거
- 2단 편집: 읽는 순서(왼쪽 단 → 오른쪽 단)로 복원
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

REF_HEADINGS = {
    "references", "reference", "bibliography", "works cited",
    "참고문헌", "참고 문헌", "참고자료", "참고 자료",
}
PAGE_NUM_RE = re.compile(r"^\s*(?:page\s*)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?\s*$", re.I)
_TERMINAL = re.compile(r"[.!?:;。…”\"')\]]$")


@dataclass
class Segment:
    id: int
    kind: str                      # text | table | keep
    page: int                      # 1부터 시작하는 쪽 번호
    text: str = ""
    rows: list[list[str]] | None = None
    # 원본 모양 PDF를 만들 때 쓰는 위치 정보: (0부터 시작하는 쪽 인덱스, x0, y0, x1, y1, 글자 크기)
    boxes: list = field(default_factory=list)
    cells: list | None = None          # 표 셀의 (x0, y0, x1, y1) — rows와 같은 모양, 없는 칸은 None


def parse_pages(spec: str | None, n_pages: int) -> list[int]:
    """'1-5,8' → [0,1,2,3,4,7] (0부터 시작하는 인덱스)."""
    if not spec:
        return list(range(n_pages))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(part)
        out.extend(i - 1 for i in range(lo, hi + 1) if 1 <= i <= n_pages)
    return sorted(set(out))


def _clean_block_text(raw: str) -> str:
    text = re.sub(r"-\n(?=[a-z])", "", raw)       # 줄바꿈 하이픈 결합
    text = text.replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _norm_margin(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())


def _block_font_sizes(page) -> dict[int, float]:
    """블록 번호 → 글자 수로 가중한 평균 글자 크기."""
    sizes: dict[int, float] = {}
    try:
        for db in page.get_text("dict")["blocks"]:
            if db.get("type") != 0:
                continue
            total = chars = 0.0
            for ln in db.get("lines", []):
                for sp in ln.get("spans", []):
                    n = len(sp.get("text", "").strip())
                    total += float(sp.get("size", 0)) * n
                    chars += n
            if chars:
                sizes[db["number"]] = total / chars
    except Exception:
        pass
    return sizes


def _text_blocks(page) -> list[tuple[float, float, float, float, str, float]]:
    """(x0, y0, x1, y1, 텍스트, 글자 크기)."""
    sizes = _block_font_sizes(page)
    out = []
    for b in page.get_text("blocks"):
        if b[6] != 0:          # 0 = 텍스트 블록
            continue
        text = _clean_block_text(b[4])
        if text:
            out.append((b[0], b[1], b[2], b[3], text, sizes.get(b[5], 10.0)))
    return out


def _table_cells(t, rows) -> list | None:
    try:
        cells = [[tuple(float(v) for v in c) if c else None for c in row.cells] for row in t.rows]
    except Exception:
        return None
    if len(cells) != len(rows) or any(len(c) != len(r) for c, r in zip(cells, rows)):
        return None
    return cells


def _order_blocks(blocks, width: float) -> tuple[list, bool]:
    """읽는 순서로 정렬. (정렬된 블록, 단 구분이 애매한지)."""
    mid_l, mid_r = width * 0.45, width * 0.55
    narrow = [b for b in blocks if (b[2] - b[0]) < width * 0.6]
    left = [b for b in narrow if b[2] <= mid_r]
    right = [b for b in narrow if b[0] >= mid_l]
    crossing = [b for b in narrow if b[0] < mid_l and b[2] > mid_r]
    two_col = len(left) >= 2 and len(right) >= 2
    ambiguous = two_col and bool(crossing)
    if not two_col:
        return sorted(blocks, key=lambda b: (round(b[1], 0), b[0])), False

    # 2단: 폭이 넓은 블록(제목 등)을 경계로 구간을 나눠 왼쪽 단 → 오른쪽 단 순서로 읽는다
    spanning = [b for b in blocks if (b[2] - b[0]) >= width * 0.6 or b in crossing]
    ordered: list = []
    by_y = sorted(blocks, key=lambda b: b[1])
    bucket: list = []

    def flush():
        lcol = sorted([b for b in bucket if (b[0] + b[2]) / 2 < width / 2], key=lambda b: b[1])
        rcol = sorted([b for b in bucket if (b[0] + b[2]) / 2 >= width / 2], key=lambda b: b[1])
        ordered.extend(lcol + rcol)
        bucket.clear()

    for b in by_y:
        if b in spanning:
            flush()
            ordered.append(b)
        else:
            bucket.append(b)
    flush()
    return ordered, ambiguous


def _in_bbox(block, bbox) -> bool:
    cx, cy = (block[0] + block[2]) / 2, (block[1] + block[3]) / 2
    return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]


def _find_tables(page):
    try:
        return list(page.find_tables().tables)
    except Exception:
        return []


def _render_png(page, dpi: int = 150) -> bytes:
    return page.get_pixmap(dpi=dpi).tobytes("png")


def _vision_page(llm, page, page_no: int, direction: str) -> list[tuple[str, str, list | None]]:
    from .prompts import vision_prompt

    system, prompt = vision_prompt(direction)
    raw = llm.generate("vision", prompt, system=system, images=[_render_png(page)],
                       json_mode=True, task="vision")
    data = json.loads(_strip_fence(raw))
    items = data.get("items", data) if isinstance(data, dict) else data
    out = []
    for it in items:
        kind = it.get("kind", "text")
        if kind not in ("text", "table", "keep"):
            kind = "text"
        if kind == "table":
            rows = [[str(c) for c in row] for row in (it.get("rows") or [])]
            if rows:
                out.append(("table", "", rows))
        else:
            text = re.sub(r"\s+", " ", str(it.get("text", ""))).strip()
            if text:
                out.append((kind, text, None))
    return out


def _strip_fence(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    return re.sub(r"\s*```$", "", t)


def extract_pdf(
    path: str,
    cfg: dict,
    llm=None,
    pages: str | None = None,
    direction: str = "en2ko",
    progress: Callable[[str], None] = print,
    with_layout: bool = False,
) -> tuple[list[Segment], list[str]]:
    """(세그먼트 목록, 경고 목록). with_layout이면 위치 정보가 필요하므로 이미지 읽기를 쓰지 않는다."""
    ecfg = cfg.get("extract", {})
    pcfg = cfg.get("pipeline", {})
    use_vision = bool(pcfg.get("use_vision_for_complex_pages", True)) and llm is not None and not with_layout
    margin = float(ecfg.get("header_footer_margin", 0.07))
    min_chars = int(ecfg.get("min_paragraph_chars", 2))

    doc = pymupdf.open(path)
    idxs = parse_pages(pages, len(doc))
    warnings: list[str] = []

    # 1) 머리글/바닥글 후보: 여러 쪽에 반복되는 위·아래 가장자리 텍스트와 쪽번호
    margin_counts: dict[str, int] = {}
    if ecfg.get("remove_headers_footers", True):
        for i in idxs:
            page = doc[i]
            h = page.rect.height
            seen = set()
            for b in _text_blocks(page):
                if b[3] < h * margin or b[1] > h * (1 - margin):
                    seen.add(_norm_margin(b[4]))
            for key in seen:
                margin_counts[key] = margin_counts.get(key, 0) + 1

    def is_header_footer(block, h: float) -> bool:
        if not ecfg.get("remove_headers_footers", True):
            return False
        in_margin = block[3] < h * margin or block[1] > h * (1 - margin)
        if not in_margin:
            return False
        if PAGE_NUM_RE.match(block[4]):
            return True
        return len(idxs) >= 2 and margin_counts.get(_norm_margin(block[4]), 0) >= 2

    # 2) 페이지별 추출
    raw: list[tuple[int, str, str, list | None, list | None]] = []   # (쪽, kind, text, rows, 위치 정보)
    for n, i in enumerate(idxs, 1):
        page = doc[i]
        w, h = page.rect.width, page.rect.height
        blocks = _text_blocks(page)
        tables = _find_tables(page)
        body = [b for b in blocks if not is_header_footer(b, h)]
        ordered, ambiguous = _order_blocks(body, w)

        if not blocks and not tables:
            warnings.append(f"{i + 1}쪽: 추출할 텍스트가 없음(스캔본일 수 있음). 건너뜀 — OCR은 3단계에서 지원")
            continue

        if use_vision and (tables or ambiguous):
            reason = "표" if tables else "2단 편집"
            progress(f"  {i + 1}쪽: {reason} 페이지 → 이미지로 읽는 중")
            try:
                for kind, text, rows in _vision_page(llm, page, i + 1, direction):
                    raw.append((i + 1, kind, text, rows, None))
                continue
            except Exception as exc:  # 실패하면 텍스트 추출로 대체
                warnings.append(f"{i + 1}쪽: 이미지 읽기 실패({exc.__class__.__name__}) → 텍스트 추출로 대체")

        # 텍스트 추출 경로 (표는 find_tables 결과를 셀 단위로 보관)
        table_boxes = [t.bbox for t in tables]
        items: list[tuple] = []
        for t in tables:
            try:
                rows = [[(c or "").replace("\n", " ").strip() for c in row] for row in t.extract()]
            except Exception:
                rows = []
            if rows:
                items.append((t.bbox[1], "table", "", rows, _table_cells(t, rows)))
        pos = {id(b): k for k, b in enumerate(ordered)}
        for b in ordered:
            if any(_in_bbox(b, tb) for tb in table_boxes):
                continue
            items.append((float(pos[id(b)]) - 1e6, "text", b[4], None, [(i, b[0], b[1], b[2], b[3], b[5])]))   # 읽는 순서 유지
        # 표는 같은 쪽에서 위치(y)에 가장 가까운 블록 뒤에 넣는다
        text_items = [it for it in items if it[1] == "text"]
        table_items = [it for it in items if it[1] == "table"]
        merged = list(text_items)
        for t_item in table_items:
            ty = t_item[0]
            insert_at = len(merged)
            for k, it in enumerate(merged):
                if it[1] != "text":
                    continue
                blk = ordered[int(it[0] + 1e6)]
                if blk[1] > ty:
                    insert_at = k
                    break
            merged.insert(insert_at, t_item)
        for _, kind, text, rows, extra in merged:
            raw.append((i + 1, kind, text, rows, extra))

    # 3) 쪽을 넘어 이어지는 문단 병합, 참고문헌 이하는 keep 처리
    segments: list[Segment] = []
    in_refs = False
    for page_no, kind, text, rows, extra in raw:
        if kind == "text" and ecfg.get("skip_references", True):
            if text.strip().rstrip(":").lower() in REF_HEADINGS:
                in_refs = True
                segments.append(Segment(0, "text", page_no, text, boxes=list(extra or [])))
                continue
        if in_refs and kind != "table":
            kind = "keep"
        if kind == "text":
            if len(text) < min_chars or PAGE_NUM_RE.match(text):
                continue
            prev = segments[-1] if segments else None
            if (prev and prev.kind == "text" and not _TERMINAL.search(prev.text)
                    and text[:1].islower()):
                prev.text = f"{prev.text} {text}"
                prev.boxes += list(extra or [])
                continue
        if kind == "table":
            segments.append(Segment(0, kind, page_no, text, rows, cells=extra))
        else:
            segments.append(Segment(0, kind, page_no, text, rows, boxes=list(extra or []) if kind == "text" else []))

    for k, seg in enumerate(segments):
        seg.id = k
    return segments, warnings
