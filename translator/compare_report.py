"""여러 모델의 번역을 한 페이지에서 나란히 비교하는 HTML.

- 원문 | 모델별 번역 | DeepL(붙여넣기) 열
- 모델 이름 가리기(블라인드) 기본 ON: 이름을 모르고 읽어야 편견 없이 비교할 수 있다
- 문단마다 "이게 제일 좋음" 투표 → 위쪽에서 모델별 득표수 집계
"""
from __future__ import annotations

import html
import json
import random
from dataclasses import dataclass
from datetime import datetime

from .pipeline import PipelineResult


@dataclass
class ProfileRun:
    name: str                 # config.yaml의 프로필 이름
    model: str
    result: PipelineResult
    stats: dict
    elapsed: str


def _e(x) -> str:
    return html.escape(str(x))


def _cell_text(value) -> str:
    if isinstance(value, list):                       # 표
        return "\n".join(" | ".join(str(c) for c in row) for row in value)
    return str(value)


def _fmt_cost(stats: dict) -> str:
    if stats.get("cost_usd") is None:
        return "요금 미설정"
    s = f"${stats['cost_usd']:.3f}"
    if stats.get("cost_krw"):
        s += f" (약 {stats['cost_krw']:,.0f}원)"
    return s


_CSS = """
body{font-family:'Malgun Gothic','Apple SD Gothic Neo',system-ui,sans-serif;margin:0;padding:20px;background:#f6f7f9;color:#1d2330;line-height:1.55}
h1{font-size:21px;margin:0 0 4px} h2{font-size:16px;margin:24px 0 8px} .sub{color:#667085;font-size:13px}
table{border-collapse:collapse;background:#fff;font-size:14px}
.summary{width:100%;border-radius:10px;overflow:hidden}
.wrap{overflow-x:auto;margin-top:8px}
.cmp{min-width:900px;width:100%}
th,td{padding:8px 10px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
th{background:#eef1f6;font-size:12px;color:#475467;position:sticky;top:0}
td.src{color:#475467;white-space:pre-wrap} td.tr{white-space:pre-wrap}
td.bad{background:#fff8c5} .note{font-size:12px;color:#b54708;margin-top:4px}
textarea{width:100%;min-height:90px;box-sizing:border-box;font:inherit;border:1px solid #d0d5dd;border-radius:6px;padding:6px}
button{font:inherit;font-size:12px;border:1px solid #d0d5dd;background:#fff;border-radius:14px;padding:3px 10px;cursor:pointer;margin-top:6px}
button.on{background:#1d4ed8;color:#fff;border-color:#1d4ed8}
.bar{position:sticky;top:0;z-index:5;background:#fffbe6;border:1px solid #fde68a;border-radius:10px;padding:10px 14px;margin:14px 0;display:flex;flex-wrap:wrap;gap:14px;align-items:center}
.bar b{font-size:15px}
details{margin-top:18px} pre{white-space:pre-wrap;background:#fff;padding:10px;border-radius:8px}
"""

_JS = """
const COLS = __COLS__;            // [{key, blind, real}]
const KEY = 'cmp-votes-' + __TITLE__;
let blind = true, votes = {};
try { votes = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
function save(){ try { localStorage.setItem(KEY, JSON.stringify(votes)); } catch (e) {} }
function label(c){ return blind ? c.blind : c.real; }
function paint(){
  document.querySelectorAll('[data-head]').forEach(h => { const c = COLS.find(x => x.key === h.dataset.head); h.textContent = label(c); });
  document.querySelectorAll('button[data-row]').forEach(b => { b.classList.toggle('on', votes[b.dataset.row] === b.dataset.col); });
  const n = {}; COLS.forEach(c => n[c.key] = 0);
  Object.values(votes).forEach(k => { if (k in n) n[k]++; });
  document.getElementById('tally').innerHTML = COLS.map(c => label(c) + ' <b>' + n[c.key] + '</b>').join(' &nbsp;·&nbsp; ');
  document.getElementById('toggle').textContent = blind ? '모델 이름 공개하기' : '모델 이름 다시 가리기';
}
document.addEventListener('click', ev => {
  const b = ev.target.closest('button[data-row]');
  if (b) { votes[b.dataset.row] = (votes[b.dataset.row] === b.dataset.col) ? undefined : b.dataset.col; if (votes[b.dataset.row] === undefined) delete votes[b.dataset.row]; save(); paint(); }
});
document.getElementById('toggle').onclick = () => { blind = !blind; paint(); };
document.getElementById('reset').onclick = () => { if (confirm('투표를 모두 지울까요?')) { votes = {}; save(); paint(); } };
paint();
"""


def write_compare_html(path: str, title: str, runs: list[ProfileRun], meta: dict,
                       deepl_paras: list[str] | None = None, deepl_full: str | None = None) -> None:
    # 열 순서를 섞어 이름을 가린다 (실행마다 같은 순서가 되도록 seed 고정)
    order = list(range(len(runs)))
    random.Random(len(runs) * 7919 + len(title)).shuffle(order)
    letters = "ABCDEFGHIJ"
    blind_name = {runs[i].name: f"번역 {letters[k]}" for k, i in enumerate(order)}
    shown = [runs[i] for i in order]
    cols = [{"key": r.name, "blind": blind_name[r.name], "real": f"{r.name} ({r.model})"} for r in shown]
    cols.append({"key": "deepl", "blind": "DeepL", "real": "DeepL"})

    # ---- 요약표 (이름은 공개: 점수·비용은 결과를 읽은 뒤 확인용)
    srows = ""
    for r in runs:
        s = r.stats
        srows += (f"<tr><td>{_e(r.name)}</td><td>{_e(r.model)}</td><td><b>{s['score']}</b></td>"
                  f"<td>{s['flagged']}</td><td>{s['retried']}</td><td>{s['review_fixed']}</td>"
                  f"<td>{s['tokens_in']:,} / {s['tokens_out']:,}</td><td>{_e(_fmt_cost(s))}</td>"
                  f"<td>{_e(r.elapsed)}</td></tr>")

    # ---- 행 만들기
    rendered = [r.result.rendered() for r in shown]
    n_rows = len(rendered[0]) if rendered else 0
    deepl_iter = iter(deepl_paras or [])
    body = ""
    for i in range(n_rows):
        first = rendered[0][i]
        if first["kind"] == "keep":
            continue
        row_id = str(i)
        deepl_text = next(deepl_iter, "") if (deepl_paras is not None and first["kind"] == "text") else ""
        tds = ""
        for ri, r in enumerate(shown):
            item = rendered[ri][i]
            cls = "tr bad" if item["flagged"] else "tr"
            note = f"<div class='note'>{'<br>'.join(_e(n) for n in item['notes'])}</div>" if item["notes"] else ""
            tds += (f"<td class='{cls}'>{_e(_cell_text(item['tgt']))}{note}<br>"
                    f"<button data-row='{row_id}' data-col='{_e(r.name)}'>이게 제일 좋음</button></td>")
        tds += (f"<td><textarea placeholder='DeepL 번역을 붙여넣기'>{_e(deepl_text)}</textarea><br>"
                f"<button data-row='{row_id}' data-col='deepl'>이게 제일 좋음</button></td>")
        body += f"<tr><td class='src'>{_e(first['page'])}쪽<br>{_e(_cell_text(first['src']))}</td>{tds}</tr>"

    head = "<th>원문</th>" + "".join(f"<th data-head='{_e(c['key'])}'></th>" for c in cols)
    full_panel = ""
    if deepl_full:
        full_panel = (f"<details><summary>DeepL 전체 번역문 (문단 수가 맞지 않아 자동으로 짝짓지 못했습니다)</summary>"
                      f"<pre>{_e(deepl_full)}</pre></details>")

    doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)} 모델 비교</title>
<style>{_CSS}</style></head><body>
<h1>{_e(title)} · 모델 비교</h1>
<div class="sub">{_e(meta.get('direction', ''))} · 문체 {_e(meta.get('style', ''))} · {datetime.now():%Y-%m-%d %H:%M} · 컴퓨터 화면에서 보는 것을 권장합니다</div>

<h2>자동 검수 요약</h2>
<table class="summary"><tr><th>프로필</th><th>모델</th><th>점수</th><th>확인 필요</th><th>재번역</th><th>LLM 검수 수정</th><th>토큰(입력/출력)</th><th>비용</th><th>시간</th></tr>{srows}</table>
<div class="sub" style="margin-top:6px">점수는 코드 검수 기준이라 번역의 자연스러움은 반영하지 못합니다. 아래에서 직접 읽고 투표하세요.</div>

<div class="bar"><span>투표 집계: <span id="tally"></span></span>
<button id="toggle"></button><button id="reset">투표 초기화</button></div>

<div class="wrap"><table class="cmp"><tr>{head}</tr>{body}</table></div>
{full_panel}
<script>{_JS.replace('__COLS__', json.dumps(cols, ensure_ascii=False)).replace('__TITLE__', json.dumps(title, ensure_ascii=False))}</script>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def split_deepl_text(text: str) -> list[str]:
    """DeepL 결과 텍스트를 빈 줄 기준 문단으로 나눈다."""
    return [p.strip().replace("\n", " ") for p in text.split("\n\n") if p.strip()]
