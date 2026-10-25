"""PDF 번역기 웹 앱 (Streamlit).

실행:  streamlit run app.py
배포:  DEPLOY.md 참고 (Streamlit Community Cloud + Secrets)

- 비밀번호(APP_PASSWORD)가 설정돼 있지 않으면 잠긴 채로 시작합니다. (API 비용을 아무나 쓰지 못하게)
- API 키는 Secrets(또는 환경변수)에만 둡니다. 화면·코드·저장소에 넣지 마세요.
"""
from __future__ import annotations

import hmac
import os
import re
import shutil
import tempfile
import time

import streamlit as st
import yaml

from translator import jobs
from translator.runner import (RunnerError, compare_documents, count_selected_pages, parse_glossary,
                               ready_profiles, translate_document)

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET_ENV_KEYS = ["GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY"]
DIRECTIONS = {"영어 → 한국어": "en2ko", "한국어 → 영어": "ko2en"}

st.set_page_config(page_title="PDF 번역기", page_icon="📄", layout="wide")


# ------------------------------------------------------------------ 설정·비밀 값

def _secret(name: str) -> str | None:
    try:
        value = st.secrets.get(name)
    except Exception:          # secrets 파일이 없는 로컬 실행
        return None
    return str(value) if value else None


def bridge_secrets() -> None:
    """Secrets에 넣은 API 키를 환경변수로 옮겨 어댑터가 읽을 수 있게 한다."""
    for key in SECRET_ENV_KEYS:
        if not os.environ.get(key):
            value = _secret(key)
            if value:
                os.environ[key] = value


@st.cache_data(show_spinner=False)
def _load_cfg_text() -> str:
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        return f.read()


def load_cfg() -> dict:
    return yaml.safe_load(_load_cfg_text())     # 실행마다 새 사본 (검수 옵션을 바꿔도 원본이 오염되지 않음)


# ------------------------------------------------------------------ 잠금

def gate() -> bool:
    expected = _secret("APP_PASSWORD") or os.environ.get("APP_PASSWORD")
    if not expected:
        if os.environ.get("ALLOW_NO_PASSWORD") == "1":      # 내 컴퓨터에서만 쓸 때
            return True
        st.title("📄 PDF 번역기")
        st.error("비밀번호(APP_PASSWORD)가 설정되지 않아 잠겨 있습니다. "
                 "Streamlit Secrets에 APP_PASSWORD를 추가하세요. (자세한 방법은 DEPLOY.md)")
        return False
    if st.session_state.get("auth_ok"):
        return True
    st.title("📄 PDF 번역기")
    fails = st.session_state.get("auth_fails", 0)
    if fails >= 5:
        st.error("비밀번호를 여러 번 틀렸습니다. 페이지를 새로고침한 뒤 다시 시도하세요.")
        return False
    with st.form("login"):
        pw = st.text_input("비밀번호", type="password")
        submitted = st.form_submit_button("들어가기")
    if submitted:
        if hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.session_state["auth_fails"] = fails + 1
            time.sleep(1)
            st.error("비밀번호가 맞지 않습니다.")
    return False


# ------------------------------------------------------------------ 도우미

def _safe_stem(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"[^\w가-힣 .\-]", "_", stem).strip(" .") or "document"
    return stem[:80]


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _fmt_cost(stats: dict) -> str:
    if stats.get("cost_usd") is None:
        return "요금 미설정"
    s = f"${stats['cost_usd']:.3f}"
    if stats.get("cost_krw"):
        s += f" (약 {stats['cost_krw']:,.0f}원)"
    return s


def _show_html(html: str, height: int) -> None:
    """검수 보고서·비교 페이지를 화면 안에 표시한다. (components.html은 새 Streamlit에서 사라지므로 st.iframe 우선)"""
    if hasattr(st, "iframe"):
        st.iframe(html, height=height)
    else:
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=True)


def _validate_pages(pages: str) -> str | None:
    p = pages.strip()
    if p and not re.fullmatch(r"[0-9,\-\s]+", p):
        st.error("쪽 범위는 숫자, 쉼표, 하이픈만 쓸 수 있습니다. 예: 1-3,5")
        return None
    return p


# ------------------------------------------------------------------ 백그라운드 작업

def _job_id(kind: str) -> str | None:
    return st.session_state.get(f"job_{kind}") or st.query_params.get(f"job_{kind}")


def _current_job(kind: str):
    """이 화면의 작업. 새로고침·재접속해도 주소의 job 값으로 다시 찾는다."""
    jid = _job_id(kind)
    job = jobs.get_job(jid)
    if jid and job is None and not st.session_state.get(f"job_{kind}_gone"):
        st.session_state[f"job_{kind}_gone"] = True      # 서버가 다시 시작돼 작업이 사라진 경우
        st.warning("이전 작업을 찾을 수 없습니다 (서버가 다시 시작되면 진행 중이던 작업은 사라집니다). 다시 시작해 주세요.")
    return job


def _start(kind: str, work) -> None:
    job = jobs.start_job(kind, work)
    st.session_state[f"job_{kind}"] = job.id
    st.session_state.pop(f"job_{kind}_gone", None)
    st.query_params[f"job_{kind}"] = job.id
    st.rerun()


def _fmt_sec(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}분 {s}초" if m else f"{s}초"


@st.fragment(run_every=2)
def _monitor(kind: str, title: str) -> None:
    job = jobs.get_job(_job_id(kind))
    if job is None or job.status != "running":
        st.rerun()                      # 끝났으면 전체 화면을 다시 그려 결과를 보여준다
        return
    st.info(f"{title} ({_fmt_sec(job.elapsed)} 경과) — 다른 탭으로 가거나 새로고침해도 계속 진행됩니다.")
    st.code("\n".join(jobs.log_tail(job)) or "시작하는 중…", language=None)


def _show_job(kind: str, title: str):
    """진행 중이면 진행 상황을, 실패했으면 오류를 보여주고, 성공했으면 결과를 돌려준다."""
    job = _current_job(kind)
    if job is None:
        return None
    if job.status == "running":
        _monitor(kind, title)
        return None
    if job.status == "error":
        st.error(job.error)
        with st.expander("진행 기록"):
            st.code("\n".join(job.log[-60:]), language=None)
        return None
    return job.result


def _is_running(kind: str) -> bool:
    job = jobs.get_job(_job_id(kind))
    return job is not None and job.status == "running"


# ------------------------------------------------------------------ 본문

def main() -> None:
    bridge_secrets()
    cfg = load_cfg()
    web = cfg.get("web", {})
    max_pages = int(web.get("max_pages", 60))

    st.title("📄 PDF 번역기")
    st.caption("PDF를 올리면 번역하고, 코드 검수 + LLM 검수로 품질을 확인합니다. 결과는 Word로 내려받을 수 있습니다.")

    # ---- 공통 입력
    up = st.file_uploader("번역할 PDF", type=["pdf"], help="글자가 선택되는 PDF만 지원합니다 (스캔본은 아직 미지원).")
    c1, c2, c3 = st.columns(3)
    direction_label = c1.selectbox("번역 방향", list(DIRECTIONS))
    direction = DIRECTIONS[direction_label]
    style = c2.radio("문체 (한국어 번역 시)", ["formal", "plain"], horizontal=True, disabled=(direction != "en2ko"),
                     format_func=lambda x: "격식체 (~입니다)" if x == "formal" else "평어체 (~이다)")
    domain = c3.text_input("분야", placeholder="예: 교육, 법률, 논문")
    pages = st.text_input("쪽 범위 (비우면 전체)", placeholder="예: 1-3,5", help=f"한 번에 최대 {max_pages}쪽")
    gl_file = st.file_uploader("용어집 (선택, CSV: en,ko 두 열)", type=["csv"])
    with st.expander("고급 옵션"):
        llm_review = st.checkbox("LLM 검수 사용 (품질↑, 비용↑)", value=bool(cfg["pipeline"].get("llm_review", True)))
        dry_run = st.checkbox("테스트 모드 (가짜 번역, API 비용 없음) — 배포가 잘 됐는지 확인용", value=False)

    glossary: list[tuple[str, str]] = []
    if gl_file is not None:
        glossary = parse_glossary(gl_file.getvalue().decode("utf-8-sig", errors="replace"), direction)
        if glossary:
            st.caption(f"용어집 {len(glossary)}개 적용")
        else:
            st.warning("용어집에서 용어를 읽지 못했습니다. 첫 줄이 `en,ko` 열 이름인지 확인하세요.")

    pages_clean = _validate_pages(pages)
    if pages_clean is None:
        return
    cfg["pipeline"]["llm_review"] = llm_review

    # ---- 업로드 파일을 임시 저장하고 쪽 수 확인
    stem = _safe_stem(up.name) if up else ""
    if up is not None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "probe.pdf")
            with open(probe, "wb") as f:
                f.write(up.getvalue())
            try:
                n_sel, n_all = count_selected_pages(probe, pages_clean or None)
                st.caption(f"전체 {n_all}쪽 중 {n_sel}쪽을 처리합니다.")
                if n_sel > max_pages:
                    st.warning(f"한 번에 {max_pages}쪽까지만 처리합니다. 쪽 범위를 나눠 주세요. 예: 1-{max_pages}")
            except RunnerError as exc:
                st.error(str(exc))

    ready, skipped = ready_profiles(cfg)
    tab_tr, tab_cmp = st.tabs(["번역", "모델 비교"])

    # ================= 번역 탭
    with tab_tr:
        if not ready and not dry_run:
            st.info("사용할 수 있는 모델이 없습니다. Secrets에 API 키를 넣거나, 고급 옵션에서 테스트 모드를 켜세요.")
        profile = st.selectbox("모델", ready or ["(없음)"], disabled=not ready,
                               help="config.yaml의 profiles에 정의된 모델 중 키가 설정된 것만 보입니다.")
        run_tr = st.button("번역 시작", type="primary", key="run_tr",
                           disabled=(up is None or (not ready and not dry_run) or _is_running("tr")))
        if run_tr:
            data = up.getvalue()
            kw = dict(profile=None if dry_run else profile, direction=direction, domain=domain, style=style,
                      glossary=glossary, pages=pages_clean or None, dry_run=dry_run, max_pages=max_pages)
            cfg_run = cfg

            def work_tr(progress, data=data, kw=kw, cfg_run=cfg_run, stem=stem):
                tmp = tempfile.mkdtemp(prefix="pdftr_")
                try:
                    pdf_path = os.path.join(tmp, f"{stem}.pdf")
                    with open(pdf_path, "wb") as f:
                        f.write(data)
                    res = translate_document(pdf_path, cfg_run, out_dir=os.path.join(tmp, "out"),
                                             progress=progress, **kw)
                    return {
                        "stem": res.stem, "stats": res.stats, "elapsed": res.elapsed, "model": res.model,
                        "warnings": res.warnings, "dry": kw["dry_run"],
                        "docx": _read_bytes(res.files["docx"]), "md": _read_bytes(res.files["md"]),
                        "csv": _read_bytes(res.files["csv"]), "report": _read_text(res.files["report"]),
                        "docx_name": os.path.basename(res.files["docx"]),
                        "md_name": os.path.basename(res.files["md"]),
                    }
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

            _start("tr", work_tr)

        tr = _show_job("tr", "번역 중입니다")
        if tr:
            s = tr["stats"]
            if tr["dry"]:
                st.warning("테스트 모드 결과입니다. 번역문은 가짜이고 점수는 의미가 없습니다.")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("품질 점수", s["score"])
            m2.metric("확인 필요 문단", s["flagged"])
            m3.metric("자동 재번역", s["retried"])
            m4.metric("소요 시간", tr["elapsed"])
            st.caption(f"모델 {tr['model']} · 호출 {s['calls']}회 · 토큰 입력 {s['tokens_in']:,} / 출력 {s['tokens_out']:,}"
                       f" · 예상 비용 {_fmt_cost(s)}")
            for w in tr["warnings"]:
                st.warning(w)
            d1, d2, d3, d4 = st.columns(4)
            d1.download_button("Word (.docx)", tr["docx"], file_name=tr["docx_name"], key="dl_docx",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            d2.download_button("마크다운 (.md)", tr["md"], file_name=tr["md_name"], key="dl_md", mime="text/markdown")
            d3.download_button("검수 보고서 (.html)", tr["report"], file_name="report.html", key="dl_report",
                               mime="text/html")
            d4.download_button("문단별 내역 (.csv)", tr["csv"], file_name="report.csv", key="dl_csv", mime="text/csv")
            st.subheader("검수 보고서 · 원문·번역문 비교 (노란색 = 확인 필요)")
            _show_html(tr["report"], 760)

    # ================= 모델 비교 탭
    with tab_cmp:
        st.write("같은 PDF를 여러 모델로 번역해, **모델 이름을 가린 채** 나란히 읽고 문단마다 투표합니다. "
                 "선택한 쪽 수 × 모델 수만큼 비용이 듭니다.")
        default_cmp = ready[:3]
        chosen = st.multiselect("비교할 모델", ready, default=default_cmp, disabled=not ready)
        if not ready and not dry_run:
            st.info("사용할 수 있는 모델이 없습니다. Secrets에 API 키를 넣거나, 고급 옵션에서 테스트 모드를 켜세요.")
        if skipped:
            with st.expander("제외된 모델"):
                for n, why in skipped.items():
                    st.write(f"- {n}: {why}")
        deepl = st.text_area("DeepL 번역 결과 (선택) — 문단 사이에 빈 줄을 넣어 붙여넣으세요", height=120)
        cmp_pages = pages_clean or str(web.get("compare_default_pages", "1-3"))
        if not pages_clean:
            st.caption(f"쪽 범위를 비워두면 비교에서는 {cmp_pages}쪽만 사용합니다 (비용 보호).")
        enough = len(chosen) >= 2 or dry_run
        if ready and len(chosen) < 2 and not dry_run:
            st.caption("모델을 2개 이상 고르면 비교가 됩니다.")
        run_cmp = st.button("비교 시작", type="primary", key="run_cmp",
                            disabled=(up is None or not enough or (not ready and not dry_run) or _is_running("cmp")))
        if run_cmp:
            data = up.getvalue()
            kw = dict(profiles=None if dry_run else list(chosen), direction=direction, domain=domain, style=style,
                      glossary=glossary, pages=cmp_pages, dry_run=dry_run, max_pages=max_pages, deepl_text=deepl)
            cfg_run = cfg

            def work_cmp(progress, data=data, kw=kw, cfg_run=cfg_run, stem=stem):
                tmp = tempfile.mkdtemp(prefix="pdfcmp_")
                try:
                    pdf_path = os.path.join(tmp, f"{stem}.pdf")
                    with open(pdf_path, "wb") as f:
                        f.write(data)
                    res = compare_documents(pdf_path, cfg_run, out_dir=os.path.join(tmp, "out"),
                                            progress=progress, **kw)
                    return {
                        "html": _read_text(res.compare_html), "dry": kw["dry_run"], "note": res.deepl_note,
                        "failed": res.failed, "warnings": res.warnings,
                        "docx": {n: (os.path.basename(f["docx"]), _read_bytes(f["docx"]))
                                 for n, f in res.run_files.items()},
                        "scores": {r.name: r.stats["score"] for r in res.runs},
                    }
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

            _start("cmp", work_cmp)

        cmp = _show_job("cmp", "모델별로 번역 중입니다")
        if cmp:
            if cmp["dry"]:
                st.warning("테스트 모드 결과입니다. 번역문은 가짜입니다.")
            for n, why in cmp["failed"].items():
                st.error(f"{n} 실패: {why}")
            for w in cmp["warnings"]:
                st.warning(w)
            if cmp["note"]:
                st.warning(cmp["note"])
            st.download_button("비교 페이지 저장 (.html)", cmp["html"], file_name="compare.html", key="dl_cmp",
                               mime="text/html")
            cols = st.columns(max(1, len(cmp["docx"])))
            for col, (n, (fname, data)) in zip(cols, cmp["docx"].items()):
                col.download_button(f"{n} Word", data, file_name=f"{n}-{fname}", key=f"dl_cmp_{n}",
                                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            _show_html(cmp["html"], 900)


if gate():
    main()
