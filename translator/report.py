"""검수 보고서(HTML) + CSV, 그리고 통계·점수 계산."""
from __future__ import annotations

import csv
import html
from datetime import datetime

from .checks import CHECK_LABELS_KO
from .pipeline import PipelineResult


def compute_stats(result: PipelineResult, cfg: dict, usage=None) -> dict:
    units = [u for u in result.units if not u.copied and u.src]
    total = len(units)

    per_check = []
    for name in result.enabled:
        first = sum(1 for u in units if any(v.check == name for v in u.initial))
        final = sum(1 for u in units if any(v.check == name for v in u.final))
        per_check.append({
            "name": name,
            "label": CHECK_LABELS_KO.get(name, name),
            "first_fail": first,
            "final_fail": final,
            "first_rate": 100.0 * (1 - first / total) if total else 100.0,
            "final_rate": 100.0 * (1 - final / total) if total else 100.0,
        })

    retried = [u for u in units if u.retries > 0]
    retried_ok = [u for u in retried if not u.final]
    unresolved_code = [u for u in units if u.final]
    review_fixed = [u for u in units if u.review_fixed]
    review_unresolved = [u for u in units if u.review_unresolved]
    flagged = [u for u in units if u.flagged]

    penalty = float(cfg.get("score", {}).get("retry_penalty", 0.3))
    if total:
        score = 100.0 * (1 - (len(flagged) + penalty * len(retried_ok)) / total)
        score = max(0.0, min(100.0, score))
    else:
        score = 100.0

    gl_a = sum(u.gl_applicable for u in units)
    gl_b = sum(u.gl_applied for u in units)

    stats = {
        "total": total,
        "per_check": per_check,
        "retried": len(retried),
        "retried_resolved": len(retried_ok),
        "unresolved_code": len(unresolved_code),
        "review_fixed": len(review_fixed),
        "review_unresolved": len(review_unresolved),
        "flagged": len(flagged),
        "score": round(score, 1),
        "glossary_applicable": gl_a,
        "glossary_applied": gl_b,
        "glossary_rate": (100.0 * gl_b / gl_a) if gl_a else None,
        "tokens_in": usage.total_input if usage else 0,
        "tokens_out": usage.total_output if usage else 0,
        "calls": usage.total_calls if usage else 0,
        "cost_usd": None,
        "cost_krw": None,
    }
    pr = cfg.get("pricing", {})
    if usage and pr.get("input_per_million_usd") is not None and pr.get("output_per_million_usd") is not None:
        usd = (usage.total_input * pr["input_per_million_usd"] + usage.total_output * pr["output_per_million_usd"]) / 1e6
        stats["cost_usd"] = usd
        if pr.get("usd_to_krw"):
            stats["cost_krw"] = usd * pr["usd_to_krw"]
    return stats


_CSS = """
body{font-family:'Malgun Gothic','Apple SD Gothic Neo',system-ui,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#1d2330;line-height:1.55}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 10px}
.sub{color:#667085;font-size:13px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-top:16px}
.card{background:#fff;border-radius:10px;padding:14px 18px;min-width:130px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.card b{display:block;font-size:26px} .card span{font-size:12px;color:#667085}
table{border-collapse:collapse;width:100%;background:#fff;border-radius:10px;overflow:hidden;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
th{background:#eef1f6;font-size:12px;color:#475467}
.bad{background:#fff8c5}
.ok{color:#067647}.warn{color:#b54708}
.pair td{width:50%} .src{color:#475467} .note{font-size:12px;color:#b54708;margin-top:4px}
.kv td:first-child{width:160px;color:#667085}
"""


def _e(x) -> str:
    return html.escape(str(x))


def write_html_report(result: PipelineResult, stats: dict, path: str, title: str, meta: dict) -> None:
    rows_checks = "".join(
        f"<tr><td>{_e(c['label'])}</td><td>{c['first_rate']:.1f}%</td><td>{c['final_rate']:.1f}%</td>"
        f"<td>{c['first_fail']}</td><td>{c['final_fail']}</td></tr>" for c in stats["per_check"])

    gl = "해당 없음(용어집 없음 또는 적용 대상 용어 없음)"
    if stats["glossary_rate"] is not None:
        gl = f"{stats['glossary_rate']:.1f}% ({stats['glossary_applied']}/{stats['glossary_applicable']})"

    cost = "요금 미설정 (config.yaml의 pricing에 단가를 넣으면 계산됩니다)"
    if stats["cost_usd"] is not None:
        cost = f"약 ${stats['cost_usd']:.3f}"
        if stats["cost_krw"] is not None:
            cost += f" (약 {stats['cost_krw']:,.0f}원)"

    analysis = result.analysis or {}
    terms = list(result.user_glossary) + list(result.auto_terms)
    terms_html = "".join(
        f"<tr><td>{_e(s)}</td><td>{_e(t)}</td><td>{'용어집' if i < len(result.user_glossary) else '자동 추출'}</td></tr>"
        for i, (s, t) in enumerate(terms)) or "<tr><td colspan=3>없음</td></tr>"

    flagged_units = [u for u in result.units if u.flagged]
    viol_rows = ""
    for u in flagged_units:
        msgs = "<br>".join(_e(v) for v in u.final)
        if u.review_unresolved and u.review_issue:
            msgs += ("<br>" if msgs else "") + "[LLM 검수] " + _e(u.review_issue)
        viol_rows += (f"<tr><td>{u.page}쪽</td><td class='src'>{_e(u.src[:300])}</td>"
                      f"<td>{_e(u.tgt[:300])}</td><td>{msgs}</td></tr>")
    if not viol_rows:
        viol_rows = "<tr><td colspan=4 class='ok'>남은 위반 없음</td></tr>"

    pairs = ""
    for item in result.rendered():
        cls = " class='bad'" if item["flagged"] else ""
        if item["kind"] == "table":
            src = "<br>".join(" | ".join(_e(c) for c in row) for row in item["src"])
            tgt = "<br>".join(" | ".join(_e(c) for c in row) for row in item["tgt"])
        else:
            src, tgt = _e(item["src"]), _e(item["tgt"])
        note = f"<div class='note'>{'<br>'.join(_e(n) for n in item['notes'])}</div>" if item["notes"] else ""
        pairs += f"<tr class='pair'{cls}><td class='src'>{src}</td><td>{tgt}{note}</td></tr>"

    doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)} 검수 보고서</title>
<style>{_CSS}</style></head><body>
<h1>{_e(title)} · 번역 검수 보고서</h1>
<div class="sub">{_e(meta.get('direction',''))} · 문체 {_e(meta.get('style',''))} · 모델 {_e(meta.get('model',''))} · {datetime.now():%Y-%m-%d %H:%M}</div>
<div class="cards">
 <div class="card"><b>{stats['score']}</b><span>품질 점수 (100점 만점)</span></div>
 <div class="card"><b>{stats['total']}</b><span>검사한 문단·셀</span></div>
 <div class="card"><b class="{'warn' if stats['flagged'] else 'ok'}">{stats['flagged']}</b><span>사람 확인 필요</span></div>
 <div class="card"><b>{stats['retried']}</b><span>자동 재번역 ({stats['retried_resolved']}개 해결)</span></div>
 <div class="card"><b>{stats['review_fixed']}</b><span>LLM 검수로 수정</span></div>
</div>

<h2>검사 항목별 통과율</h2>
<table><tr><th>항목</th><th>1차 통과율</th><th>최종 통과율</th><th>1차 위반</th><th>최종 위반</th></tr>{rows_checks}</table>
<div class="sub" style="margin-top:6px">점수 = 100 × (1 − (남은 위반 문단 + {0.3:g}×재번역으로 해결된 문단) ÷ 전체). 계수는 config.yaml의 score.retry_penalty.</div>

<h2>용어집 적용률</h2><div>{gl}</div>

<h2>사용량</h2>
<table class="kv">
<tr><td>API 호출</td><td>{stats['calls']}회</td></tr>
<tr><td>입력 토큰</td><td>{stats['tokens_in']:,}</td></tr>
<tr><td>출력 토큰</td><td>{stats['tokens_out']:,}</td></tr>
<tr><td>예상 비용</td><td>{cost}</td></tr>
<tr><td>소요 시간</td><td>{meta.get('elapsed','')}</td></tr>
</table>

<h2>문서 분석 결과</h2>
<table class="kv">
<tr><td>요약</td><td>{_e(analysis.get('summary',''))}</td></tr>
<tr><td>문서 유형 / 분야</td><td>{_e(analysis.get('doc_type',''))} / {_e(analysis.get('domain',''))}</td></tr>
<tr><td>권장 문체</td><td>{_e(analysis.get('tone',''))}</td></tr>
</table>
<h2>번역에 사용한 용어</h2>
<table><tr><th>원문</th><th>번역어</th><th>출처</th></tr>{terms_html}</table>

<h2>사람 확인이 필요한 문단 ({len(flagged_units)})</h2>
<table><tr><th>쪽</th><th>원문</th><th>번역문</th><th>문제</th></tr>{viol_rows}</table>

<h2>원문·번역문 비교 (노란색 = 확인 필요)</h2>
<table><tr><th>원문</th><th>번역문</th></tr>{pairs}</table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def write_csv_report(result: PipelineResult, path: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:  # 엑셀에서 한글이 깨지지 않게 BOM
        w = csv.writer(f)
        w.writerow(["쪽", "종류", "확인필요", "재번역횟수", "1차위반", "최종위반", "LLM검수", "원문", "번역문"])
        for u in result.units:
            if u.copied:
                continue
            w.writerow([
                u.page, u.kind, "Y" if u.flagged else "", u.retries,
                " / ".join(str(v) for v in u.initial),
                " / ".join(str(v) for v in u.final),
                ("수정됨: " if u.review_fixed else "") + u.review_issue,
                u.src, u.tgt,
            ])
