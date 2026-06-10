"""追問處理：意圖分類 + SQL 改寫。

意圖類型：
  ADD_TABLE    需要引入目前 SQL 沒有的新表格（加年齡、加配息資料等）
  REMOVE_TABLE 移除某個表格或欄位
  MODIFY_SQL   只修改 SQL 邏輯（WHERE、聚合、排序、時間範圍等），不新增表格
  NEW_QUERY    完全不同的新需求，應重新走完整 Phase1+2+StepA+StepB 流程
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import REFINER_MODEL, GENERATION_MODEL, REFINER_CLASSIFY_REASONING_EFFORT, REFINER_REFINE_REASONING_EFFORT, get_model_pricing
from .generator import _chat, _load_schema_for_tables

INTENTS = ("ADD_TABLE", "REMOVE_TABLE", "MODIFY_SQL", "NEW_QUERY")

_CLASSIFY_SYSTEM = """\
你是一個 SQL 需求分析助理。根據目前 SQL 和使用者的追問，判斷追問意圖。
只輸出 JSON，不要任何其他文字。"""

_REFINE_SYSTEM = """\
你是一位 Oracle SQL 專家，熟悉台灣金融業報表邏輯。
根據使用者的修改指令，改寫已有的 SQL，並說明改法與最終設計思路。

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


@dataclass
class RefineResult:
    intent: str
    target_tables: list[str] = field(default_factory=list)
    modification_note: str = ""
    new_reasoning: str = ""
    new_sql: str = ""
    classify_tokens: dict[str, int] = field(default_factory=dict)
    refine_tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0


def classify_followup(
    current_sql: str,
    new_query: str,
    available_tables: set[str],
    model: str = REFINER_MODEL,
) -> dict:
    """判斷追問意圖，回傳 {intent, target_tables, explanation}。"""
    table_sample = ", ".join(sorted(available_tables)[:60])
    prompt = f"""\
目前 SQL（節錄前 1500 字元）：
{current_sql[:1500]}

使用者追問：{new_query}

可用表格（部分列舉）：{table_sample}

請判斷追問意圖，輸出 JSON：
{{
  "intent": "ADD_TABLE|REMOVE_TABLE|MODIFY_SQL|NEW_QUERY",
  "target_tables": [],
  "explanation": "一句話說明"
}}

意圖定義：
- ADD_TABLE：需要引入目前 SQL 沒有的新表格（如加客戶年齡、加配息、加市佔率）
- REMOVE_TABLE：要移除某個表格、欄位或 JOIN
- MODIFY_SQL：只修改 SQL 邏輯（WHERE 條件、GROUP BY、排序、時間範圍、閾值等），不新增表格
- NEW_QUERY：完全不同的新需求，無法在現有 SQL 基礎上修改"""

    resp = _chat(
        model,
        messages=[
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        reasoning_effort=REFINER_CLASSIFY_REASONING_EFFORT,
    )
    raw = (resp.choices[0].message.content or "").strip().strip("```json").strip("```").strip()
    tokens = {
        "classify_in": resp.usage.prompt_tokens,
        "classify_out": resp.usage.completion_tokens,
    }
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"intent": "MODIFY_SQL", "target_tables": [], "explanation": raw}
    result["_tokens"] = tokens
    return result


def build_conversation_summary(conversation: list) -> str:
    """壓縮對話歷史為文字（避免 context 爆炸）。"""
    lines: list[str] = []
    for i, turn in enumerate(conversation, 1):
        sql_preview = turn.sql[:200] + "..." if len(turn.sql) > 200 else turn.sql
        lines.append(f"[Turn {i}] 需求：{turn.user_query}")
        if turn.modification:
            lines.append(f"         改法（{turn.intent}）：{turn.modification[:120]}")
        lines.append(f"         SQL（節錄）：{sql_preview}")
    return "\n".join(lines)


def refine(
    conversation_summary: str,
    current_sql: str,
    current_reasoning: str,
    new_query: str,
    classification: dict,
    model: str = GENERATION_MODEL,
) -> RefineResult:
    """改寫 SQL，回傳 RefineResult。"""
    intent = classification.get("intent", "MODIFY_SQL")
    target_tables: list[str] = classification.get("target_tables") or []
    classify_tokens = classification.get("_tokens", {})

    extra_schema = ""
    if intent == "ADD_TABLE" and target_tables:
        extra_schema = _load_schema_for_tables(target_tables)

    extra_block = f"\n\n【新增表格 Schema】\n{extra_schema}" if extra_schema else ""

    user_prompt = f"""\
【對話歷史摘要】
{conversation_summary}

【目前 SQL】
{current_sql}

【目前 SQL 思路】
{current_reasoning}

【使用者指令】
{new_query}
{extra_block}

請依以下格式輸出：

--- 改法 ---
（說明做了什麼改動，以及為何這樣改）

--- 最終思路 ---
（說明這份 SQL的完整設計決策，為何能符合使用者需求：選了哪些表格、JOIN 條件、時間篩選、聚合邏輯，使用者的需求的核心目標是什麼、這樣的寫法如何回應它、哪些設計決策是為了滿足哪個需求點）

--- 最終 SQL ---
（改寫後的完整 Oracle SQL）"""

    resp = _chat(
        model,
        messages=[
            {"role": "system", "content": _REFINE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        reasoning_effort=REFINER_REFINE_REASONING_EFFORT,
    )
    raw = resp.choices[0].message.content or ""
    refine_tokens = {
        "refine_in": resp.usage.prompt_tokens,
        "refine_out": resp.usage.completion_tokens,
    }

    modification_note, new_reasoning, new_sql = "", "", ""
    if "--- 改法 ---" in raw:
        after = raw.split("--- 改法 ---", 1)[1]
        if "--- 最終思路 ---" in after:
            modification_note = after.split("--- 最終思路 ---", 1)[0].strip()
            after2 = after.split("--- 最終思路 ---", 1)[1]
            if "--- 最終 SQL ---" in after2:
                new_reasoning = after2.split("--- 最終 SQL ---", 1)[0].strip()
                new_sql = after2.split("--- 最終 SQL ---", 1)[1].strip()
            else:
                new_sql = after2.strip()
        elif "--- 最終 SQL ---" in after:
            modification_note = after.split("--- 最終 SQL ---", 1)[0].strip()
            new_sql = after.split("--- 最終 SQL ---", 1)[1].strip()
    else:
        new_sql = raw.strip()

    clf_price_in, clf_price_out = get_model_pricing(REFINER_MODEL)
    clf_in = classify_tokens.get("classify_in", 0)
    clf_out = classify_tokens.get("classify_out", 0)
    clf_cost = clf_in / 1_000_000 * clf_price_in + clf_out / 1_000_000 * clf_price_out

    ref_price_in, ref_price_out = get_model_pricing(model)
    ref_in = refine_tokens.get("refine_in", 0)
    ref_out = refine_tokens.get("refine_out", 0)
    ref_cost = ref_in / 1_000_000 * ref_price_in + ref_out / 1_000_000 * ref_price_out

    return RefineResult(
        intent=intent,
        target_tables=target_tables,
        modification_note=modification_note,
        new_reasoning=new_reasoning,
        new_sql=new_sql,
        classify_tokens=classify_tokens,
        refine_tokens=refine_tokens,
        cost_usd=clf_cost + ref_cost,
    )
