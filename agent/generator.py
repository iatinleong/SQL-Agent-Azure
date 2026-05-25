"""SQL 生成：Phase 2 檢索後，Step A（候選池草稿）→ Step B（自我批判 + 最終 SQL）。"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import BASE_DIR, GENERATION_MODEL, get_model_pricing, openai_client
from .retriever import RetrievalHit

_REASONING_MODELS = {"o1", "o1-mini", "o3", "o3-mini", "o4-mini"}


def _chat(model: str, messages: list[dict], **kwargs) -> object:
    """統一呼叫入口：不支援自訂 temperature 的 model 自動移除該參數。"""
    base = model.split("-")[0] if "-" in model else model
    no_temp = (model in _REASONING_MODELS
               or base in _REASONING_MODELS
               or model.startswith("gpt-5"))
    if no_temp:
        kwargs.pop("temperature", None)
    return openai_client.chat.completions.create(model=model, messages=messages, **kwargs)

SCHEMA_PATH: Path = BASE_DIR / "schema.csv"
RELATIONSHIPS_PATH: Path = BASE_DIR / "relationships.json"
METRICS_PATH: Path = BASE_DIR / "metrics.json"
BUSINESS_SKILLS_PATH: Path = BASE_DIR / "business_skills.json"
CODE_MAPPING_PATH: Path = BASE_DIR / "code_mapping.json"

_col_codes: dict | None = None  # { COLUMN_NAME_UPPER: {code: desc} }

def _get_col_codes() -> dict:
    """載入 code_mapping.json（扁平欄位結構），以欄位名大寫為 key。"""
    global _col_codes
    if _col_codes is not None:
        return _col_codes
    if CODE_MAPPING_PATH.exists():
        with open(CODE_MAPPING_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        _col_codes = {k.upper(): v for k, v in raw.items()}
    else:
        _col_codes = {}
    return _col_codes


def _fmt_codes(codes: dict, max_show: int = 20) -> str:
    """將代碼 dict 格式化為 [001=男, 002=女, ...共4種]。超過 max_show 種則截斷。"""
    items = list(codes.items())
    shown = items[:max_show]
    parts = [f"{k}={v}" for k, v in shown]
    suffix = f"...共{len(items)}種" if len(items) > max_show else ""
    inner = ", ".join(parts) + (", " + suffix if suffix else "")
    return f"[{inner}]"

SEP = "─" * 62
WIDE_SEP = "═" * 62

_SQL_CAP_PER_CASE = 3000  # 每筆 case SQL 注入上限（字元）


@dataclass
class GenerationResult:
    candidate_tables: list[str]
    all_tables: list[str]
    step_a_sql: str
    step_a_reasoning: str
    final_analysis: str      # Step B：與參考案例的比對差異（items 1-3）
    final_reasoning: str     # Step B：最終 SQL 設計決策與原因（item 4，獨立欄位）
    final_sql: str
    injected_summary: dict = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0


# ── Schema 載入 ────────────────────────────────────────────────────

def _load_schema_for_tables(table_names: list[str]) -> str:
    """從 schema.csv 取出指定表格的欄位，格式化為 prompt 用文字。
    若 code_mapping.json 有該欄位的代碼對照（≤30 種），附加在欄位說明後面。
    """
    table_set = {t.upper() for t in table_names}
    by_table: dict[str, list[dict]] = {}

    with open(SCHEMA_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tname = row.get("表格名稱", "").upper()
            if tname in table_set:
                by_table.setdefault(tname, []).append(row)

    if not by_table:
        return "（找不到相關表格定義）"

    col_code_map = _get_col_codes()

    lines: list[str] = []
    for tname in sorted(by_table):
        rows = by_table[tname]
        tcn = rows[0].get("表格中文名稱", "")
        lines.append(f"\n【{tname}】{'（' + tcn + '）' if tcn else ''}")
        for row in rows:
            col = row.get("欄位名稱", "")
            col_cn = row.get("欄位中文名稱", "")
            defn = row.get("欄位定義說明", "")
            pk = "PK  " if row.get("Primary Key", "") == "PK" else "    "
            col_codes = col_code_map.get(col.upper(), {})
            already_in_defn = sum(1 for k in col_codes if k in defn) >= 2
            all_same_desc = len(set(col_codes.values())) <= 1
            code_hint = (
                f"  {_fmt_codes(col_codes)}"
                if 0 < len(col_codes) <= 30
                and not already_in_defn
                and not all_same_desc
                else ""
            )
            lines.append(f"  {pk}{col}（{col_cn}）：{defn}{code_hint}")
    return "\n".join(lines)


# ── Metrics 載入 ──────────────────────────────────────────────────

def _load_metrics_text() -> str:
    """載入 metrics.json 並格式化為 prompt 用文字。"""
    if not METRICS_PATH.exists():
        return ""
    with open(METRICS_PATH, encoding="utf-8") as f:
        metrics = json.load(f)

    lines = ["【業務指標計算規則】"]
    for m in metrics:
        name = m.get("name", "")
        expr = m.get("expression", "")
        instr = m.get("llm_instruction", "")
        lines.append(f"\n▸ {name}：{expr}")
        if instr:
            lines.append(f"  → {instr}")
    return "\n".join(lines)


# ── Business Skills 載入 ──────────────────────────────────────────

def _load_business_skills_text(query: str, scene: str = "") -> str:
    """依場景名稱或關鍵字觸發 business_skills.json 規則，組成 prompt 文字。"""
    if not BUSINESS_SKILLS_PATH.exists():
        return ""
    with open(BUSINESS_SKILLS_PATH, encoding="utf-8") as f:
        skills: list[dict] = json.load(f)

    triggered: list[str] = []
    query_lower = query.lower()
    for skill in skills:
        scenes: list[str] = skill.get("trigger_scenes", [])
        keywords: list[str] = skill.get("trigger_keywords", [])
        scene_hit = scene and any(s == scene for s in scenes)
        keyword_hit = any(kw.lower() in query_lower for kw in keywords)
        if scene_hit or keyword_hit:
            triggered.append(f"▸ [{skill['name']}]\n{skill['rule']}")

    if not triggered:
        return ""
    return "【業務技能規則（請務必遵守）】\n\n" + "\n\n".join(triggered)


# ── Relationships 載入 ─────────────────────────────────────────────

def _get_relationship_pairs(table_set: set[str] | None = None) -> list[tuple[str, str]]:
    """直接從 JSON 取 (table_a, table_b) 對，用於 injected_summary。避免解析格式化文字。"""
    if not RELATIONSHIPS_PATH.exists():
        return []
    with open(RELATIONSHIPS_PATH, encoding="utf-8") as f:
        rels = json.load(f)
    upper_set = {t.upper() for t in table_set} if table_set else None
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for r in rels:
        if r.get("inferred"):
            continue
        ta, tb = r["table_a"].upper(), r["table_b"].upper()
        if upper_set is not None and not (ta in upper_set and tb in upper_set):
            continue
        key = (ta, tb)
        if key not in seen:
            seen.add(key)
            pairs.append((r["table_a"], r["table_b"]))
    return pairs


def _load_relationships_text(table_set: set[str] | None = None) -> str:
    """載入 relationships.json，只保留 table_a 與 table_b 都在 table_set 內的關聯。
    table_set 為 None 時不過濾（全部載入）。
    """
    if not RELATIONSHIPS_PATH.exists():
        return ""
    with open(RELATIONSHIPS_PATH, encoding="utf-8") as f:
        rels = json.load(f)

    upper_set = {t.upper() for t in table_set} if table_set else None

    lines = ["【表格關聯關係（JOIN 參考）】"]
    kept = 0
    for r in rels:
        if r.get("inferred"):
            continue
        ta, tb = r["table_a"].upper(), r["table_b"].upper()
        if upper_set is not None and not (ta in upper_set and tb in upper_set):
            continue
        jt = r.get("join_type", "JOIN")
        cond = r.get("condition", "")
        note = r.get("join_note", "")
        variants = r.get("condition_variants") or {}
        lines.append(f"  {r['table_a']}  {jt}  {r['table_b']}")
        if cond:
            lines.append(f"    ON {cond}")
        if note:
            lines.append(f"    ※ {note}")
        for v_cond, v_desc in variants.items():
            lines.append(f"    或 ON {v_cond}  [{v_desc}]")
        kept += 1

    if kept == 0:
        return ""
    return "\n".join(lines)


# ── Case 工具 ──────────────────────────────────────────────────────

def _get_union_tables(
    hits: list[RetrievalHit],
    all_cases: list[dict],
    available: set[str],
) -> list[str]:
    """取 Top-5 檢索案例的 SQL tables 聯集。"""
    from .eval_table_selection import _extract_truth_tables
    case_map = {str(c.get("資料夾")): c for c in all_cases}
    union: set[str] = set()
    for hit in hits:
        union |= _extract_truth_tables(case_map.get(hit.case_id, {}), available)
    return sorted(union)


def _get_case_sql_text(case_id: str, all_cases: list[dict], cap: int = _SQL_CAP_PER_CASE) -> str:
    """取某個 case 的 SQL 文字（多個 SQL 檔合併，超長截斷）。"""
    case = next((c for c in all_cases if str(c.get("資料夾")) == case_id), None)
    if not case:
        return ""
    parts = []
    for s in (case.get("SQL") or []):
        fn = s.get("檔名", "")
        content = s.get("內容", "").strip()
        if content:
            parts.append(f"-- [{fn}]\n{content}")
    full = "\n\n".join(parts)
    if len(full) > cap:
        return full[:cap] + f"\n... （截斷，原長 {len(full)} 字元）"
    return full


# ── Step A ─────────────────────────────────────────────────────────

_STEP_A_SYSTEM = """\
你是一位 Oracle SQL 專家，熟悉台灣金融業的報表邏輯與資料倉儲設計。
請根據報表需求，從候選表格中選出適合的表格，寫出完整可執行的 Oracle SQL。
SQL 中不要放入假設性資料或 placeholder，所有欄位必須來自提供的表格定義。
遇到「本月」「上個月」「今年」等相對日期，請依據今日日期換算成正確的絕對日期區間。

【Schema 規則】
所有表格一律加上 DM_S_VIEW schema 前綴（例如 DM_S_VIEW.M_AC_ACCOUNT），
唯一例外：表格名稱本身已含有 schema 前綴（例如 S_MELODYJJJIAN.CUSTOMER_GROUP_2026），則保持原樣不做修改。

【Oracle 語法與效能】
語法正確性（嚴格遵守）：
- 使用 Oracle 19c+ 語法，禁用其他資料庫方言（MySQL 的 LIMIT、PostgreSQL 的 ILIKE 等）。
- 取前 N 筆：FETCH FIRST N ROWS ONLY 或 ROWNUM，不使用 LIMIT。
- 日期函數：TO_DATE()、TRUNC()、ADD_MONTHS()、LAST_DAY()；字串函數：NVL()、DECODE()、SUBSTR()。
- NULL 處理：NVL() 或 IS NULL / IS NOT NULL，避免直接用 = NULL。

效能（每條都須主動考量）：
- WHERE 先過濾高基數索引欄位（日期範圍、帳號、分公司代碼），縮小掃描範圍後再 JOIN。
- 同一大表多次存取時，以 CTE（WITH ... AS）或 inline view 確保只掃描一次。
- 排名、累計、移動平均等分析需求一律用視窗函數（ROW_NUMBER() / RANK() / SUM() OVER(...)），禁止用效能差的關聯子查詢替代。
- 避免在 WHERE 或 JOIN 條件的索引欄位上套函數（如 TRUNC(date_col) = ...），應改寫為範圍條件。"""


def _step_a(
    requirement: str,
    schema_text: str,
    relationships_text: str,
    metrics_text: str,
    model: str,
    entities_text: str = "",
    skills_text: str = "",
    today: str = "",
    report_plan_text: str = "",
) -> tuple[str, str, int, int]:
    """Step A：LLM 從候選池自由生成 SQL + 思路。回傳 (sql, reasoning, in_tok, out_tok)。"""
    optional_blocks = []
    if report_plan_text:
        optional_blocks.append(report_plan_text)
    if entities_text:
        optional_blocks.append(entities_text)
    if skills_text:
        optional_blocks.append(skills_text)
    if metrics_text:
        optional_blocks.append(metrics_text)
    if relationships_text:
        optional_blocks.append(relationships_text)
    extra = ("\n\n" + "\n\n".join(optional_blocks)) if optional_blocks else ""

    user_prompt = f"""\
【報表需求】
{requirement}
{extra}

【候選表格欄位定義（來自語義相似案例的 union tables）】
{schema_text}

請依以下格式輸出：

--- SQL ---
（完整 Oracle SQL）

--- 思路 ---
（你選了哪些表格及原因、JOIN 條件、時間篩選、聚合邏輯）"""

    system = (f"今日日期：{today}\n\n" + _STEP_A_SYSTEM) if today else _STEP_A_SYSTEM
    resp = _chat(
        model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )
    raw = resp.choices[0].message.content or ""
    in_tok = resp.usage.prompt_tokens
    out_tok = resp.usage.completion_tokens

    sql, reasoning = "", ""
    if "--- SQL ---" in raw and "--- 思路 ---" in raw:
        after_sql = raw.split("--- SQL ---", 1)[1]
        sql = after_sql.split("--- 思路 ---", 1)[0].strip()
        reasoning = after_sql.split("--- 思路 ---", 1)[1].strip()
    else:
        sql = raw.strip()

    return sql, reasoning, in_tok, out_tok


# ── Step B ─────────────────────────────────────────────────────────

_STEP_B_SYSTEM = """\
你是一位 Oracle SQL 審查員，熟悉台灣金融業報表邏輯。
你在第一輪已寫了一份 SQL，現在需要對照歷史參考案例與欄位定義來檢視並改進。
⚠️ 參考案例是語義相似度檢索結果，不一定完全符合本次需求，也不代表最佳寫法，僅供參考。
最終判斷依據是【報表需求】和【欄位定義】，而非參考案例。"""


def _step_b(
    requirement: str,
    step_a_sql: str,
    step_a_reasoning: str,
    hits: list[RetrievalHit],
    all_cases: list[dict],
    schema_text: str,
    model: str,
) -> tuple[str, str, str, int, int]:
    """Step B：對照 top-5 案例 SQL 自我批判，輸出最終 SQL。
    回傳 (analysis, final_reasoning, sql, in_tok, out_tok)。
    analysis      = 與參考案例的比對（items 1-3）
    final_reasoning = 最終 SQL 設計決策與原因（item 4）
    """
    case_map = {str(c.get("資料夾")): c for c in all_cases}

    case_blocks: list[str] = []
    for hit in hits:
        case = case_map.get(hit.case_id, {})
        req_summary = (case.get("需求") or {}).get("需求摘要", "")[:80]
        sql_text = _get_case_sql_text(hit.case_id, all_cases)
        case_blocks.append(
            f"=== 參考案例 [{hit.case_id}]（相似度 {hit.score:.4f}）===\n"
            f"需求摘要：{req_summary}\n\n"
            f"{sql_text}"
        )
    case_sqls_text = "\n\n".join(case_blocks)

    user_prompt = f"""\
【報表需求】
{requirement}

【第一輪 SQL】
{step_a_sql}

【第一輪思路】
{step_a_reasoning}

【參考案例（語義相似，僅供參考）】
{case_sqls_text}

【所有相關表格欄位定義】
{schema_text}

請依以下格式輸出：

--- 分析 ---
（比較第一輪思路與參考案例：
  1. 表格選擇是否一致或有差異？
  2. JOIN 條件有無不同？
  3. 篩選條件、聚合邏輯有無可改進？）

--- 最終思路 ---
（說明這份 SQL的完整設計決策，為何能符合使用者需求：選了哪些表格、JOIN 條件、時間篩選、聚合邏輯，使用者的需求的核心目標是什麼、這樣的寫法如何回應它、哪些設計決策是為了滿足哪個需求點）

--- 最終 SQL ---
（最終版完整 Oracle SQL）"""

    resp = _chat(
        model,
        messages=[
            {"role": "system", "content": _STEP_B_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )
    raw = resp.choices[0].message.content or ""
    in_tok = resp.usage.prompt_tokens
    out_tok = resp.usage.completion_tokens

    analysis, final_reasoning, final_sql = "", "", ""
    if "--- 分析 ---" in raw:
        after_analysis = raw.split("--- 分析 ---", 1)[1]
        if "--- 最終思路 ---" in after_analysis:
            analysis = after_analysis.split("--- 最終思路 ---", 1)[0].strip()
            after_reasoning = after_analysis.split("--- 最終思路 ---", 1)[1]
            if "--- 最終 SQL ---" in after_reasoning:
                final_reasoning = after_reasoning.split("--- 最終 SQL ---", 1)[0].strip()
                final_sql = after_reasoning.split("--- 最終 SQL ---", 1)[1].strip()
            else:
                final_sql = after_reasoning.strip()
        elif "--- 最終 SQL ---" in after_analysis:
            analysis = after_analysis.split("--- 最終 SQL ---", 1)[0].strip()
            final_sql = after_analysis.split("--- 最終 SQL ---", 1)[1].strip()
    else:
        final_sql = raw.strip()

    return analysis, final_reasoning, final_sql, in_tok, out_tok


# ── 主入口 ─────────────────────────────────────────────────────────

def generate(
    requirement: str,
    hits: list[RetrievalHit],
    all_cases: list[dict],
    model: str = GENERATION_MODEL,
    scene: str = "",
    report_plan_text: str = "",
) -> GenerationResult:
    """完整生成流程：Step A（草稿）→ Step B（自我批判 + 最終 SQL）。"""
    from .schema_summarizer import load_table_summaries
    from .eval_table_selection import _extract_truth_tables
    from .entity_extractor import extract_entities

    available = set(load_table_summaries().keys())
    case_map = {str(c.get("資料夾")): c for c in all_cases}

    # ── 實體擷取：商品/概念/分公司 → extra_tables + 提示文字 ────────
    extraction = extract_entities(requirement)
    if extraction.detected_products or extraction.detected_concepts or extraction.detected_branches:
        print(f"\n{SEP}")
        print("=== 實體擷取 ===")
        if extraction.detected_products:
            print(f"  商品：{', '.join(extraction.detected_products)}")
        if extraction.detected_concepts:
            print(f"  概念：{', '.join(extraction.detected_concepts)}")
        if extraction.detected_branches:
            print(f"  分公司：{', '.join(extraction.detected_branches)}")
        if extraction.extra_tables:
            extra_in_available = [t for t in extraction.extra_tables if t in available]
            print(f"  追加候選表格：{', '.join(extra_in_available) or '（無新增）'}")
        if extraction.codes:
            print(f"  WHERE 提示：{extraction.codes}")

    # ── 候選池：top-5 union tables ∪ entity extra_tables ──────────
    candidate_tables = _get_union_tables(hits, all_cases, available)
    candidate_set = set(candidate_tables)
    for t in extraction.extra_tables:
        if t in available:
            candidate_set.add(t)
    candidate_tables = sorted(candidate_set)

    rels_text = _load_relationships_text(table_set=candidate_set)
    metrics_text = _load_metrics_text()
    skills_text = _load_business_skills_text(requirement, scene)
    step_a_schema = _load_schema_for_tables(candidate_tables)

    rel_count = rels_text.count("\n  ") if rels_text else 0
    skills_count = skills_text.count("▸ [") if skills_text else 0

    print(f"\n{SEP}")
    print(f"=== Step A：候選池草稿生成（模型：{model}）===")
    print(f"  候選表格（{len(candidate_tables)} 張）：{', '.join(candidate_tables)}")
    print(f"  注入 relationships：{rel_count} 條（已依候選池過濾）")
    print(f"  注入 metrics：全部 {len([l for l in metrics_text.splitlines() if l.startswith('▸')])} 條")
    if skills_count:
        print(f"  注入 business_skills：{skills_count} 條（場景={scene or '—'}）")

    from datetime import date as _date
    today = _date.today().strftime("%Y/%m/%d")
    step_a_sql, step_a_reasoning, a_in, a_out = _step_a(
        requirement, step_a_schema, rels_text, metrics_text, model,
        entities_text=extraction.enriched_entities,
        skills_text=skills_text,
        today=today,
        report_plan_text=report_plan_text,
    )
    print(f"  tokens：in={a_in}  out={a_out}")
    print(f"\n{step_a_sql[:400]}{'...' if len(step_a_sql) > 400 else ''}")

    # ── Step B：擴展 schema，加入 top-5 案例也有用到的表格 ──────────
    all_hit_tables: set[str] = set(candidate_tables)
    for hit in hits:
        all_hit_tables |= _extract_truth_tables(case_map.get(hit.case_id, {}), available)
    all_tables = sorted(all_hit_tables)
    step_b_schema = _load_schema_for_tables(all_tables)

    print(f"\n{SEP}")
    print(f"=== Step B：自我批判（注入 {len(hits)} 筆參考案例）===")
    print(f"  完整表格範圍（{len(all_tables)} 張）：{', '.join(all_tables)}")

    analysis, final_reasoning, final_sql, b_in, b_out = _step_b(
        requirement, step_a_sql, step_a_reasoning, hits, all_cases, step_b_schema, model
    )
    print(f"  tokens：in={b_in}  out={b_out}")

    print(f"\n{WIDE_SEP}")
    print("=== 最終 SQL ===")
    print(final_sql)
    print(WIDE_SEP)

    # ── 整理注入內容摘要 ──────────────────────────────────────────
    import re as _re
    skill_names = _re.findall(r"▸ \[([^\]]+)\]", skills_text)
    metric_names = _re.findall(r"▸ (.+?)：", metrics_text)
    rel_pairs = _get_relationship_pairs(table_set=candidate_set)

    injected_summary = {
        "today": today,
        "entities": {
            "products":  extraction.detected_products,
            "concepts":  extraction.detected_concepts,
            "branches":  extraction.detected_branches,
            "extra_tables": [t for t in extraction.extra_tables if t in available],
            "codes":     extraction.codes,
        },
        "skills":        skill_names,
        "metrics":       metric_names,
        "relationships": rel_pairs,
    }

    price_in, price_out = get_model_pricing(model)
    gen_cost = (a_in + b_in) / 1_000_000 * price_in + (a_out + b_out) / 1_000_000 * price_out

    return GenerationResult(
        candidate_tables=candidate_tables,
        all_tables=all_tables,
        step_a_sql=step_a_sql,
        step_a_reasoning=step_a_reasoning,
        final_analysis=analysis,
        final_reasoning=final_reasoning,
        final_sql=final_sql,
        tokens={"step_a_in": a_in, "step_a_out": a_out, "step_b_in": b_in, "step_b_out": b_out},
        injected_summary=injected_summary,
        cost_usd=gen_cost,
    )
