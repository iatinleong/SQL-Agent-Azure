"""一次性批次作業：用 LLM 把每個歷史案例總結成業務邏輯摘要，存入 case_summaries/<id>.txt。

摘要用途：向量化後作為 Phase 3 的檢索文件，讓業務員的自然語言需求能比對到最相關的歷史案例。
摘要必須用業務語言描述，不帶 SQL 語法，且時間範圍只寫概念不寫具體數字。
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import ALL_CASES_PATH, BASE_DIR, CLASSIFICATION_MODEL, CLASSIFICATION_REASONING_EFFORT, openai_client

SUMMARIES_DIR: Path = BASE_DIR / "case_summaries"

_SYSTEM_PROMPT = """\
你是一位熟悉金融業務的報表分析師，能從 SQL 程式碼中讀懂背後的業務邏輯。

【背景說明】
我們在建立一套智能報表系統：當業務員用自然語言描述報表需求時，系統會從歷史案例庫中找出最相似的歷史案例，再根據這些案例生成對應的 Oracle SQL。

相似度比對是靠「向量搜尋」——把每個歷史案例的業務摘要向量化，再拿業務員的新需求去比對。
因此摘要必須用業務員會說的語言描述，才能被正確比對到。

【資料說明】
每個案例包含兩個來源：
1. 業務員的需求文字（需求摘要、篩選條件、欄位）：業務員寫的，往往簡略甚至不完整
2. SQL 程式碼：工程師實作的，是這份報表真正業務邏輯的完整體現

請以 SQL 為主要依據來理解業務邏輯，需求文字僅作為輔助參考。

【任務】
閱讀 SQL 程式碼，理解它在做什麼業務事情，寫一段 100–150 字的業務邏輯摘要。

【核心寫作原則】

✅ 應該描述的內容：
1. 這份報表要解決什麼業務問題（目的）
2. 圈選對象是誰（哪類客戶、帳戶、商品別、分公司）
3. 篩選邏輯的業務概念（例如：有庫存、曾交易、已離職、前N大、未簽署等條件）
4. 輸出的統計維度與結構（按客戶/按營業員/按商品/跨期比較，看哪些數字）
5. 業務場景關鍵詞（行銷名單、月報、靜止戶、市佔率、人員異動、動能追蹤等）

❌ 絕對不能出現的內容：
- 任何 SQL 語法或程式碼片段
- 英文欄位名（如 ACCT_NBR、EMP_NBR、PROD_TYPE_CODE 等）
- 具體年份或日期數字（如 2022、2025/03、202501）
  → 改用概念：「歷年比較」「多年期」「近期數月」「指定期間」「當年度」「月均」
- 具體 Top-N 數值（如 Top-50、前20名）
  → 改用：「前N大客戶」「高交易量客戶」

【格式】
- 連貫的段落文字，不要條列式
- 100–150 字
"""


def _build_user_prompt(case: dict) -> str:
    req = case.get("需求", {}) if isinstance(case.get("需求"), dict) else {}
    biz = case.get("業務場景", {}) if isinstance(case.get("業務場景"), dict) else {}

    scene = biz.get("業務場景", "（未標記）")
    req_summary = req.get("需求摘要", "（無）")
    filters = req.get("篩選條件", [])
    fields = req.get("欄位", [])

    filters_text = "、".join(str(f) for f in filters) if filters else "（未指定）"
    fields_text = "、".join(str(f) for f in fields) if fields else "（未指定）"

    sql_parts = case.get("SQL", [])
    sql_text = "\n\n".join(s.get("內容", "") for s in sql_parts if s.get("內容", "").strip())

    return f"""\
【業務員需求（供參考，可能簡略）】
業務場景：{scene}
需求摘要：{req_summary}
篩選條件：{filters_text}
輸出欄位：{fields_text}

【SQL 程式碼（主要依據，完整呈現業務邏輯）】
{sql_text}

需求摘要是營業員的簡略需求，可是有時候他們講的就很籠統簡略，所以應該以上方 SQL為主要依據，需求摘要為輔，寫出這個報表案例的業務邏輯摘要。"""


def summarize_case(case: dict) -> str:
    """呼叫 LLM 產出單一案例的業務摘要。"""
    response = openai_client.chat.completions.create(
        model=CLASSIFICATION_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(case)},
        ],
        max_completion_tokens=8000,
        reasoning_effort=CLASSIFICATION_REASONING_EFFORT,
    )
    return response.choices[0].message.content.strip()


def get_summary_path(case_id: str) -> Path:
    return SUMMARIES_DIR / f"{case_id}.txt"


def load_summaries() -> dict[str, str]:
    """從 case_summaries/ 目錄載入所有摘要，回傳 {case_id: summary_text}。"""
    if not SUMMARIES_DIR.exists():
        return {}
    return {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(SUMMARIES_DIR.glob("*.txt"))
    }


def build_summaries(
    case_ids: list[str] | None = None,
    force: bool = False,
) -> dict[str, str]:
    """
    批次產出案例業務摘要，存為 case_summaries/<id>.txt。

    Args:
        case_ids: 指定只跑哪幾個案例（None = 全部）
        force:    True 時覆蓋已存在的摘要
    """
    SUMMARIES_DIR.mkdir(exist_ok=True)

    with open(ALL_CASES_PATH, encoding="utf-8") as f:
        all_cases: list[dict] = json.load(f)

    if case_ids is not None:
        target_ids = set(case_ids)
        all_cases = [c for c in all_cases if str(c.get("資料夾")) in target_ids]

    total = len(all_cases)
    results: dict[str, str] = {}

    for i, case in enumerate(all_cases, 1):
        case_id = str(case.get("資料夾", i))
        path = get_summary_path(case_id)

        if path.exists() and not force:
            print(f"  [{i:3}/{total}] 案例 {case_id} 已存在，跳過")
            results[case_id] = path.read_text(encoding="utf-8")
            continue

        req_summary = (case.get("需求") or {}).get("需求摘要", "")
        print(f"  [{i:3}/{total}] 案例 {case_id}：{req_summary[:40]}...")
        summary = summarize_case(case)
        path.write_text(summary, encoding="utf-8")
        results[case_id] = summary
        print(f"           → {summary[:60]}...")

    new_count = sum(1 for c in all_cases if not get_summary_path(str(c.get("資料夾"))).exists() or force)
    print(f"\n完成。共處理 {total} 筆 → {SUMMARIES_DIR}")
    return results
