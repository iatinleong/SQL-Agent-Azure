"""使用者個人化知識庫：按報表主題分類，關鍵字觸發注入。儲存於 Supabase user_profiles 表。"""

from __future__ import annotations

import json

from .config import PROFILE_MODEL, PROFILE_REASONING_EFFORT
from .supabase_logger import get_client

_TABLE = "user_profiles"

# ── 載入 ─────────────────────────────────────────────────────────────────────

def load_profile(employee_id: str) -> list[dict]:
    """從 Supabase 載入知識庫，回傳 list[dict]，找不到時回傳空 list。"""
    client = get_client()
    if not client or not employee_id:
        return []
    try:
        result = (
            client.table(_TABLE)
            .select("preference_summary")
            .eq("employee_id", employee_id)
            .limit(1)
            .execute()
        )
        if result.data:
            raw = result.data[0].get("preference_summary", "") or ""
            return _parse(raw)
    except Exception as e:
        print(f"[user_profile] 載入失敗：{e}")
    return []


# ── 關鍵字觸發選取 ────────────────────────────────────────────────────────────

def select_rules(query: str, rules: list[dict]) -> list[dict]:
    """從知識庫中選出關鍵字命中的條目（同 metrics/skills 觸發邏輯）。"""
    if not rules or not query:
        return []
    q = query.lower()
    matched = []
    for rule in rules:
        keywords = rule.get("trigger_keywords") or []
        if any(kw.lower() in q for kw in keywords):
            matched.append(rule)
    return matched


def format_rules_text(rules: list[dict]) -> str:
    """將選中的條目格式化為 prompt 注入文字。"""
    if not rules:
        return ""
    lines = []
    for r in rules:
        lines.append(f"【{r.get('name', '')}】{r.get('note', '')}")
    return "\n".join(lines)


# ── 更新 ─────────────────────────────────────────────────────────────────────

def update_profile(
    employee_id: str,
    current_rules: list[dict],
    requirement: str,
    qa_history: list[dict],
    understanding: str,
    corrections: list[str] | None = None,
) -> list[dict]:
    """根據本次查詢觀察，用 LLM 更新知識庫，寫回 Supabase。回傳更新後的 list[dict]。"""
    from .generator import _chat

    observation = _build_observation(requirement, qa_history, understanding, corrections or [])
    if not observation.strip():
        return current_rules

    existing_json = json.dumps(current_rules, ensure_ascii=False, indent=2) if current_rules else "[]"

    prompt = f"""\
【現有個人化知識庫】
{existing_json}

【本次查詢觀察】
{observation}

請更新知識庫，輸出完整的 JSON 陣列。每個條目格式：
{{"name": "主題名稱", "trigger_keywords": ["關鍵字1", "關鍵字2"], "note": "注意事項"}}

更新規則：
- 若現有條目的 name 語意相近，或 trigger_keywords 有大量重疊 → 合併更新那條，不要新增重複條目
- 若本次觀察帶來全新主題的可復用知識 → 新增一條
- 若本次查詢沒有帶來新的可復用知識 → 原樣回傳，不要修改
- note 不要太多字，只記錄可復用的知識（不記錄當次的具體數值、特定日期）
- trigger_keywords 3-6 個中文業務術語
- 只輸出 JSON 陣列，不要其他文字"""

    try:
        resp = _chat(
            PROFILE_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一個金融業報表知識分析師，專門從查詢紀錄中萃取可復用的報表製作知識。"
                        "只輸出 JSON 陣列，不要任何說明文字。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            reasoning_effort=PROFILE_REASONING_EFFORT,
        )
        raw = (resp.choices[0].message.content or "").strip()
        new_rules = _parse(raw)
    except Exception as e:
        print(f"[user_profile] LLM 更新失敗：{e}")
        return current_rules

    _save_profile(employee_id, json.dumps(new_rules, ensure_ascii=False))
    return new_rules


# ── 內部工具 ──────────────────────────────────────────────────────────────────

def _parse(raw: str) -> list[dict]:
    """解析 JSON 字串，失敗時回傳空 list。"""
    for fence in ("```json", "```"):
        if raw.startswith(fence):
            raw = raw[len(fence):]
    raw = raw.strip("`").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _build_observation(
    requirement: str,
    qa_history: list[dict],
    understanding: str,
    corrections: list[str],
) -> str:
    parts = [f"需求：{requirement}"]
    if qa_history:
        qa_lines = [f"  系統問：{item['q']}\n  用戶答：{item['a']}" for item in qa_history]
        parts.append("系統提問與用戶回答：\n" + "\n".join(qa_lines))
    if understanding:
        parts.append(f"最終報表理解：{understanding}")
    if corrections:
        parts.append("用戶修正指令：\n" + "\n".join(f"  - {c}" for c in corrections))
    return "\n\n".join(parts)


def _save_profile(employee_id: str, profile_json: str) -> None:
    from datetime import datetime, timezone
    client = get_client()
    if not client or not employee_id:
        return
    try:
        client.table(_TABLE).upsert({
            "employee_id": employee_id,
            "preference_summary": profile_json,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"[user_profile] 寫入失敗：{e}")
