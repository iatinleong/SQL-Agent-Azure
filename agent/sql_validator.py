"""SQL 語法驗證：sqlglot（解析層）+ sqlfluff（規則層）+ schema prefix + 幻覺檢查，
失敗時用 LLM 自動修正。所有 check 一次收集，統一送 LLM 修正。
"""

from __future__ import annotations

import csv
from pathlib import Path

from .config import VALIDATOR_MODEL

_SCHEMA_PATH = Path(__file__).parent.parent / "schema.csv"
_DM_S_VIEW = "DM_S_VIEW."
_ORACLE_SYSTEM_TABLES = {"DUAL"}


# ── 前處理：清理 LLM 輸出的雜訊 ───────────────────────────────────

def _clean(sql: str) -> str:
    """移除 LLM 輸出中可能夾帶的 markdown fence 與多餘空白。"""
    s = sql.strip()
    for fence in ("```sql", "```"):
        if s.startswith(fence):
            s = s[len(fence):]
            break
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


# ── 驗證層：各獨立 check ────────────────────────────────────────────

def _run_sqlglot(sql: str) -> list[str]:
    """用 sqlglot 做 parse 層語法檢查。"""
    try:
        import sqlglot
        sqlglot.transpile(sql, read="oracle", write="oracle")
        return []
    except Exception as e:
        return [f"[sqlglot] {e}"]


_SQLFLUFF_STYLE_PREFIXES = (
    "LT",    # 縮排/空白
    "AL08",  # 欄位別名樣式
    "CP",    # 大小寫偏好
    "RF",    # 引用格式
    "CV10",  # 關鍵字大小寫
    "CV11",  # 逗號位置
    "AM05",  # JOIN 條件需完整表名（alias 是標準寫法，不需強制）
    "ST09",  # JOIN 表格順序（純樣式）
    "CV02",  # NVL→COALESCE（兩者 Oracle 均合法）
    "CV06",  # 語句需以分號結尾（分號存在時仍誤報）
)


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


def _check_oracle_quirks(sql: str) -> list[str]:
    """偵測 sqlglot/sqlfluff 不會抓的 Oracle 特有語法限制。
    目前規則：SELECT 沒有 FROM 來源時，Oracle 必須用 FROM DUAL。
    """
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, dialect="oracle")
    except Exception:
        return []

    errors: list[str] = []
    seen: set = set()

    for select_node in tree.find_all(exp.Select):
        if select_node.args.get("from"):
            continue
        # sqlglot 在某些結構（CTE + FETCH FIRST、視窗函數）下，from arg 可能未填入，
        # 只要 SELECT 含欄位引用、聚合函數或視窗函數，就一定有資料來源，不需 FROM DUAL。
        if (select_node.find(exp.Column)
                or select_node.find(exp.AggFunc)
                or select_node.find(exp.Window)):
            continue

        cte_name = ""
        node = select_node.parent
        while node:
            if isinstance(node, exp.CTE):
                cte_name = node.alias_or_name
                break
            node = getattr(node, "parent", None)

        key = cte_name or id(select_node)
        if key in seen:
            continue
        seen.add(key)

        loc = f" (CTE: {cte_name})" if cte_name else ""
        errors.append(
            f"[Oracle quirk] SELECT 沒有 FROM{loc}："
            "Oracle 不允許無來源表格的 SELECT，請改為 SELECT ... FROM DUAL"
        )

    return errors


def _check_dm_s_view_prefix(sql: str) -> list[str]:
    """確認每個表格都有 schema 前綴。
    沒有任何 schema 前綴（如 M_AC_ACCOUNT）→ 報錯，應改為 DM_S_VIEW.M_AC_ACCOUNT。
    已有其他 schema 前綴（如 S_MELODYJJJIAN.CUSTOMER_GROUP_2026）→ 保持原樣，不報。
    """
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, dialect="oracle")
    except Exception:
        return []

    cte_names: set[str] = {cte.alias_or_name.upper() for cte in tree.find_all(exp.CTE)}
    errors: list[str] = []
    seen: set[str] = set()

    for tnode in tree.find_all(exp.Table):
        raw_name = tnode.name or ""
        if not raw_name:
            continue
        if raw_name.upper() in cte_names:
            continue
        if raw_name.upper() in _ORACLE_SYSTEM_TABLES:
            continue
        db = tnode.db or ""
        if not db:
            key = raw_name.upper()
            if key not in seen:
                seen.add(key)
                errors.append(
                    f"[schema prefix] 表格 {raw_name} 缺少 schema 前綴，"
                    f"應改為 DM_S_VIEW.{raw_name}"
                )

    return errors


# ── 幻覺檢查（AST + schema.csv）──────────────────────────────────────

def _load_schema_lookup() -> dict[str, set[str]]:
    """從 schema.csv 建立 {正規化表格名稱: {欄位名稱大寫, ...}}。"""
    lookup: dict[str, set[str]] = {}
    with open(_SCHEMA_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tname = row.get("表格名稱", "").strip().upper()
            col = row.get("欄位名稱", "").strip().upper()
            if tname and col:
                lookup.setdefault(tname, set()).add(col)
    return lookup


def _normalize_table(db: str, name: str) -> str:
    """將 SQL 中的表格引用正規化為 schema.csv 的 key 格式。
    DM_S_VIEW.M_AC_ACCOUNT → M_AC_ACCOUNT
    S_ARIELSHAO.CUSTOMER_GROUP_2026Q1 → S_ARIELSHAO.CUSTOMER_GROUP_2026Q1（保留）
    """
    if db:
        full = f"{db.upper()}.{name.upper()}"
    else:
        full = name.upper()
    if full.startswith(_DM_S_VIEW):
        return full[len(_DM_S_VIEW):]
    return full


def check_hallucination(sql: str) -> list[str]:
    """
    決定性幻覺檢查：用 sqlglot AST 提取表格與有明確表格限定詞的欄位，
    與 schema.csv 做字串比對。回傳錯誤訊息 list（空 list = 通過）。

    跳過的情況（避免誤報）：
    - CTE 名稱（非實際表格）
    - 無法解析 alias 的欄位引用
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return []

    schema_lookup = _load_schema_lookup()
    errors: list[str] = []

    try:
        tree = sqlglot.parse_one(sql, dialect="oracle")
    except Exception:
        return []

    # 1. 收集 CTE 名稱（虛擬表格，不驗證）
    cte_names: set[str] = {
        cte.alias_or_name.upper() for cte in tree.find_all(exp.CTE)
    }

    # 收集 CTE 輸出的欄位別名（如 ROW_NUMBER() OVER (...) AS RN）
    cte_col_aliases: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        body = cte.this
        if isinstance(body, exp.Subquery):
            body = body.this
        if isinstance(body, exp.Select):
            for expr in body.expressions:
                if isinstance(expr, exp.Alias):
                    cte_col_aliases.add(expr.alias.upper())

    # 2. 建立 alias → 正規化表格名 的對照表
    alias_map: dict[str, str] = {}
    for tnode in tree.find_all(exp.Table):
        raw_name = tnode.name or ""
        if not raw_name:
            continue
        normalized = _normalize_table(tnode.db or "", raw_name)
        if normalized in cte_names or raw_name.upper() in cte_names:
            continue
        alias = (tnode.alias or "").upper()
        if alias:
            if alias in alias_map and alias_map[alias] != normalized:
                alias_map[alias] = ""  # 同一 alias 在不同 CTE 中指不同表，標記為不明確
            else:
                alias_map[alias] = normalized
        alias_map[raw_name.upper()] = normalized
        alias_map[normalized] = normalized

    # 3. 驗證表格存在（每個不存在的表格都報）
    seen_table_errors: set[str] = set()
    for tnode in tree.find_all(exp.Table):
        raw_name = tnode.name or ""
        if not raw_name:
            continue
        normalized = _normalize_table(tnode.db or "", raw_name)
        if normalized in cte_names or raw_name.upper() in cte_names:
            continue
        if raw_name.upper() in _ORACLE_SYSTEM_TABLES:
            continue
        if normalized not in schema_lookup and normalized not in seen_table_errors:
            seen_table_errors.add(normalized)
            errors.append(f"[幻覺] 表格不存在於 schema：{normalized}")

    # 4. 驗證有限定詞的欄位存在（每個不存在的欄位都報）
    seen_col_errors: set[str] = set()
    for cnode in tree.find_all(exp.Column):
        col_name = (cnode.name or "").upper()
        qualifier = (cnode.table or "").upper()

        if not col_name or col_name == "*" or not qualifier:
            continue
        if qualifier in cte_names:
            continue

        actual_table = alias_map.get(qualifier)
        if not actual_table or actual_table not in schema_lookup:
            continue

        if col_name not in schema_lookup[actual_table]:
            key = f"{actual_table}.{col_name}"
            if key not in seen_col_errors:
                seen_col_errors.add(key)
                errors.append(
                    f"[幻覺] 欄位不存在於 schema：{actual_table}.{col_name}"
                )

    # 5. 驗證無限定詞的欄位（對查詢中所有已知表格的欄位聯集比對）
    all_query_cols: set[str] = set()
    for tname in set(alias_map.values()):
        if tname in schema_lookup:
            all_query_cols.update(schema_lookup[tname])

    if all_query_cols:
        seen_unqualified_errors: set[str] = set()
        for cnode in tree.find_all(exp.Column):
            col_name = (cnode.name or "").upper()
            qualifier = (cnode.table or "").upper()
            if not col_name or col_name == "*" or qualifier:
                continue
            if col_name in cte_names:
                continue
            if col_name not in all_query_cols and col_name not in cte_col_aliases and col_name not in seen_unqualified_errors:
                seen_unqualified_errors.add(col_name)
                errors.append(
                    f"[幻覺] 欄位不存在於查詢中任何表格：{col_name}"
                )

    return errors


# ── 全套錯誤收集 ────────────────────────────────────────────────────

def _collect_all_errors(sql: str) -> list[str]:
    """依序執行所有驗證：語法 → Oracle quirk → schema prefix → 幻覺 → sqlfluff。
    sqlglot parse 失敗時，AST 類 check 無意義，直接回傳語法錯誤。
    """
    glot_errors = _run_sqlglot(sql)
    if glot_errors:
        return glot_errors

    errors: list[str] = []
    errors += _check_oracle_quirks(sql)
    errors += _check_dm_s_view_prefix(sql)
    errors += check_hallucination(sql)
    errors += _run_sqlfluff(sql)
    return errors


# ── 公開 check（不含 LLM 修正）──────────────────────────────────────

def validate_sql(sql: str) -> list[str]:
    """全套驗證（sqlglot → Oracle quirk → schema prefix → 幻覺 → sqlfluff）。"""
    return _collect_all_errors(_clean(sql))


# ── Schema hint（幻覺修正用）─────────────────────────────────────────

def _build_schema_hint(sql: str) -> str:
    """從 SQL 中提取實際使用的表格，回傳欄位清單供 LLM 修正幻覺時參考。"""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, dialect="oracle")
    except Exception:
        return ""

    schema_lookup = _load_schema_lookup()
    cte_names: set[str] = {cte.alias_or_name.upper() for cte in tree.find_all(exp.CTE)}

    used_tables: set[str] = set()
    for tnode in tree.find_all(exp.Table):
        raw_name = tnode.name or ""
        if not raw_name:
            continue
        normalized = _normalize_table(tnode.db or "", raw_name)
        if normalized in cte_names or raw_name.upper() in cte_names:
            continue
        if raw_name.upper() in _ORACLE_SYSTEM_TABLES:
            continue
        if normalized in schema_lookup:
            used_tables.add(normalized)

    if not used_tables:
        return ""

    lines = []
    for tname in sorted(used_tables):
        cols = ", ".join(sorted(schema_lookup[tname]))
        lines.append(f"{tname}：{cols}")
    return "\n".join(lines)


# ── LLM 修正 ───────────────────────────────────────────────────────

def _fix_with_llm(sql: str, errors: list[str], model: str, schema_hint: str = "") -> tuple[str, dict]:
    from .generator import _chat

    error_text = "\n".join(errors)
    resp = _chat(
        model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是 Oracle SQL 專家。根據錯誤訊息修正 SQL，只輸出修正後的完整 SQL，"
                    "不要任何說明、不要 markdown fence。\n\n"
                    "【Schema 規則】所有表格一律加上 DM_S_VIEW schema 前綴"
                    "（例如 DM_S_VIEW.M_AC_ACCOUNT），"
                    "唯一例外：表格名稱本身已含有其他 schema 前綴"
                    "（例如 S_MELODYJJJIAN.CUSTOMER_GROUP_2026），則保持原樣不做修改。\n\n"
                    "【Oracle 語法】使用 Oracle 19c+ 語法，"
                    "禁用其他資料庫方言（MySQL 的 LIMIT、PostgreSQL 的 ILIKE 等）。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"【錯誤訊息】\n{error_text}\n\n"
                    + (f"【可用欄位定義（每行：表格名稱：欄位清單）】\n{schema_hint}\n\n" if schema_hint else "")
                    + f"【原始 SQL】\n{sql}"
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


# ── 主入口：驗證並自動修正 ────────────────────────────────────────

def validate_and_fix(
    sql: str,
    model: str = VALIDATOR_MODEL,
    max_iter: int = 3,
) -> tuple[str, list[dict], dict]:
    """
    全套驗證並自動修正，最多 max_iter 輪。
    回傳 (final_sql, log, total_tokens)。
    log 每筆：{"round": int, "errors": list[str], "passed": bool}
    """
    sql = _clean(sql)
    total_tokens: dict[str, int] = {}
    log: list[dict] = []

    for i in range(max_iter):
        errors = _collect_all_errors(sql)
        passed = len(errors) == 0
        log.append({"round": i + 1, "errors": errors, "passed": passed})

        if passed:
            break

        has_hallucination = any(e.startswith("[幻覺]") for e in errors)
        schema_hint = _build_schema_hint(sql) if has_hallucination else ""
        sql, tokens = _fix_with_llm(sql, errors, model, schema_hint=schema_hint)
        for k, v in tokens.items():
            total_tokens[k] = total_tokens.get(k, 0) + v

    return sql, log, total_tokens
