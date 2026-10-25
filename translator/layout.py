"""원본 PDF 위에 번역문을 얹어 '원래 모양' 그대로의 번역 PDF를 만든다.

방식: 번역한 문단의 원래 위치(상자)에서 원문 글자만 지우고(그림·선·배경은 그대로),
같은 상자 안에 번역문을 넣는다. 한국어가 상자보다 길면 글자 크기를 줄여 맞춘다.
스캔본(글자가 없는 PDF)과 이미지로 읽은 페이지는 위치 정보가 없어 지원하지 않는다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(HERE, "fonts")
HCR_NAMES = ["HCRBatang.ttf", "HCR Batang.ttf", "HamchoromBatang.ttf", "HCRBatang-Regular.ttf", "함초롬바탕.ttf"]
SYSTEM_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc",
]
FLAG_BG = (1.0, 0.96, 0.6)          # 확인이 필요한 문단 배경 (Word 출력의 노란색과 같은 의미)
LINE_HEIGHT = 1.2


@dataclass
class LayoutReport:
    font_label: str = ""
    placed: int = 0
    shrunk: int = 0
    overflow: list[str] = field(default_factory=list)       # 칸에 못 맞춰 크기를 많이 줄인 곳
    skipped: int = 0                                         # 위치 정보가 없어 건너뛴 문단


def find_font() -> tuple[str | None, str]:
    """(글꼴 파일, 표시 이름). 함초롬바탕이 fonts/에 있으면 그것을 쓰고, 없으면 명조 계열로 대신한다."""
    for name in HCR_NAMES:
        p = os.path.join(FONT_DIR, name)
        if os.path.exists(p):
            return p, "함초롬바탕"
    if os.path.isdir(FONT_DIR):                                    # 이름이 조금 달라도 HCR/함초롬 이름이면 인정
        for fn in sorted(os.listdir(FONT_DIR)):
            if fn.lower().endswith((".ttf", ".otf")) and any(k in fn.lower() for k in ("hcr", "hamchorom", "함초롬")):
                return os.path.join(FONT_DIR, fn), "함초롬바탕"
    for p in SYSTEM_FONTS:
        if os.path.exists(p):
            return p, "Noto Serif CJK KR (함초롬바탕 대신)"
    if shutil.which("fc-match"):
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", "Noto Serif CJK KR"], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
            if out and os.path.exists(out):
                return out, "Noto Serif CJK KR (함초롬바탕 대신)"
        except Exception:
            pass
    return None, ""


def subset_font(font_path: str, text: str, out_dir: str) -> str:
    """쓰는 글자만 남긴 글꼴 파일을 만든다. 한글 글꼴은 전체가 수 MB~수십 MB라 그대로 넣으면 PDF가 너무 커진다."""
    import logging

    from fontTools import subset
    from fontTools.ttLib import TTCollection, TTFont

    logging.getLogger("fontTools").setLevel(logging.ERROR)
    number = 0
    if font_path.lower().endswith(".ttc"):
        coll = TTCollection(font_path)
        for k, f in enumerate(coll.fonts):
            names = " ".join(str(n) for n in f["name"].names if n.nameID in (1, 4))
            if " KR" in names or "Korean" in names:
                number = k
                break
    font = TTFont(font_path, fontNumber=number, lazy=True)
    chars = set(text) | {chr(c) for c in range(32, 127)}
    opts = subset.Options()
    opts.layout_features = ["*"]
    opts.notdef_outline = True
    opts.glyph_names = False
    opts.name_IDs = ["*"]
    sub = subset.Subsetter(opts)
    sub.populate(text="".join(sorted(chars)))
    sub.subset(font)
    out = os.path.join(out_dir, "subset-font.ttf")
    font.flavor = None
    font.save(out)
    return out


def _bg_color(page, rect) -> tuple[float, float, float]:
    """상자 가장자리에서 배경색을 추정한다 (글자가 아닌 가장 흔한 색)."""
    try:
        pix = page.get_pixmap(clip=rect, dpi=36, alpha=False)
        w, h = pix.width, pix.height
        if w < 2 or h < 2:
            return (1, 1, 1)
        pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
        colors = [tuple(pix.pixel(x, y)[:3]) for x, y in pts]
        r, g, b = Counter(colors).most_common(1)[0][0]
        return (r / 255, g / 255, b / 255)
    except Exception:
        return (1, 1, 1)


def _is_dark(rgb) -> bool:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] < 0.4


def split_by_weights(text: str, weights: list[float]) -> list[str]:
    """번역문을 상자 크기 비율대로 여러 상자(쪽을 넘어 이어진 문단 등)에 나눈다. 단어 경계에서 자른다."""
    if len(weights) <= 1:
        return [text]
    words = text.split(" ")
    total_chars = sum(len(w) + 1 for w in words)
    total_w = sum(weights) or 1.0
    parts: list[list[str]] = [[] for _ in weights]
    acc, k, bound = 0, 0, weights[0] / total_w * total_chars
    for w in words:
        if acc >= bound and k < len(weights) - 1:
            k += 1
            bound += weights[k] / total_w * total_chars
        parts[k].append(w)
        acc += len(w) + 1
    return [" ".join(p) for p in parts]


def _fit_text(page, rect, text, start_size, min_size, color, fontname):
    """상자에 들어가는 가장 큰 글자 크기로 넣는다. (사용한 크기, 맞췄는지)."""
    size = start_size
    while size >= min_size - 1e-6:
        rc = page.insert_textbox(rect, text, fontname=fontname, fontsize=size, color=color,
                                 lineheight=LINE_HEIGHT, align=pymupdf.TEXT_ALIGN_LEFT)
        if rc >= 0:
            return size, True
        size = round(size - 0.5, 2)
    return None, False


def build_layout_pdf(src_pdf: str, rendered: list[dict], out_path: str, *, font_path: str | None = None,
                     min_scale: float = 0.6, progress=lambda m: None) -> LayoutReport:
    """rendered: PipelineResult.rendered() 결과."""
    rep = LayoutReport()
    if font_path is None:
        font_path, rep.font_label = find_font()
    else:
        rep.font_label = os.path.basename(font_path)
    if not font_path:
        raise RuntimeError("한글 글꼴을 찾지 못했습니다. fonts/ 폴더에 함초롬바탕(HCRBatang.ttf)을 넣어주세요.")

    # 1) 쪽별로 '지울 상자 + 넣을 글'을 모은다
    tasks: dict[int, list[dict]] = {}

    def add(page_idx, rect, text, size, flagged):
        tasks.setdefault(page_idx, []).append({"rect": pymupdf.Rect(rect), "text": text, "size": size, "flag": flagged})

    for item in rendered:
        seg = item["seg"]
        if item["kind"] == "text" and item["tgt"] and item["tgt"] != item["src"]:
            if not seg.boxes:
                rep.skipped += 1
                continue
            weights = [max(1.0, (b[3] - b[1]) * (b[4] - b[2])) for b in seg.boxes]
            for b, part in zip(seg.boxes, split_by_weights(item["tgt"], weights)):
                if part.strip():
                    add(b[0], (b[1], b[2], b[3], b[4]), part, float(b[5]), item["flagged"])
        elif item["kind"] == "table" and seg.cells:
            for r, row in enumerate(item["tgt"]):
                for c, cell in enumerate(row):
                    box = seg.cells[r][c] if r < len(seg.cells) and c < len(seg.cells[r]) else None
                    src_cell = (item["src"][r][c] if item["src"] else "")
                    if box and cell and cell != src_cell:
                        h = box[3] - box[1]
                        add(seg.page - 1, box, cell, min(11.0, max(6.0, h * 0.55)), item["flagged"])
        elif item["kind"] == "table" and not seg.cells:
            rep.skipped += 1

    # 2) 쓰는 글자만 남긴 글꼴을 만들고, 쪽마다 원문 지우기 → 번역문 넣기
    import tempfile
    tmp_font_dir = tempfile.mkdtemp(prefix="hcrfont_")
    try:
        used = "".join(t["text"] for lst in tasks.values() for t in lst)
        font_path = subset_font(font_path, used, tmp_font_dir)
    except Exception as exc:
        progress(f"글꼴 축소 실패({exc.__class__.__name__}) — 글꼴 전체를 넣습니다 (파일이 커집니다)")
    doc = pymupdf.open(src_pdf)
    for page_idx in sorted(tasks):
        page = doc[page_idx]
        lst = tasks[page_idx]
        bgs = []
        for t in lst:
            bg = FLAG_BG if t["flag"] else _bg_color(page, t["rect"])
            bgs.append(bg)
            page.add_redact_annot(t["rect"], fill=bg)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        fontname = f"kfont{page_idx}"
        page.insert_font(fontname=fontname, fontfile=font_path)
        page_limit = page.rect.y1 - 18
        for t, bg in zip(lst, bgs):
            color = (1, 1, 1) if _is_dark(bg) else (0, 0, 0)
            start = max(5.0, t["size"])
            minimum = max(4.5, start * min_scale)
            r = t["rect"]                       # 원래 상자는 글자 줄 높이에 딱 맞아서, 반 줄 정도 여유를 준다
            r = pymupdf.Rect(r.x0, r.y0, r.x1, min(page_limit, r.y1 + start * 0.35))
            t["rect"] = r
            size, ok = _fit_text(page, r, t["text"], start, minimum, color, fontname)
            if not ok:   # 아래로 조금 늘려서 다시 시도 (쪽 아래 여백까지만)
                ext = pymupdf.Rect(t["rect"].x0, t["rect"].y0, t["rect"].x1,
                                   min(page_limit, t["rect"].y1 + max(12.0, t["rect"].height * 0.6)))
                size, ok = _fit_text(page, ext, t["text"], minimum, 3.0, color, fontname)
                rep.overflow.append(f"{page_idx + 1}쪽: \"{t['text'][:24]}…\" 칸이 모자라 글자를 많이 줄였습니다"
                                    if ok else f"{page_idx + 1}쪽: \"{t['text'][:24]}…\" 칸에 넣지 못했습니다")
                if not ok:
                    continue
            elif size < start - 0.4:
                rep.shrunk += 1
            rep.placed += 1
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()
    shutil.rmtree(tmp_font_dir, ignore_errors=True)
    progress(f"원본 모양 PDF: {rep.placed}곳 배치, 글자 크기 줄인 곳 {rep.shrunk}, 위치 정보 없어 건너뜀 {rep.skipped}")
    return rep
