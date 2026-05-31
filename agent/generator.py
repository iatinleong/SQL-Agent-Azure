"""SQL 生成：Phase 2 檢索後，Step A（候選池草稿）→ Step C（全套驗證 + 自動修正）。"""

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
    step_a_sql: str
    step_a_reasoning: str
    final_reasoning: str     # = step_a_reasoning，供 UI「SQL 思路」expander 顯示
    final_sql: str
    step_c_log: list[dict] = field(default_factory=list)   # Step C：全套驗證迭代記錄
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

def _select_metrics(query: str = "") -> list[dict]:
    """回傳 trigger_keywords routing 後命中的 metric dicts；無命中時 fallback 全量。"""
    if not METRICS_PATH.exists():
        return []
    with open(METRICS_PATH, encoding="utf-8") as f:
        metrics = json.load(f)
    if not query:
        return metrics
    q = query.lower()
    matched = [m for m in metrics if any(kw.lower() in q for kw in m.get("trigger_keywords", []))]
    return matched if matched else metrics


def _format_metrics_text(selected: list[dict]) -> str:
    if not selected:
        return ""
    lines = ["【業務指標計算規則】"]
    for m in selected:
        lines.append(f"\n▸ {m.get('name', '')}：{m.get('expression', '')}")
        if m.get("llm_instruction"):
            lines.append(f"  → {m['llm_instruction']}")
    return "\n".join(lines)


def _load_metrics_text(query: str = "") -> str:
    """載入 metrics.json，依 trigger_keywords routing 只注入命中的指標。
    完全沒命中時 fallback 全量注入。
    """
    return _format_metrics_text(_select_metrics(query))


# ── Business Skills 載入 ──────────────────────────────────────────

def _select_skills(query: str = "", scene: str = "") -> list[dict]:
    """回傳 trigger_keywords / scene routing 後命中的 skill dicts。"""
    if not BUSINESS_SKILLS_PATH.exists():
        return []
    with open(BUSINESS_SKILLS_PATH, encoding="utf-8") as f:
        skills: list[dict] = json.load(f)
    q = query.lower()
    return [
        s for s in skills
        if (scene and any(sc == scene for sc in s.get("trigger_scenes", [])))
        or any(kw.lower() in q for kw in s.get("trigger_keywords", []))
    ]


def _format_skills_text(triggered: list[dict]) -> str:
    if not triggered:
        return ""
    parts = [f"▸ [{s['name']}]\n{s['rule']}" for s in triggered]
    return "【業務技能規則（請務必遵守）】\n\n" + "\n\n".join(parts)


def _load_business_skills_text(query: str, scene: str = "") -> str:
    """依場景名稱或關鍵字觸發 business_skills.json 規則，組成 prompt 文字。"""
    return _format_skills_text(_select_skills(query, scene))


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


# ── 參考案例文字 ───────────────────────────────────────────────────

def _build_cases_text(
    hits: list[RetrievalHit],
    all_cases: list[dict],
) -> str:
    """格式化 Top-5 案例 SQL（含 case_id 與相似度分數）供 Step A 注入，僅供參考。"""
    if not hits:
        return ""
    case_map = {str(c.get("資料夾")): c for c in all_cases}
    blocks: list[str] = []
    for hit in hits:
        case = case_map.get(hit.case_id, {})
        req_summary = (case.get("需求") or {}).get("需求摘要", "")[:80]
        sql_text = _get_case_sql_text(hit.case_id, all_cases)
        blocks.append(
            f"=== 案例 [{hit.case_id}]（相似度 {hit.score:.4f}）===\n"
            f"需求摘要：{req_summary}\n\n"
            f"{sql_text}"
        )
    header = (
      "【參考案例 SQL】\n"
      "以下案例由語義相似度檢索得出，供了解欄位命名風格與 SQL 結構參考，"
      "不代表邏輯正確。請以本次提供的表格定義、指標規則、業務邏輯為準。"
  )
    return header + "\n\n" + "\n\n".join(blocks)


# ── Step A ─────────────────────────────────────────────────────────

_STEP_A_SYSTEM = """\
你是一位 Oracle SQL 專家，熟悉台灣金融業的報表邏輯與資料倉儲設計。
請根據報表需求，從候選表格中選出適合的表格，寫出完整可執行的 Oracle SQL。
SQL 中不要放入假設性資料或 placeholder，所有欄位必須來自提供的表格定義。
遇到「本月」「上個月」「今年」等相對日期，請依據今日日期換算成正確的絕對日期區間。

【Oracle 語法與效能】
語法正確性（嚴格遵守）：
- 使用 Oracle 19c+ 語法，禁用其他資料庫方言（MySQL 的 LIMIT、PostgreSQL 的 ILIKE 等）。
- 取前 N 筆：FETCH FIRST N ROWS ONLY 或 ROWNUM，不使用 LIMIT。僅在使用者明確要求筆數限制（例如「前10名」「Top 50」）時才加；使用者未提及時絕對不要自行加上任何 FETCH FIRST 或 ROWNUM 限制。
- 日期函數：TO_DATE()、TRUNC()、ADD_MONTHS()、LAST_DAY()；字串函數：NVL()、DECODE()、SUBSTR()。
- NULL 處理：NVL() 或 IS NULL / IS NOT NULL，避免直接用 = NULL。
- 沒有實際資料表的 SELECT 必須加 FROM DUAL。

效能（每條都須主動考量）：
- WHERE 先過濾高基數索引欄位（日期範圍、帳號、分公司代碼），縮小掃描範圍後再 JOIN。
- 同一大表多次存取時，以 CTE（WITH ... AS）或 inline view 確保只掃描一次。
- 排名、累計、移動平均等分析需求一律用視窗函數（ROW_NUMBER() / RANK() / SUM() OVER(...)），禁止用效能差的關聯子查詢替代。
- 避免在 WHERE 或 JOIN 條件的索引欄位上套函數（如 TRUNC(date_col) = ...），應改寫為範圍條件。

【資料模型規則】
- 帳戶唯一識別為複合鍵：acct_nbr + prod_type_code（同一 acct_nbr 在不同商品類別下是不同帳戶）。
- 任何兩張表以帳戶關聯時，JOIN 條件必須同時包含 prod_type_code 與 acct_nbr；只用 acct_nbr 會導致一對多膨脹。
- 個人層級以 party_id 識別；帳戶層級以 acct_nbr + prod_type_code 識別。層級由高到低：分公司 → 個人(party_id) → 帳戶(acct_nbr + prod_type_code)。

【欄位別名】
最外層 SELECT 的每個欄位都加中文別名，例如：acct_nbr AS "帳號"、COUNT(*) AS "筆數"。"""


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
    cases_text: str = "",
    user_profile: str = "",
) -> tuple[str, str, int, int]:
    """Step A：LLM 從候選池生成 SQL + 思路（附參考案例）。回傳 (sql, reasoning, in_tok, out_tok)。"""
    optional_blocks = []
    if user_profile.strip():
        optional_blocks.append(
            "【使用者個人化注意事項（來自歷史對話）】\n"
            "以下根據此使用者過去的查詢習慣整理，僅供參考。\n"
            "請依本次需求判斷哪些條目適用，與本次查詢無關的請忽略。\n"
            + user_profile.strip()
        )
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
    if cases_text:
        optional_blocks.append(cases_text)
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
（你選了哪些表格及原因、JOIN 條件、時間篩選、聚合邏輯；若有參考案例的寫法，說明參考了哪些）"""

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


# ── 主入口 ─────────────────────────────────────────────────────────

def generate(
    requirement: str,
    hits: list[RetrievalHit],
    all_cases: list[dict],
    model: str = GENERATION_MODEL,
    scene: str = "",
    report_plan_text: str = "",
    extra_context: str = "",
    user_profile: list[dict] | None = None,
) -> GenerationResult:
    """完整生成流程：Step A（草稿）→ Step B（全套驗證 + 自動修正）。
    extra_context：Q&A 確認後的最終需求補充文字，與原始 requirement union 做 metrics/skills 提取。
    """
    from .schema_summarizer import load_table_summaries
    from .entity_extractor import extract_entities

    available = set(load_table_summaries().keys())
    case_map = {str(c.get("資料夾")): c for c in all_cases}

    # ── 實體擷取：對 combined text（原始 + extra_context）──────────
    from .table_retriever import retrieve_tables
    extraction_text = (requirement + "\n" + extra_context).strip() if extra_context else requirement
    extraction = extract_entities(extraction_text)
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

    # ── 候選池：case union ∪ table embedding 檢索（兩次）∪ entity extra_tables ──
    candidate_tables = _get_union_tables(hits, all_cases, available)
    candidate_set = set(candidate_tables)

    semantic_a = retrieve_tables(requirement, top_n=5)
    semantic_b = retrieve_tables(extra_context, top_n=5) if extra_context else []
    semantic_tables = list(dict.fromkeys(semantic_a + semantic_b))
    new_from_semantic = [t for t in semantic_tables if t in available and t not in candidate_set]
    if new_from_semantic:
        print(f"\n{SEP}")
        print(f"=== 表格語意檢索（新增）：{', '.join(new_from_semantic)} ===")
    candidate_set.update(t for t in semantic_tables if t in available)

    for t in extraction.extra_tables:
        if t in available:
            candidate_set.add(t)
    candidate_tables = sorted(candidate_set)

    rels_text = _load_relationships_text(table_set=set(candidate_tables))

    # ── Metrics union：原始需求 ∪ extra_context ────────────────────
    metrics_orig = _select_metrics(requirement)
    metrics_extra = _select_metrics(extra_context) if extra_context else []
    seen_m = {m["name"] for m in metrics_orig}
    metrics_new = [m for m in metrics_extra if m["name"] not in seen_m]
    metrics_union = metrics_orig + metrics_new
    metrics_text = _format_metrics_text(metrics_union)

    # ── Skills union：原始需求 ∪ extra_context ─────────────────────
    skills_orig = _select_skills(requirement, scene)
    skills_extra = _select_skills(extra_context, scene) if extra_context else []
    seen_s = {s["name"] for s in skills_orig}
    skills_new_list = [s for s in skills_extra if s["name"] not in seen_s]
    skills_union = skills_orig + skills_new_list
    skills_text = _format_skills_text(skills_union)

    step_a_schema = _load_schema_for_tables(candidate_tables)

    rel_count = rels_text.count("\n  ") if rels_text else 0
    _m_total = sum(1 for _ in open(METRICS_PATH, encoding="utf-8") if '"name"' in _)
    _m_mode = "routing" if len(metrics_union) < _m_total else "fallback 全量"

    print(f"\n{SEP}")
    print(f"=== Step A：候選池草稿生成（模型：{model}，注入 {len(hits)} 筆參考案例）===")
    print(f"  候選表格（{len(candidate_tables)} 張）：{', '.join(candidate_tables)}")
    print(f"  注入 relationships：{rel_count} 條（已依候選池過濾）")
    print(f"  注入 metrics：{len(metrics_union)}/{_m_total} 條（{_m_mode}）"
          + (f"，其中 {len(metrics_new)} 條來自最終確認" if metrics_new else ""))
    if skills_union:
        print(f"  注入 business_skills：{len(skills_union)} 條"
              + (f"，其中 {len(skills_new_list)} 條來自最終確認" if skills_new_list else ""))

    from datetime import date as _date
    today = _date.today().strftime("%Y/%m/%d")
    from .user_profile import select_rules, format_rules_text
    _all_rules = user_profile or []
    _query_for_profile = " ".join(filter(None, [requirement, extra_context]))
    _matched_rules = select_rules(_query_for_profile, _all_rules)
    profile_text = format_rules_text(_matched_rules)

    cases_text = _build_cases_text(hits, all_cases)
    step_a_sql, step_a_reasoning, a_in, a_out = _step_a(
        requirement, step_a_schema, rels_text, metrics_text, model,
        entities_text=extraction.enriched_entities,
        skills_text=skills_text,
        today=today,
        report_plan_text=report_plan_text,
        cases_text=cases_text,
        user_profile=profile_text,
    )
    print(f"  tokens：in={a_in}  out={a_out}")
    print(f"\n{step_a_sql[:400]}{'...' if len(step_a_sql) > 400 else ''}")

    # ── Step C：全套驗證（語法 + schema prefix + 幻覺）+ 自動修正 ───
    from .sql_validator import validate_and_fix
    from .config import VALIDATOR_MODEL

    final_sql = step_a_sql
    print(f"\n{SEP}")
    print("=== Step B：SQL 驗證（語法 + schema prefix + 幻覺）===")
    final_sql, step_c_log, fix_tokens = validate_and_fix(
        final_sql, model=VALIDATOR_MODEL, max_iter=1
    )
    for entry in step_c_log:
        if entry["passed"]:
            print(f"  Round {entry['round']}：✅ 通過")
        else:
            print(f"  Round {entry['round']}：❌ {len(entry['errors'])} 個問題")
            for e in entry["errors"]:
                print(f"    {e}")

    print(f"\n{WIDE_SEP}")
    print("=== 最終 SQL ===")
    print(final_sql)
    print(WIDE_SEP)

    # ── 整理注入內容摘要 ──────────────────────────────────────────
    rel_pairs = _get_relationship_pairs(table_set=set(candidate_tables))

    injected_summary = {
        "today": today,
        "entities": {
            "products":  extraction.detected_products,
            "concepts":  extraction.detected_concepts,
            "branches":  extraction.detected_branches,
            "extra_tables": [t for t in extraction.extra_tables if t in available],
            "codes":     extraction.codes,
        },
        "metrics":       [m["name"] for m in metrics_union],
        "metrics_orig":  [m["name"] for m in metrics_orig],
        "metrics_new":   [m["name"] for m in metrics_new],
        "skills":        [s["name"] for s in skills_union],
        "skills_orig":   [s["name"] for s in skills_orig],
        "skills_new":    [s["name"] for s in skills_new_list],
        "relationships": rel_pairs,
        "user_profile":  profile_text,
        "user_profile_matched": [r.get("name") for r in _matched_rules],
    }

    price_in, price_out = get_model_pricing(model)
    clf_price_in, clf_price_out = get_model_pricing(VALIDATOR_MODEL)
    gen_cost = a_in / 1_000_000 * price_in + a_out / 1_000_000 * price_out
    fix_cost = (
        fix_tokens.get("fix_in", 0) / 1_000_000 * clf_price_in
        + fix_tokens.get("fix_out", 0) / 1_000_000 * clf_price_out
    )

    return GenerationResult(
        candidate_tables=candidate_tables,
        step_a_sql=step_a_sql,
        step_a_reasoning=step_a_reasoning,
        final_reasoning=step_a_reasoning,
        final_sql=final_sql,
        step_c_log=step_c_log,
        tokens={
            "step_a_in": a_in, "step_a_out": a_out,
            **fix_tokens,
        },
        injected_summary=injected_summary,
        cost_usd=gen_cost + fix_cost,
    )
