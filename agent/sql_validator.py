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

        if i < max_iter - 1:
            sql, tokens = _fix_with_llm(sql, errors, model)
            for k, v in tokens.items():
                total_tokens[k] = total_tokens.get(k, 0) + v
        # 最後一輪不再修正，保留最後修正結果（或原始）

    return sql, log, total_tokens
