"""SQL 語法驗證：sqlglot（解析層）+ sqlfluff（規則層），失敗時用 LLM 自動修正。"""

from __future__ import annotations

from .config import CLASSIFICATION_MODEL


# ── 前處理：清理 LLM 輸出的雜訊 ───────────────────────────────────

def _clean(sql: str) -> str:
    """移除 LLM 輸出中可能夾帶的 markdown fence 與多餘空白。"""
    s = sql.strip()
    # 移除開頭的 ```sql 或 ```
    for fence in ("```sql", "```"):
        if s.startswith(fence):
            s = s[len(fence):]
            break
    # 移除結尾的 ```
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


# ── 驗證層 ─────────────────────────────────────────────────────────

def _run_sqlglot(sql: str) -> list[str]:
    """用 sqlglot 做 parse 層語法檢查。"""
    try:
        import sqlglot
        sqlglot.transpile(sql, read="oracle", write="oracle")
        return []
    except Exception as e:
        return [f"[sqlglot] {e}"]


# sqlfluff 純樣式規則（縮排、空格），對語意無影響，過濾掉以減少雜訊
_SQLFLUFF_STYLE_PREFIXES = ("LT", "AL08", "CP", "RF", "CV10", "CV11")


def _run_sqlfluff(sql: str) -> list[str]:
    """用 sqlfluff oracle dialect 做規則層語法檢查，過濾純樣式規則。"""
    try:
        import sqlfluff
        result = sqlfluff.lint(sql, dialect="oracle")
        issues = []
        for v in result:
            code = v.get("code", "")
            if any(code.startswith(p) for p in _SQLFLUFF_STYLE_PREFIXES):
                continue
            desc = v.get("description", "")
            line = v.get("line_no", "?")
            issues.append(f"[sqlfluff {code}] L{line}: {desc}")
        return issues
    except Exception as e:
        return [f"[sqlfluff] {e}"]


def validate_sql(sql: str) -> list[str]:
    """執行雙重驗證，回傳所有問題（空 list = 通過）。
    先清理 LLM 輸出格式，再跑 sqlglot；若有 parse 錯誤就不跑 sqlfluff（沒意義）。
    """
    sql = _clean(sql)
    glot_errors = _run_sqlglot(sql)
    if glot_errors:
        return glot_errors
    return _run_sqlfluff(sql)


# ── LLM 修正 ───────────────────────────────────────────────────────

def _fix_with_llm(sql: str, errors: list[str], model: str) -> tuple[str, dict]:
    from .generator import _chat

    error_text = "\n".join(errors)
    resp = _chat(
        model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是 Oracle SQL 專家。"
                    "根據錯誤訊息修正 SQL，只輸出修正後的完整 SQL，"
                    "不要任何說明、不要 markdown fence。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"【錯誤訊息】\n{error_text}\n\n"
                    f"【原始 SQL】\n{sql}"
                ),
            },
        ],
        temperature=0,
    )
    fixed = (resp.choices[0].message.content or "").strip()
    for fence in ("```sql", "```"):
        if fixed.startswith(fence):
            fixed = fixed[len(fence):]
    fixed = fixed.strip("`").strip()
    tokens = {
        "fix_in": resp.usage.prompt_tokens,
        "fix_out": resp.usage.completion_tokens,
    }
    return fixed, tokens


# ── 語意審查 ───────────────────────────────────────────────────────

def semantic_review(
    sql: str,
    schema_text: str,
    requirement: str,
    model: str,
) -> tuple[str, str, bool, dict]:
    """
    語意層審查：幻覺（表/欄位存在性）、Oracle 語法、效能。
    回傳 (final_sql, note, changed, tokens)。
    若 SQL 已無問題，LLM 回覆 PASS，changed=False，SQL 不變。
    """
    from .generator import _chat

    prompt = (
        "【需求說明】\n"
        f"{requirement}\n\n"
        "【Schema（可用表格與欄位）】\n"
        f"{schema_text}\n\n"
        "【待審查 SQL】\n"
        f"{sql}\n\n"
        "請從以下三個面向審查這份 SQL：\n"
        "1. 幻覺檢查：SQL 中所有表格名稱、欄位名稱是否確實存在於上方 Schema？\n"
        "2. Oracle 語法：是否有非 Oracle 語法（LIMIT、ILIKE、:: 型別轉換等）？\n"
        "3. 效能優化：在不改變報表目的與輸出結果的前提下，是否有可優化（運算資源、運算速度）的寫法？\n"
        "   例如：WHERE 條件對索引欄位套函數可改成範圍條件、多次掃描同表應改用 CTE、\n"
        "   排名/累計需求應用視窗函數取代關聯子查詢、可提早過濾以縮小 JOIN 前的資料量等。\n\n"
        "如果 SQL 已正確且沒有需要改進的地方，只回覆：\n"
        "PASS\n\n"
        "如果有需要修正的問題，回覆：\n"
        "--- 問題 ---\n"
        "（說明發現的問題，效能優化需說明為何不影響報表結果）\n\n"
        "--- 修正後 SQL ---\n"
        "（完整修正後的 Oracle SQL，不含 markdown fence）"
    )

    resp = _chat(
        model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是 Oracle SQL 審查專家。"
                    "只在發現真正的問題時才改寫 SQL；若 SQL 已正確，直接回覆 PASS。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )
    raw = (resp.choices[0].message.content or "").strip()
    tokens = {
        "semantic_in": resp.usage.prompt_tokens,
        "semantic_out": resp.usage.completion_tokens,
    }

    if raw.upper().startswith("PASS"):
        return sql, "PASS", False, tokens

    note = ""
    new_sql = sql
    if "--- 問題 ---" in raw and "--- 修正後 SQL ---" in raw:
        after_issue = raw.split("--- 問題 ---", 1)[1]
        note = after_issue.split("--- 修正後 SQL ---", 1)[0].strip()
        new_sql = _clean(after_issue.split("--- 修正後 SQL ---", 1)[1].strip())
    else:
        note = raw

    return new_sql, note, True, tokens


# ── 主入口 ─────────────────────────────────────────────────────────

def validate_and_fix(
    sql: str,
    model: str = CLASSIFICATION_MODEL,
    max_iter: int = 3,
) -> tuple[str, list[dict], dict]:
    """
    驗證並自動修正 SQL，最多嘗試 max_iter 輪。

    回傳：
      final_sql     — 最終 SQL（通過或最後一輪修正後）
      log           — [{"round": 1, "errors": [...], "passed": bool}, ...]
      total_tokens  — 所有 LLM fix 呼叫的 token 加總
    """
    sql = _clean(sql)
    total_tokens: dict[str, int] = {}
    log: list[dict] = []

    for i in range(max_iter):
        errors = validate_sql(sql)
        passed = len(errors) == 0
        log.append({"round": i + 1, "errors": errors, "passed": passed})

        if passed:
            break

        # 有錯就 fix；max_iter=1 時：驗證一次→fix一次→結束（不再驗證）
        sql, tokens = _fix_with_llm(sql, errors, model)
        for k, v in tokens.items():
            total_tokens[k] = total_tokens.get(k, 0) + v

    return sql, log, total_tokens
