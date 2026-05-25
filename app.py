"""Streamlit UI for SQL Agent."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime

import streamlit as st

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

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
    phase1_log: str = ""
    phase2_log: str = ""
    step_a_log: str = ""
    step_b_log: str = ""


# ── Session state ─────────────────────────────────────────────────

def _init():
    for k, v in {
        "conversation": [],
        "hits": None,
        "all_cases": None,
        "primary_scene": "",
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── Log helpers ───────────────────────────────────────────────────

def _fmt_phase1(classification) -> str:
    header = [
        f"**主要場景：** {classification.主要場景}",
        f"**分類理由：** {classification.分類理由}",
        "",
        "各場景置信度：",
    ]
    chart = []
    for item in sorted(classification.各標籤置信度, key=lambda x: x.分數, reverse=True):
        bar = "█" * round(item.分數 * 16) + "░" * (16 - round(item.分數 * 16))
        tag = " ← 主" if item.標籤 == classification.主要場景 else (
              " ← 次" if item.標籤 == classification.次要場景 else "")
        cjk = sum(1 for c in item.標籤 if "一" <= c <= "鿿")
        dw  = cjk * 2 + (len(item.標籤) - cjk)
        pad = " " * max(0, 22 - dw)
        chart.append(f"{item.標籤}{pad}  {item.分數:.2f}  {bar}{tag}")
    return "\n".join(header) + "\n```\n" + "\n".join(chart) + "\n```"


def _fmt_phase2(hits, all_cases: list) -> str:
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

def _run_and_render_full(requirement: str) -> Turn | None:
    import json as _json
    from agent.classifier import classify_intent
    from agent.config import ALL_CASES_PATH, GENERATION_MODEL
    from agent.entity_extractor import extract_entities
    from agent.generator import generate
    from agent.reader import normalize_requirement
    from agent.retriever import retrieve

    with open(ALL_CASES_PATH, encoding="utf-8") as f:
        all_cases = _json.load(f)

    req_text = normalize_requirement(requirement)

    with st.container(border=True):
        # Phase 1
        _s = st.empty()
        _s.caption("⏳ Phase 1：場景分類中…")
        classification = classify_intent(requirement)
        primary_scene  = classification.主要場景
        phase1_log     = _fmt_phase1(classification)
        _s.empty()
        with st.expander("Phase 1：場景分類", expanded=False):
            st.markdown(phase1_log)

        # Phase 2
        _s = st.empty()
        _s.caption("⏳ Phase 2：向量檢索中…")
        hits = retrieve(req_text, all_cases, top_k=5)
        if not hits:
            _s.warning("找不到案例摘要，請先執行 `python -m agent --summarize`")
            return None

        extraction    = extract_entities(req_text)
        entity_sec    = (
            f"**偵測到的實體**\n\n{extraction.enriched_entities}\n\n---\n\n"
            if extraction.enriched_entities else ""
        )
        phase2_log = entity_sec + "**Top-5 檢索結果**\n\n" + _fmt_phase2(hits, all_cases)
        _s.empty()
        with st.expander("Phase 2：向量檢索", expanded=False):
            st.markdown(phase2_log)

        # Step A + B
        _s = st.empty()
        _s.caption("⏳ Step A + B：SQL 生成中…（需要一些時間）")
        gen = generate(req_text, hits, all_cases, model=GENERATION_MODEL, scene=primary_scene)

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
        with st.expander("Step A：草稿生成", expanded=False):
            st.markdown(step_a_log)
        with st.expander("Step B：自我批判", expanded=False):
            st.markdown(step_b_log)

        st.markdown('<div class="sa-sql-label">最終 SQL</div>', unsafe_allow_html=True)
        st.code(_clean_sql(gen.final_sql), language="sql")
        if gen.final_reasoning:
            with st.expander("SQL 思路", expanded=False):
                st.markdown(gen.final_reasoning)

    st.session_state.hits          = hits
    st.session_state.all_cases     = all_cases
    st.session_state.primary_scene = primary_scene

    return Turn(
        user_query=requirement,
        sql=gen.final_sql,
        reasoning=gen.final_reasoning,
        intent="NEW_QUERY",
        phase1_log=phase1_log,
        phase2_log=phase2_log,
        step_a_log=step_a_log,
        step_b_log=step_b_log,
    )


def _run_and_render_refiner(new_query: str) -> Turn | None:
    from agent.refiner import build_conversation_summary, classify_followup, refine
    from agent.schema_summarizer import load_table_summaries
    from agent.config import CLASSIFICATION_MODEL, GENERATION_MODEL

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
        return _run_and_render_full(new_query)

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

    return Turn(
        user_query=new_query,
        sql=result.new_sql,
        reasoning=result.new_reasoning,
        intent=result.intent,
        modification=result.modification_note,
        phase1_log=phase1_log,
    )


# ── Save feedback ─────────────────────────────────────────────────

def _save_feedback(rating: str, text: str):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    turns_snapshot = [
        {"query": t.user_query, "sql": t.sql, "intent": t.intent}
        for t in st.session_state.conversation
    ]

    # 嘗試寫入 Supabase（Cloud 環境）
    supabase_url = st.secrets.get("SUPABASE_URL", "") if hasattr(st, "secrets") else ""
    supabase_key = st.secrets.get("SUPABASE_KEY", "") if hasattr(st, "secrets") else ""
    if not supabase_url:
        import os
        supabase_url = os.getenv("SUPABASE_URL", "")
        supabase_key = os.getenv("SUPABASE_KEY", "")

    if supabase_url and supabase_key:
        try:
            from supabase import create_client
            client = create_client(supabase_url, supabase_key)
            client.table("feedback").insert({
                "timestamp": ts,
                "rating": rating,
                "feedback_text": text,
                "turns": turns_snapshot,
            }).execute()
            return
        except Exception as e:
            st.warning(f"Supabase 寫入失敗，改存本機：{e}")

    # Fallback：寫入本機 JSON
    from agent.config import BASE_DIR
    out = BASE_DIR / "experiment" / f"{ts}_feedback.json"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": ts,
            "turns": turns_snapshot,
            "feedback": {"rating": rating, "text": text},
        }, f, ensure_ascii=False, indent=2)


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
            st.rerun()
    with c2:
        if st.button("👎  需改進", use_container_width=True,
                     type="primary" if rating == "down" else "secondary"):
            st.session_state[rating_key] = "down"
            st.rerun()

    st.write("")
    text = st.text_area(
        "說明你的想法（選填）",
        placeholder="例如：某個 phase 的邏輯有問題、SQL 表格選錯、結果不符合預期…",
        height=110,
        key="_global_fb_text",
    )

    st.write("")
    if rating:
        st.warning("送出後頁面將重新整理。", icon="⚠️")
        if st.button("確定送出", type="primary", use_container_width=True):
            _save_feedback(rating, text)
            st.session_state.pop(rating_key, None)
            st.session_state.pop("_global_fb_text", None)
            st.rerun()
    else:
        st.caption("請先選擇 👍 或 👎")


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
    # User
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
            ("Phase 1：場景分類", turn.phase1_log),
            ("Phase 2：向量檢索", turn.phase2_log),
            ("Step A：草稿生成", turn.step_a_log),
            ("Step B：自我批判", turn.step_b_log),
        ]
        for label, log in log_sections:
            if log.strip():
                with st.expander(label, expanded=False):
                    st.markdown(log)

        # Final SQL (直接顯示)
        st.markdown('<div class="sa-sql-label">最終 SQL</div>', unsafe_allow_html=True)
        st.code(_clean_sql(turn.sql), language="sql")

        # Reasoning
        if turn.reasoning:
            with st.expander("SQL 思路", expanded=False):
                st.markdown(turn.reasoning)


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
        if st.session_state.conversation:
            st.write("")
            if st.button("新對話", use_container_width=True):
                for k in ("conversation", "hits", "all_cases"):
                    st.session_state[k] = [] if k == "conversation" else None
                st.session_state.primary_scene = ""
                st.rerun()

    st.markdown('<hr class="sa-div">', unsafe_allow_html=True)

    # ── Conversation ──────────────────────────────────────────────
    for i, turn in enumerate(st.session_state.conversation):
        _render_turn(turn, i)

    # ── Input ──────────────────────────────────────────────────────
    is_first = not st.session_state.conversation
    prompt = st.chat_input(
        "描述你的報表需求..." if is_first else "繼續追問，或修改 SQL...",
        max_chars=100,
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
        is_safe, reason = check_input(prompt)
        _gs.empty()

        if not is_safe:
            st.error(f"⛔ 輸入不符合規範，請重新描述報表需求。\n\n原因：{reason}")
        else:
            try:
                turn = _run_and_render_full(prompt) if is_first else _run_and_render_refiner(prompt)
            except Exception as e:
                import traceback
                st.error(f"錯誤：{e}\n\n```\n{traceback.format_exc()}\n```")
                turn = None

            if turn:
                st.session_state.conversation.append(turn)
            elif is_first:
                st.warning("找不到案例摘要，請先執行 `python -m agent --summarize`")

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
