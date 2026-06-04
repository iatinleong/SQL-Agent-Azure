"""報表結構規劃：在生成 SQL 前，透過對話確認報表需求細節。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import PLAN_MODEL
from .generator import _chat


@dataclass
class ReportPlan:
    status: str = "confirm"         # "ask" | "confirm"
    question: str = ""              # status="ask" 時，向使用者提的問題（一次一個）
    granularity: str = "其他"       # 帳戶/客戶/營業員/分公司/其他
    granularity_detail: str = ""    # 每列代表什麼（白話）
    understanding: str = ""         # status="confirm" 時，LLM 對整份報表需求的完整理解摘要
    tables: list = field(default_factory=list)  # status="confirm" 時，LLM 從 schema 選出實際需要的表格
    tokens: dict = field(default_factory=dict)


_SYSTEM = """\
你是一位台灣金融業的資深資料科學家，日常工作之一就是幫助營業員 query 他們想要的資料。
你非常熟悉金融業的數據結構與業務邏輯，也深刻理解營業員在日常工作中會需要什麼樣的報表。
你擅長從簡短的業務描述中，推敲出背後真正的數據需求，並判斷還有哪些關鍵資訊尚未釐清。
根據使用者的需求、歷史案例 SQL 與雙方對話記錄，判斷是否已有足夠資訊來生成正確的報表。
與使用者溝通時，只用業務員聽得懂的語言，盡量不要提及任何英文資料表名稱、欄位英文名稱或SQL用語。
只輸出 JSON，不要其他文字。"""


def plan_report(
    requirement: str,
    case_sqls: list[str],
    qa_history: list[dict] | None = None,
    entities_text: str = "",
    schema_text: str = "",
    metrics_text: str = "",
    skills_text: str = "",
    user_profile: list[dict] | None = None,
    model: str = PLAN_MODEL,
) -> ReportPlan:
    """
    qa_history：[{"q": "...", "a": "..."}, ...]，代表已確認的問答記錄。
    entities_text：實體擷取結果（分公司代碼、商品代碼、WHERE 提示等）。
    schema_text：候選表格欄位定義，幫助 LLM 基於實際可用欄位釐清需求。
    metrics_text：命中的業務指標計算規則（routing 後）。
    skills_text：命中的業務邏輯規則。
    """
    from datetime import date as _date
    today = _date.today().strftime("%Y/%m/%d")

    sqls_text = "\n\n---\n\n".join(case_sqls[:5]) if case_sqls else "（無歷史案例）"

    entities_content = entities_text.strip() if entities_text.strip() else "（無）"
    schema_content = schema_text.strip() if schema_text.strip() else "（無）"

    qa_block = ""
    if qa_history:
        lines = [f"  系統問：{item['q']}\n  使用者答：{item['a']}" for item in qa_history]
        qa_block = "\n\n【雙方對話記錄（已確認的資訊，請以此為依據）】\n" + "\n\n".join(lines)

    metrics_block = ""
    if metrics_text.strip():
        metrics_block = f"\n\n【參考資料 4：業務指標計算規則】\n以下是與本次需求相關的指標定義，用於判斷使用者說的「業績」「市佔」等詞彙的精確含意，以及是否需要進一步確認口徑。\n{metrics_text.strip()}"

    skills_block = ""
    if skills_text.strip():
        skills_block = f"\n\n【參考資料 5：業務邏輯規則】\n以下是與本次需求相關的業務邏輯，用於判斷篩選條件、計算方式是否有歧義需要釐清。\n{skills_text.strip()}"

    from .user_profile import select_rules, format_rules_text
    _matched = select_rules(requirement, user_profile or [])
    _profile_text = format_rules_text(_matched)
    profile_block = ""
    if _profile_text:
        profile_block = f"\n\n【參考資料 6：個人化報表知識（來自歷史查詢）】\n以下是此使用者過去針對類似報表累積的知識，已確認的作法可直接採用，不必再詢問。\n{_profile_text}"

    prompt = f"""\
今日日期：{today}

【使用者需求】
{requirement}

【參考資料 1：相似歷史案例 SQL】
以下是語意相似的歷史需求案例，幫助了解這類需求通常如何實作，僅供參考，不一定對。
{sqls_text}

【參考資料 2：系統自動識別的確定資訊】
以下是從需求中自動擷取的確定事實（分公司代碼、商品代碼、相關資料來源業務說明等）。這些是已知的，絕對不可再詢問使用者，直接採用。
{entities_content}

【參考資料 3：可用欄位定義】
以下是候選表格的欄位清單，用於判斷哪些資料可查、需求是否可實現。與使用者溝通時絕對不可提及欄位英文名稱或表格英文名稱。
{schema_content}{metrics_block}{skills_block}{profile_block}{qa_block}

你的任務是確認是否已有足夠資訊來生成報表。判斷原則：
- 若有任何真正無法從需求或歷史案例中判斷的關鍵資訊（例如：時間範圍不明確、不知道要篩哪個條件、不確定業績指標的定義）→ status="ask"，提一個最重要的問題。
- 若資訊已足夠（可合理推斷）→ status="confirm"，用白話寫出你對整份報表的完整理解。
- 每次只問一個問題。顯而易見的事情不需要問。盡量 confirm，只有真的不確定才 ask。

輸出 JSON（不要其他文字）：
{{
  "status": "ask 或 confirm",
  "question": "若 status=ask：一個最關鍵的問題，用業務員聽得懂的話問；否則空字串",
  "granularity": "帳戶|客戶|營業員|分公司|其他",
  "granularity_detail": "每一列代表什麼，用業務員聽得懂的話說明，50字以內",
  "understanding": "若 status=confirm：用業務員聽得懂的白話說明這份報表要呈現什麼，包含時間範圍、篩選條件、排列方式、每列代表什麼；絕對不可出現任何資料表名稱、欄位英文名稱或任何技術術語；否則空字串",
  "tables": "若 status=confirm：從【參考資料 3：可用欄位定義】中，選出這份報表可能需要用到的表格英文名稱清單，例如 ['M_AC_ACCOUNT', 'M_AT_STOCK_TXN']；否則空陣列 []"
}}"""

    resp = _chat(
        model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )
    raw = (resp.choices[0].message.content or "").strip()
    for fence in ("```json", "```"):
        if raw.startswith(fence):
            raw = raw[len(fence):]
    raw = raw.strip("`").strip()

    tokens = {
        "plan_in": resp.usage.prompt_tokens,
        "plan_out": resp.usage.completion_tokens,
    }
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        d = {}

    raw_tables = d.get("tables", [])
    tables = [t.upper() for t in raw_tables if isinstance(t, str)] if isinstance(raw_tables, list) else []

    return ReportPlan(
        status=d.get("status", "confirm"),
        question=d.get("question", ""),
        granularity=d.get("granularity", "其他"),
        granularity_detail=d.get("granularity_detail", ""),
        understanding=d.get("understanding", ""),
        tables=tables,
        tokens=tokens,
    )


def fmt_plan_for_user(plan: ReportPlan) -> str:
    """轉成業務員看得懂的確認文字。"""
    return plan.understanding or f"每一列代表：{plan.granularity_detail}"


def fmt_plan_for_prompt(plan: ReportPlan) -> str:
    """轉成注入 Step A prompt 的說明文字。"""
    lines = ["【報表需求理解（使用者已確認，請嚴格遵守）】"]
    if plan.understanding:
        lines.append(f"  {plan.understanding}")
    lines.append(f"  每一列粒度：{plan.granularity}（{plan.granularity_detail}）")
    return "\n".join(lines)
