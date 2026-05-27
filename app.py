"""Streamlit UI for SQL Agent."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime

import streamlit as st

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── 將 Streamlit Secrets 同步到 os.environ（讓 agent 模組能用 os.getenv 讀取）
for _k in ("OPENAI_API_KEY", "SUPABASE_URL", "SUPABASE_KEY"):
    if _k not in os.environ and hasattr(st, "secrets") and _k in st.secrets:
        os.environ[_k] = st.secrets[_k]

st.set_page_config(
    page_title="SQL Agent",
    page_icon="⬡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS ───────────────────────────────────────────────────────────

st.markdown("""
<style>
.block-container { padding-top: 1.8rem; padding-bottom: 4rem;
                   padding-left: 3rem; padding-right: 3rem; }
footer, header { visibility: hidden; }

/* Title */
.sa-title { font-size: 2rem; font-weight: 800; letter-spacing: -0.5px;
            margin: 0; line-height: 1.2; }
.sa-sub   { font-size: 0.85rem; color: #888; margin-top: 4px; }

/* User bubble */
.sa-user { display:flex; align-items:flex-start; gap:10px;
           margin: 1.8rem 0 0.6rem 0; }
.sa-user-avatar { width:26px; height:26px; border-radius:50%;
    background:#e8eaf0; color:#555; font-size:0.68rem; font-weight:700;
    display:flex; align-items:center; justify-content:center;
    flex-shrink:0; margin-top:2px; }
.sa-user-text { font-size:1rem; color:#111; line-height:1.65; }

/* Intent badge */
.sa-badge { display:inline-block; padding:2px 9px; border-radius:4px;
            font-size:0.72rem; font-weight:600; margin-bottom:10px; }
.sa-badge-add    { background:#dafbe1; color:#1a7f37; }
.sa-badge-remove { background:#ffebe9; color:#cf222e; }
.sa-badge-modify { background:#fff3cd; color:#856404; }

/* Log expander */
details summary { font-size:0.88rem !important; color:#555; }
details summary:hover { color:#222; }

/* SQL section label */
.sa-sql-label { font-size:0.75rem; font-weight:600; color:#888;
                letter-spacing:0.06em; text-transform:uppercase;
                margin:12px 0 4px 0; }

/* Divider */
.sa-div { border:none; border-top:1px solid #f0f0f0; margin:14px 0 8px 0; }

/* Code */
.stCodeBlock { border-radius:6px !important; }

/* Input */
.stChatInput textarea { font-size:1rem; }
</style>
""", unsafe_allow_html=True)


# ── Dataclass ─────────────────────────────────────────────────────

@dataclass
class Turn:
    user_query: str
    sql: str
    reasoning: str
    intent: str = "NEW_QUERY"
    modification: str = ""
    phase1_log: str = ""   # 向量檢索
    phase2_log: str = ""   # 報表需求確認
    injected_log: str = ""
    step_a_log: str = ""
    step_b_log: str = ""
    step_c_log: list = field(default_factory=list)
    cost_usd: float = 0.0


# ── Session state ─────────────────────────────────────────────────

def _init():
    for k, v in {
        "conversation": [],
        "hits": None,
        "all_cases": None,
        "primary_scene": "",
        "_plan": None,
        "_sql_results": {},
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── Log helpers ───────────────────────────────────────────────────

def _fmt_injected(summary: dict) -> str:
    lines = []

    if summary.get("today"):
        lines.append(f"**今日日期（已注入 prompt）：** {summary['today']}")

    ent = summary.get("entities", {})
    if any([ent.get("products"), ent.get("concepts"), ent.get("branches")]):
        lines.append("\n**偵測到的實體：**")
        if ent.get("products"):
            lines.append(f"- 商品：{', '.join(ent['products'])}")
        if ent.get("concepts"):
            lines.append(f"- 業務概念：{', '.join(ent['concepts'])}")
        if ent.get("branches"):
            lines.append(f"- 分公司：{', '.join(ent['branches'])}")
        if ent.get("extra_tables"):
            lines.append(f"- 追加候選表格：{', '.join(ent['extra_tables'])}")
        if ent.get("codes"):
            for k, v in ent["codes"].items():
                lines.append(f"- WHERE 提示 `{k}` = `{v}`")

    if summary.get("skills"):
        lines.append(f"\n**觸發的 Business Skills（{len(summary['skills'])} 條）：**")
        for s in summary["skills"]:
            lines.append(f"- {s}")

    if summary.get("metrics"):
        lines.append(f"\n**注入的業務指標（{len(summary['metrics'])} 條）：**")
        for m in summary["metrics"]:
            lines.append(f"- {m}")

    if summary.get("relationships"):
        lines.append(f"\n**注入的 JOIN 關聯（{len(summary['relationships'])} 組）：**")
        for a, b in summary["relationships"]:
            lines.append(f"- {a} ↔ {b}")

    return "\n".join(lines) if lines else "（無額外注入）"

def _fmt_phase1(hits, all_cases: list) -> str:
    case_map = {str(c.get("資料夾")): c for c in all_cases}
    lines = []
    for hit in hits:
        c = case_map.get(hit.case_id, {})
        summary = (c.get("需求") or {}).get("需求摘要", "")[:70]
        scene = (c.get("業務場景") or {}).get("業務場景", "")
        lines.append(
            f"**#{hit.rank}** `[{hit.case_id}]` score={hit.score:.4f}  \n"
            f"{summary}  \n場景：{scene}"
        )
    return "\n\n".join(lines)



# ── Pipeline (progressive inline rendering) ───────────────────────

def _start_new_query(prompt: str, guardrail_tokens: dict | None = None) -> None:
    """Phase 1（向量檢索）+ Phase 2（報表確認），結果存入 session_state._plan 後 rerun。"""
    import json as _json
    from agent.config import ALL_CASES_PATH
    from agent.generator import _get_case_sql_text
    from agent.reader import normalize_requirement
    from agent.report_planner import plan_report
    from agent.retriever import retrieve

    with open(ALL_CASES_PATH, encoding="utf-8") as f:
        all_cases = _json.load(f)

    req_text = normalize_requirement(prompt)

    with st.container(border=True):
        # Phase 1：案例檢索 + 表格語意檢索
        from agent.embedding import set_waiting_callback, clear_waiting_callback
        _s = st.empty()
        _s.caption("⏳ Phase 1：向量檢索中…")

        def _on_waiting(n_others: int) -> None:
            _parts = ["目前有其他人正在使用向量檢索"]
            if n_others > 0:
                _parts.append(f"有 {n_others} 人在排隊")
            _parts.append("請稍候…")
            _s.warning("⏳ " + "，".join(_parts))

        set_waiting_callback(_on_waiting)
        try:
            hits = retrieve(req_text, all_cases, top_k=5)
            if not hits:
                _s.warning("找不到案例摘要，請先執行 `python -m agent --summarize`")
                return

            # 實體擷取
            from agent.entity_extractor import extract_entities
            extraction = extract_entities(req_text)
            entities_text = extraction.enriched_entities

            # 表格語意檢索（含分數，供 UI 顯示）
            from agent.table_retriever import retrieve_tables
            from agent.schema_summarizer import load_table_summaries
            from agent.generator import _get_union_tables, _load_schema_for_tables
            _summaries = load_table_summaries()
            available = set(_summaries.keys())
            _semantic_with_scores: list[tuple[str, float]] = retrieve_tables(req_text, top_n=5, with_scores=True)
            _semantic_tables = [t for t, _ in _semantic_with_scores]

            # 補充業務說明給 entities_text
            _table_desc_lines = []
            for _t, _ in _semantic_with_scores:
                _summary = _summaries.get(_t, "")
                if _summary:
                    _table_desc_lines.append(f"  • {_t}：{_summary[:80]}")
            if _table_desc_lines:
                _table_block = "【系統識別的相關資料來源（業務說明）】\n" + "\n".join(_table_desc_lines)
                entities_text = (entities_text + "\n\n" + _table_block).strip()

            # 候選表格 schema（供 Phase 2 report_planner 使用）
            _candidate_set_plan = set(_get_union_tables(hits, all_cases, available))
            _candidate_set_plan.update(t for t in _semantic_tables if t in available)
            for _t in extraction.extra_tables:
                if _t in available:
                    _candidate_set_plan.add(_t)
            schema_for_plan = _load_schema_for_tables(sorted(_candidate_set_plan))

            # Phase 1 log：案例 + 表格語意
            _table_rows = "\n".join(
                f"| `{t}` | {_summaries.get(t, '')[:55]} | {s:.4f} |"
                for t, s in _semantic_with_scores
            )
            phase1_log = (
                "**Top-5 案例檢索**\n\n"
                + _fmt_phase1(hits, all_cases)
                + "\n\n**表格語意檢索 Top-5**\n\n"
                "| 表格 | 業務說明 | 相似度 |\n"
                "|------|---------|--------|\n"
                + _table_rows
            )
        finally:
            clear_waiting_callback()

        _s.empty()
        with st.expander("Phase 1：向量檢索", expanded=False):
            st.markdown(phase1_log)

        # Phase 2：報表需求確認
        _s = st.empty()
        _s.caption("⏳ Phase 2：分析報表結構中…")
        case_sqls = [_get_case_sql_text(h.case_id, all_cases) for h in hits]
        plan = plan_report(req_text, case_sqls, entities_text=entities_text, schema_text=schema_for_plan)
        _s.empty()

    st.session_state._plan = {
        "prompt":           prompt,
        "req":              req_text,
        "hits":             hits,
        "all_cases":        all_cases,
        "scene":            "",
        "phase1_log":       phase1_log,
        "guardrail_tokens": guardrail_tokens or {},
        "case_sqls":        case_sqls,
        "entities_text":    entities_text,
        "schema_for_plan":  schema_for_plan,
        "plan":             plan,
        "qa_history":       [],
        "all_plan_tokens":  dict(plan.tokens),
    }
    st.rerun()


def _confirm_and_generate(pending: dict) -> None:
    """使用者確認後執行 SQL 生成，寫入 Supabase，存入對話歷史，rerun。"""
    from agent.report_planner import fmt_plan_for_prompt, fmt_plan_for_user
    from agent.config import CLASSIFICATION_MODEL, GENERATION_MODEL, get_model_pricing
    from agent.generator import generate

    plan             = pending["plan"]
    report_plan_text = fmt_plan_for_prompt(plan)
    report_plan_log  = fmt_plan_for_user(plan)

    with st.expander("Phase 2：報表需求確認", expanded=True):
        st.markdown(report_plan_log)

    _s = st.empty()
    _s.caption("⏳ Step A + B：SQL 生成中…（需要一些時間）")
    gen = generate(
        pending["req"],
        pending["hits"],
        pending["all_cases"],
        model=GENERATION_MODEL,
        scene=pending["scene"],
        report_plan_text=report_plan_text,
    )

    step_a_log = (
        f"**候選表格（{len(gen.candidate_tables)} 張）：**  \n"
        f"{', '.join(gen.candidate_tables)}\n\n"
        + (f"**Step A 思路：**\n\n{gen.step_a_reasoning}" if gen.step_a_reasoning else "")
    )
    step_b_log = (
        f"**完整表格範圍（{len(gen.all_tables)} 張）：**  \n"
        f"{', '.join(gen.all_tables)}\n\n"
        + (f"**Step B 分析：**\n\n{gen.final_analysis}" if gen.final_analysis else "")
    )
    _s.empty()
    with st.expander("Prompt 注入內容", expanded=False):
        st.markdown(_fmt_injected(gen.injected_summary))
    with st.expander("Step A：草稿生成", expanded=False):
        st.markdown(step_a_log)
    with st.expander("Step B：自我批判", expanded=False):
        st.markdown(step_b_log)

    # Step C：語法驗證 + 決定性幻覺檢查結果
    step_c_log = gen.step_c_log
    if step_c_log:
        all_ok = all(e.get("passed", True) for e in step_c_log)
        label = f"Step C：驗證　{'✅ 通過' if all_ok else '❌ 有問題'}"
        with st.expander(label, expanded=not all_ok):
            for entry in step_c_log:
                if entry.get("stage") == "hallucination":
                    if entry["passed"]:
                        st.success("C-2 幻覺檢查：通過")
                    else:
                        st.warning(f"C-2 幻覺檢查：發現 {len(entry['errors'])} 個問題，已送 LLM 修正")
                        for err in entry["errors"]:
                            st.code(err, language="text")
                elif entry.get("passed"):
                    st.success(f"C-1 Round {entry['round']}：語法驗證通過")
                else:
                    st.warning(f"C-1 Round {entry['round']}：發現 {len(entry['errors'])} 個問題")
                    for err in entry["errors"]:
                        st.code(err, language="text")

    st.markdown('<div class="sa-sql-label">最終 SQL</div>', unsafe_allow_html=True)
    st.code(_clean_sql(gen.final_sql), language="sql")
    if gen.final_reasoning:
        with st.expander("SQL 思路", expanded=False):
            st.markdown(gen.final_reasoning)

    # ── 費用計算 ──────────────────────────────────────────────────
    clf_price_in, clf_price_out = get_model_pricing(CLASSIFICATION_MODEL)
    guardrail_tokens = pending["guardrail_tokens"]
    all_plan_tokens  = pending.get("all_plan_tokens", {})

    guardrail_cost = (
        guardrail_tokens.get("guardrail_in", 0) / 1_000_000 * clf_price_in
        + guardrail_tokens.get("guardrail_out", 0) / 1_000_000 * clf_price_out
    )
    plan_cost = (
        all_plan_tokens.get("plan_in", 0) / 1_000_000 * clf_price_in
        + all_plan_tokens.get("plan_out", 0) / 1_000_000 * clf_price_out
    )
    total_cost = guardrail_cost + plan_cost + gen.cost_usd

    from agent.supabase_logger import insert
    ok, err = insert("experiments", {
        "name": "generate",
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "results": {
            "query": pending["prompt"],
            "scene": pending["scene"],
            "candidate_tables": gen.candidate_tables,
            "all_tables": gen.all_tables,
            "final_sql": gen.final_sql,
            "tokens": {**guardrail_tokens, **all_plan_tokens, **gen.tokens},
            "cost_usd": total_cost,
        },
        "log": "",
    })
    if not ok:
        st.warning(f"⚠️ Supabase log 寫入失敗：{err}")

    turn = Turn(
        user_query=pending["prompt"],
        sql=gen.final_sql,
        reasoning=gen.final_reasoning,
        intent="NEW_QUERY",
        phase1_log=pending["phase1_log"],
        phase2_log=report_plan_log,
        injected_log=_fmt_injected(gen.injected_summary),
        step_a_log=step_a_log,
        step_b_log=step_b_log,
        step_c_log=gen.step_c_log,
        cost_usd=total_cost,
    )
    st.session_state.conversation.append(turn)
    st.session_state.hits          = pending["hits"]
    st.session_state.all_cases     = pending["all_cases"]
    st.session_state.primary_scene = pending["scene"]
    st.session_state._plan         = None
    st.rerun()


def _run_and_render_refiner(new_query: str, guardrail_tokens: dict | None = None) -> Turn | None:
    from agent.refiner import build_conversation_summary, classify_followup, refine
    from agent.schema_summarizer import load_table_summaries
    from agent.config import CLASSIFICATION_MODEL, GENERATION_MODEL, get_model_pricing

    conv          = st.session_state.conversation
    current_sql   = conv[-1].sql
    current_rsn   = conv[-1].reasoning
    available     = set(load_table_summaries().keys())

    # Fast intent check (outside bordered container)
    _s = st.empty()
    _s.caption("⏳ 分析追問意圖…")
    classification = classify_followup(current_sql, new_query, available, model=CLASSIFICATION_MODEL)
    intent = classification.get("intent", "MODIFY_SQL")
    _s.empty()

    if intent == "NEW_QUERY":
        return _run_and_render_full(new_query, guardrail_tokens=guardrail_tokens)

    phase1_log = (
        f"**意圖：** {intent}  \n"
        f"**說明：** {classification.get('explanation', '')}  \n"
        f"**相關表格：** {', '.join(classification.get('target_tables') or []) or '—'}"
    )

    with st.container(border=True):
        if intent in _BADGE:
            cls, label = _BADGE[intent]
            st.markdown(f'<span class="sa-badge {cls}">{label}</span>',
                        unsafe_allow_html=True)

        with st.expander("Phase 1：追問分析", expanded=False):
            st.markdown(phase1_log)

        _s = st.empty()
        _s.caption("⏳ SQL 改寫中…")
        summary = build_conversation_summary(conv)
        result  = refine(summary, current_sql, current_rsn, new_query,
                         classification, model=GENERATION_MODEL)
        _s.empty()

        if result.modification_note:
            with st.expander("改法說明", expanded=False):
                st.markdown(result.modification_note)

        st.markdown('<div class="sa-sql-label">最終 SQL</div>', unsafe_allow_html=True)
        st.code(_clean_sql(result.new_sql), language="sql")
        if result.new_reasoning:
            with st.expander("SQL 思路", expanded=False):
                st.markdown(result.new_reasoning)

    # ── 費用計算 ──────────────────────────────────────────────────
    guardrail_cost = 0.0
    if guardrail_tokens:
        g_price_in, g_price_out = get_model_pricing(CLASSIFICATION_MODEL)
        guardrail_cost = (
            guardrail_tokens.get("guardrail_in", 0) / 1_000_000 * g_price_in
            + guardrail_tokens.get("guardrail_out", 0) / 1_000_000 * g_price_out
        )
    total_cost = guardrail_cost + result.cost_usd

    # ── 寫 Supabase experiment log ────────────────────────────────
    from agent.supabase_logger import insert
    all_tokens = {
        **(guardrail_tokens or {}),
        **result.classify_tokens,
        **result.refine_tokens,
    }
    ok, err = insert("experiments", {
        "name": "refine",
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "results": {
            "query": new_query,
            "intent": result.intent,
            "target_tables": result.target_tables,
            "final_sql": result.new_sql,
            "tokens": all_tokens,
            "cost_usd": total_cost,
        },
        "log": "",
    })
    if not ok:
        st.warning(f"⚠️ Supabase log 寫入失敗：{err}")

    return Turn(
        user_query=new_query,
        sql=result.new_sql,
        reasoning=result.new_reasoning,
        intent=result.intent,
        modification=result.modification_note,
        phase1_log=phase1_log,
        cost_usd=total_cost,
    )


# ── Save feedback ─────────────────────────────────────────────────

def _save_feedback(rating: str, text: str):
    from agent.supabase_logger import insert
    from agent.config import BASE_DIR

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    turns_snapshot = [
        {"query": t.user_query, "sql": t.sql, "intent": t.intent}
        for t in st.session_state.conversation
    ]
    payload = {
        "timestamp": ts,
        "rating": rating,
        "feedback_text": text,
        "turns": turns_snapshot,
    }

    ok, err = insert("feedback", payload)

    if not ok:
        st.warning(f"⚠️ Supabase feedback 寫入失敗：{err}")
        # Fallback：寫入本機 JSON
        out = BASE_DIR / "experiment" / f"{ts}_feedback.json"
        out.parent.mkdir(exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


# ── Global feedback dialog ────────────────────────────────────────

@st.dialog("你認為 SQL Agent 回答得如何？")
def _feedback_dialog():
    rating_key = "_global_fb_rating"
    rating = st.session_state.get(rating_key)

    c1, c2, _ = st.columns([1, 1, 6])
    with c1:
        if st.button("👍  有幫助", use_container_width=True,
                     type="primary" if rating == "up" else "secondary"):
            st.session_state[rating_key] = "up"
            # 不呼叫 st.rerun()，讓按鈕點擊自然觸發 dialog 內重繪
    with c2:
        if st.button("👎  需改進", use_container_width=True,
                     type="primary" if rating == "down" else "secondary"):
            st.session_state[rating_key] = "down"

    st.write("")
    text = st.text_area(
        "說明你的想法（選填）",
        placeholder="例如：某個 phase 的邏輯有問題、SQL 表格選錯、結果不符合預期…",
        height=110,
        key="_global_fb_text",
    )

    # 重新讀取（按鈕點擊後 session_state 已更新）
    rating = st.session_state.get(rating_key)

    st.write("")
    if rating:
        st.info(f"已選擇：{'👍 有幫助' if rating == 'up' else '👎 需改進'}　送出後將開始新對話。")
        if st.button("確定送出", type="primary", use_container_width=True):
            _save_feedback(rating, text)
            # 清除 feedback 狀態
            st.session_state.pop(rating_key, None)
            st.session_state.pop("_global_fb_text", None)
            # 重置對話，開始新一輪
            for k in ("conversation", "hits", "all_cases"):
                st.session_state[k] = [] if k == "conversation" else None
            st.session_state.primary_scene = ""
            st.session_state._plan = None
            st.rerun()
    else:
        st.caption("請先選擇 👍 或 👎")


# ── Oracle SQL runner ─────────────────────────────────────────────

def _init_oracle_client() -> None:
    """初始化 Oracle thick mode client（每個 session 只執行一次）。"""
    if st.session_state.get("_oracle_client_init"):
        return
    try:
        import oracledb
        lib_dir = (st.secrets.get("oracle") or {}).get("lib_dir")
        if lib_dir:
            oracledb.init_oracle_client(lib_dir=lib_dir)
        else:
            oracledb.init_oracle_client()
    except Exception:
        pass  # 已初始化或 thin mode，忽略
    st.session_state._oracle_client_init = True


def _sql_runner_widget(sql: str, widget_key: str) -> None:
    """執行 SQL 按鈕 + 結果顯示（結果快取在 session_state._sql_results）。"""
    import pandas as pd

    results: dict = st.session_state._sql_results

    if st.button("▶ 執行 SQL", key=f"run_{widget_key}", type="secondary"):
        if "oracle" not in st.secrets:
            st.session_state._sql_results[widget_key] = {
                "ok": False,
                "error": "尚未設定 Oracle 連線，請在 .streamlit/secrets.toml 加入 [oracle] 區段。",
            }
        else:
            with st.spinner("連線 Oracle 執行中…"):
                try:
                    import oracledb
                    import time
                    _init_oracle_client()
                    ora = st.secrets["oracle"]
                    dsn  = f"{ora['host']}:{ora['port']}/{ora['service_name']}"
                    conn = oracledb.connect(user=ora["user"], password=ora["password"], dsn=dsn)
                    cursor = conn.cursor()
                    cursor.arraysize = 10000
                    t0 = time.time()
                    cursor.execute(sql)
                    if cursor.description:
                        cols = [d[0].lower() for d in cursor.description]
                        rows = cursor.fetchmany(500)
                        elapsed = time.time() - t0
                        df = pd.DataFrame(rows, columns=cols)
                        st.session_state._sql_results[widget_key] = {
                            "ok": True, "df": df,
                            "fetched": len(rows), "elapsed": elapsed,
                        }
                    else:
                        st.session_state._sql_results[widget_key] = {
                            "ok": True, "df": None,
                        }
                    cursor.close()
                    conn.close()
                except Exception as e:
                    st.session_state._sql_results[widget_key] = {
                        "ok": False, "error": str(e),
                    }
        st.rerun()

    r = results.get(widget_key)
    if r is None:
        return
    if r["ok"]:
        if r["df"] is not None:
            fetched  = r["fetched"]
            elapsed  = r.get("elapsed", 0)
            cap = f"查詢結果　{fetched} 筆　{elapsed:.1f}s"
            if fetched == 500:
                cap += "　（已達 500 筆上限，可能有更多）"
            st.caption(cap)
            st.dataframe(r["df"], use_container_width=True)
        else:
            st.success("執行成功（無回傳資料）")
    else:
        st.error(f"執行錯誤：{r['error']}")


# ── Render ────────────────────────────────────────────────────────

_BADGE = {
    "ADD_TABLE":    ("sa-badge-add",    "＋ 新增表格"),
    "REMOVE_TABLE": ("sa-badge-remove", "－ 移除欄位"),
    "MODIFY_SQL":   ("sa-badge-modify", "✎ 修改邏輯"),
}

def _clean_sql(raw: str) -> str:
    s = raw.strip()
    for fence in ("```sql", "```"):
        if s.startswith(fence):
            s = s[len(fence):]
    return s.strip().rstrip("`").strip()


def _render_turn(turn: Turn, idx: int):
    st.markdown(
        f'<div class="sa-user">'
        f'<div class="sa-user-avatar">你</div>'
        f'<div class="sa-user-text">{turn.user_query}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        # Badge (追問才顯示)
        if turn.intent in _BADGE:
            cls, label = _BADGE[turn.intent]
            st.markdown(f'<span class="sa-badge {cls}">{label}</span>',
                        unsafe_allow_html=True)

        # Modification note
        if turn.modification:
            with st.expander("改法說明", expanded=False):
                st.markdown(turn.modification)

        # Phase / Step logs
        log_sections = [
            ("Phase 1：向量檢索", turn.phase1_log),
            ("Phase 2：報表需求確認", turn.phase2_log),
            ("Prompt 注入內容", turn.injected_log),
            ("Step A：草稿生成", turn.step_a_log),
            ("Step B：自我批判", turn.step_b_log),
            ("Step C：語法驗證", turn.step_c_log),
        ]
        for label, log in log_sections:
            if label == "Step C：語法驗證":
                if log:
                    all_ok = all(e.get("passed", True) for e in log)
                    icon = "✅" if all_ok else "❌"
                    with st.expander(f"{label}　{icon}", expanded=False):
                        for entry in log:
                            if entry.get("stage") == "hallucination":
                                if entry["passed"]:
                                    st.success("C-2 幻覺檢查：通過")
                                else:
                                    st.warning(f"C-2 幻覺檢查：發現 {len(entry['errors'])} 個問題，已送 LLM 修正")
                                    for err in entry["errors"]:
                                        st.code(err, language="text")
                            elif entry.get("passed"):
                                st.success(f"C-1 Round {entry['round']}：通過")
                            else:
                                st.warning(f"C-1 Round {entry['round']}：{len(entry['errors'])} 個問題")
                                for err in entry["errors"]:
                                    st.code(err, language="text")
            elif log.strip():
                with st.expander(label, expanded=False):
                    st.markdown(log)

        # Final SQL (直接顯示)
        st.markdown('<div class="sa-sql-label">最終 SQL</div>', unsafe_allow_html=True)
        st.code(_clean_sql(turn.sql), language="sql")
        # _sql_runner_widget(_clean_sql(turn.sql), f"turn_{idx}")

        # Reasoning
        if turn.reasoning:
            with st.expander("SQL 思路", expanded=False):
                st.markdown(turn.reasoning)



# ── Available tables ──────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_available_tables() -> list[str]:
    from agent.table_retriever import _EXTRA_TABLES, _USED_TABLES_PATH
    tables: set[str] = set(_EXTRA_TABLES)
    if _USED_TABLES_PATH.exists():
        for line in _USED_TABLES_PATH.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t:
                tables.add(t)
    return sorted(tables)


def _render_available_tables() -> None:
    tables = _load_available_tables()
    names = "　".join(tables)
    st.markdown(
        f'<p style="font-size:0.72rem;color:#999;line-height:1.6;margin:0">'
        f'可使用資料表（{len(tables)} 張）：{names}</p>',
        unsafe_allow_html=True,
    )


# ── Main ──────────────────────────────────────────────────────────

def main():
    _init()

    # ── Header ────────────────────────────────────────────────────
    h1, _, h2 = st.columns([5, 2, 1])
    with h1:
        st.markdown('<p class="sa-title">SQL Agent</p>', unsafe_allow_html=True)
        st.markdown('<p class="sa-sub">以自然語言描述報表需求，自動生成 Oracle SQL</p>',
                    unsafe_allow_html=True)
    with h2:
        if st.session_state.conversation or st.session_state._plan:
            st.write("")
            if st.button("新對話", use_container_width=True):
                for k in ("conversation", "hits", "all_cases"):
                    st.session_state[k] = [] if k == "conversation" else None
                st.session_state.primary_scene = ""
                st.session_state._plan = None
                st.rerun()

    st.markdown('<hr class="sa-div">', unsafe_allow_html=True)
    _render_available_tables()

    # ── 報表結構確認中（ask / confirm 雙向對話）────────────────────
    if st.session_state._plan is not None:
        from agent.report_planner import fmt_plan_for_user, plan_report

        pending = st.session_state._plan
        plan    = pending["plan"]

        # 使用者原始需求氣泡
        st.markdown(
            f'<div class="sa-user">'
            f'<div class="sa-user-avatar">你</div>'
            f'<div class="sa-user-text">{pending["prompt"]}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            with st.expander("Phase 1：向量檢索", expanded=False):
                st.markdown(pending["phase1_log"])

            # 顯示已完成的問答記錄
            for qa in pending.get("qa_history", []):
                st.info(f"系統問：{qa['q']}")
                st.markdown(
                    f'<div class="sa-user" style="margin:4px 0 12px 0;">'
                    f'<div class="sa-user-avatar">你</div>'
                    f'<div class="sa-user-text">{qa["a"]}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            try:
                if plan.status == "ask":
                    # LLM 還有問題要問
                    st.info(f"**{plan.question}**")

                else:
                    # LLM 認為資訊充足，呈現理解讓使用者確認
                    st.markdown(
                        "**根據你的需求與相似案例，系統理解這份報表應呈現如下，請確認是否正確：**"
                    )
                    st.markdown(fmt_plan_for_user(plan))
                    st.markdown("---")

                    with st.form("plan_confirm_form", clear_on_submit=True):
                        col_input, col_send = st.columns([6, 1])
                        with col_input:
                            correction = st.text_input(
                                "如有需要調整，請說明",
                                placeholder="例如：希望每一列是分公司層級，而不是帳戶",
                                label_visibility="collapsed",
                            )
                        with col_send:
                            corrected = st.form_submit_button("送出", use_container_width=True)
                        confirmed = st.form_submit_button(
                            "邏輯確認過沒有問題，立刻開始生成 SQL",
                            type="primary",
                            use_container_width=True,
                        )

                    submitted = confirmed or corrected
                    if submitted:
                        if correction.strip():
                            # 使用者指出需修正 → 加入記錄重新分析
                            _s = st.empty()
                            _s.caption("⏳ 重新分析報表結構…")
                            qa_history = pending.get("qa_history", []) + [
                                {"q": "請確認報表呈現方式", "a": correction}
                            ]
                            new_plan = plan_report(
                                pending["req"],
                                pending["case_sqls"],
                                qa_history=qa_history,
                                entities_text=pending.get("entities_text", ""),
                                schema_text=pending.get("schema_for_plan", ""),
                            )
                            _s.empty()
                            pending["plan"] = new_plan
                            pending["qa_history"] = qa_history
                            _acc = pending.get("all_plan_tokens", {})
                            for k, v in new_plan.tokens.items():
                                _acc[k] = _acc.get(k, 0) + v
                            pending["all_plan_tokens"] = _acc
                            st.session_state._plan = pending
                            st.rerun()
                        else:
                            # 確認無誤 → 生成 SQL
                            _confirm_and_generate(pending)

            except Exception as e:
                import traceback
                st.error(f"錯誤：{e}\n\n```\n{traceback.format_exc()}\n```")

        # ask 狀態：用 chat_input 接收回答
        if plan.status == "ask":
            answer = st.chat_input("請回答上方的問題…")
            if answer:
                _s = st.empty()
                _s.caption("⏳ 重新分析報表結構…")
                qa_history = pending.get("qa_history", []) + [
                    {"q": plan.question, "a": answer}
                ]
                new_plan = plan_report(
                    pending["req"],
                    pending["case_sqls"],
                    qa_history=qa_history,
                    entities_text=pending.get("entities_text", ""),
                    schema_text=pending.get("schema_for_plan", ""),
                )
                _s.empty()
                pending["plan"] = new_plan
                pending["qa_history"] = qa_history
                _acc = pending.get("all_plan_tokens", {})
                for k, v in new_plan.tokens.items():
                    _acc[k] = _acc.get(k, 0) + v
                pending["all_plan_tokens"] = _acc
                st.session_state._plan = pending
                st.rerun()

        return  # 不顯示對話歷史或正常 chat input

    # ── Conversation ──────────────────────────────────────────────
    for i, turn in enumerate(st.session_state.conversation):
        _render_turn(turn, i)

    # ── Input ──────────────────────────────────────────────────────
    is_first = not st.session_state.conversation
    prompt = st.chat_input(
        "描述你的報表需求..." if is_first else "繼續追問，或修改 SQL...",
        max_chars=300,
    )

    if prompt:
        st.markdown(
            f'<div class="sa-user">'
            f'<div class="sa-user-avatar">你</div>'
            f'<div class="sa-user-text">{prompt}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── Guardrail ─────────────────────────────────────────────
        _gs = st.empty()
        _gs.caption("🛡️ 安全審查中…")
        from agent.guardrail import check_input
        _guard = check_input(prompt)
        is_safe, reason = _guard[0], _guard[1]
        guardrail_tokens = _guard[2] if len(_guard) > 2 else {}
        _gs.empty()

        if not is_safe:
            st.error(f"⛔ 輸入不符合規範，請重新描述報表需求。\n\n原因：{reason}")
        else:
            try:
                if is_first:
                    _start_new_query(prompt, guardrail_tokens=guardrail_tokens)
                else:
                    turn = _run_and_render_refiner(prompt, guardrail_tokens=guardrail_tokens)
                    if turn:
                        st.session_state.conversation.append(turn)
            except Exception as e:
                import traceback
                st.error(f"錯誤：{e}\n\n```\n{traceback.format_exc()}\n```")

    # ── Feedback button (bottom) ──────────────────────────────────
    if st.session_state.conversation:
        st.write("")
        st.markdown('<hr class="sa-div">', unsafe_allow_html=True)
        if st.button(
            "📝  請填寫回饋　你留下的寶貴意見會用於調整 Agent，謝謝！",
            use_container_width=True,
            key="global_fb_btn",
        ):
            _feedback_dialog()


if __name__ == "__main__":
    main()
