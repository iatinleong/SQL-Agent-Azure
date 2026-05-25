"""報表結構規劃：在生成 SQL 前，透過對話確認報表需求細節。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import CLASSIFICATION_MODEL
from .generator import _chat


@dataclass
class ReportPlan:
    status: str = "confirm"         # "ask" | "confirm"
    question: str = ""              # status="ask" 時，向使用者提的問題（一次一個）
    granularity: str = "其他"       # 帳戶/客戶/營業員/分公司/其他
    granularity_detail: str = ""    # 每列代表什麼（白話）
    understanding: str = ""         # status="confirm" 時，LLM 對整份報表需求的完整理解摘要
    tokens: dict = field(default_factory=dict)


_SYSTEM = """\
你是一位台灣金融業的資深資料科學家，日常工作之一就是幫助營業員 query 他們想要的資料。
你非常熟悉金融業的數據結構與業務邏輯，也深刻理解營業員在日常工作中會需要什麼樣的報表。
你擅長從簡短的業務描述中，推敲出背後真正的數據需求，並判斷還有哪些關鍵資訊尚未釐清。
根據使用者的需求、歷史案例 SQL 與雙方對話記錄，判斷是否已有足夠資訊來生成正確的報表。
只輸出 JSON，不要其他文字。"""


def plan_report(
    requirement: str,
    case_sqls: list[str],
    qa_history: list[dict] | None = None,
    entities_text: str = "",
    model: str = CLASSIFICATION_MODEL,
) -> ReportPlan:
    """
    qa_history：[{"q": "...", "a": "..."}, ...]，代表已確認的問答記錄。
    entities_text：實體擷取結果（分公司代碼、商品代碼、WHERE 提示等）。
    """
    from datetime import date as _date
    today = _date.today().strftime("%Y/%m/%d")

    sqls_text = "\n\n---\n\n".join(case_sqls[:5]) if case_sqls else "（無歷史案例）"

    entities_block = (
        f"\n\n【系統已自動識別的實體資訊（以下為確定事實，絕對不可詢問使用者，直接使用）】\n{entities_text}"
        if entities_text.strip() else ""
    )

    qa_block = ""
    if qa_history:
        lines = [f"系統問：{item['q']}\n使用者答：{item['a']}" for item in qa_history]
        qa_block = "\n\n【雙方對話記錄（已確認的資訊，請以此為依據）】\n" + "\n\n".join(lines)

    prompt = f"""\
今日日期：{today}

【使用者需求】
{requirement}

【相似歷史案例 SQL（了解這類需求通常怎麼寫）】
{sqls_text}{entities_block}{qa_block}

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
  "understanding": "若 status=confirm：用白話描述你對整份報表的完整理解，包含時間範圍、篩選條件、排列方式、每列內容等，讓使用者能一眼確認是否正確；否則空字串"
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

    return ReportPlan(
        status=d.get("status", "confirm"),
        question=d.get("question", ""),
        granularity=d.get("granularity", "其他"),
        granularity_detail=d.get("granularity_detail", ""),
        understanding=d.get("understanding", ""),
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
