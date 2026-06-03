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
    except ImportError:
        return []
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
        # sqlglot 在某些結構（CTE + 純函數呼叫、FETCH FIRST、視窗函數）下，from arg 可能未填入，
        # 只要 SELECT 含欄位引用、聚合函數、視窗函數或任何 Func（TO_CHAR/ADD_MONTHS/TRUNC/SYSDATE 等），
        # 就表示有明確計算來源，不觸發 FROM DUAL 警告。
        if (select_node.find(exp.Column)
                or select_node.find(exp.AggFunc)
                or select_node.find(exp.Window)
                or select_node.find(exp.Func)):
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

    # 收集所有 AS 別名（CTE、derived table、視窗函數等），避免外層 SELECT 引用時誤報幻覺
    cte_col_aliases: set[str] = {
        expr.alias.upper()
        for sel in tree.find_all(exp.Select)
        for expr in sel.expressions
        if isinstance(expr, exp.Alias)
    }

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


# ── Data Redaction 直接替換 ─────────────────────────────────────────

def _is_in_window_spec(col_node) -> bool:
    """Return True if col_node is inside an OVER() window spec (PARTITION BY / ORDER BY).
    Such usage is NOT a SELECT output — it's an analytical key, like JOIN/WHERE.
    """
    from sqlglot import exp
    node = getattr(col_node, "parent", None)
    while node is not None:
        if isinstance(node, exp.Window):
            return True
        if isinstance(node, exp.Select):
            return False
        node = getattr(node, "parent", None)
    return False


def _is_intermediate_select(select_node) -> bool:
    """Return True if this SELECT is inside a CTE or subquery (not the final output row)."""
    from sqlglot import exp
    node = getattr(select_node, "parent", None)
    while node is not None:
        if isinstance(node, (exp.CTE, exp.Subquery)):
            return True
        node = getattr(node, "parent", None)
    return False


def _get_direct_real_tables(select_node, cte_names: set[str]) -> set[str]:
    """回傳此 SELECT 的 FROM/JOIN 直接來源的真實表格（不含 subquery 內層）。"""
    from sqlglot import exp
    tables: set[str] = set()
    sources = []
    # sqlglot 用 from_ 儲存 FROM 子句（from 是 Python 關鍵字）
    from_arg = select_node.args.get("from") or select_node.args.get("from_")
    if from_arg:
        sources.append(getattr(from_arg, "this", from_arg))
    for join in (select_node.args.get("joins") or []):
        sources.append(getattr(join, "this", join))
    for src in sources:
        if isinstance(src, exp.Table):
            raw = src.name or ""
            if raw and raw.upper() not in cte_names:
                tables.add(_normalize_table(src.db or "", raw))
    return tables


def _check_data_redaction(sql: str) -> list[str]:
    """偵測最終報表 SELECT（最外層）輸出 party_id，以及全域的 party_id/party_id_mask 互比。
    CTE / subquery 內的 party_id 作為 JOIN/GROUP key 不報錯。
    - final SELECT 含 party_id 且來源表有 party_id_mask → 建議直接替換
    - final SELECT 含 party_id 且來源表無 party_id_mask → 建議 JOIN M_AC_ACCOUNT
    - 全域：party_id = party_id_mask 互比 / IN 互比 → 報錯
    """
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, dialect="oracle")
    except Exception:
        return []

    schema_lookup = _load_schema_lookup()
    cte_names: set[str] = {cte.alias_or_name.upper() for cte in tree.find_all(exp.CTE)}

    alias_map: dict[str, str] = {}
    for tnode in tree.find_all(exp.Table):
        raw_name = tnode.name or ""
        if not raw_name or raw_name.upper() in cte_names:
            continue
        normalized = _normalize_table(tnode.db or "", raw_name)
        alias = (tnode.alias or "").upper()
        if alias:
            alias_map[alias] = normalized
        alias_map[raw_name.upper()] = normalized
        alias_map[normalized] = normalized

    errors: list[str] = []
    seen: set[str] = set()

    for select_node in tree.find_all(exp.Select):
        # CTE / subquery 內的 party_id 是 JOIN/GROUP key，不是最終輸出，不報錯
        if _is_intermediate_select(select_node):
            continue
        direct_tables = _get_direct_real_tables(select_node, cte_names)

        for expr in select_node.expressions:
            for col in expr.find_all(exp.Column):
                col_name = (col.name or "").upper()
                qualifier = (col.table or "").upper()

                if col_name != "PARTY_ID":
                    continue
                # PARTITION BY / ORDER BY inside OVER() is an analytical key,
                # not a SELECT output — treat like JOIN/WHERE, skip
                if _is_in_window_spec(col):
                    continue

                key = f"pid_{qualifier}"
                if key in seen:
                    continue
                seen.add(key)

                original = f"{qualifier.lower()}.party_id" if qualifier else "party_id"

                # 解析來源表
                if qualifier and qualifier not in cte_names:
                    resolved = alias_map.get(qualifier, "")
                    source_cols = schema_lookup.get(resolved, set())
                else:
                    # 無 qualifier：合併所有 direct_tables 的欄位
                    source_cols = set()
                    for tname in direct_tables:
                        source_cols |= schema_lookup.get(tname, set())

                if "PARTY_ID_MASK" in source_cols:
                    errors.append(
                        f"[Data Redaction] 禁止 SELECT {original}："
                        f"來源表已有 party_id_mask 欄位，請直接將 {original} 替換為 party_id_mask"
                    )
                else:
                    errors.append(
                        f"[Data Redaction] 禁止 SELECT {original}："
                        "請改 JOIN DM_S_VIEW.M_AC_ACCOUNT ON party_id，"
                        "並在 SELECT 改用 M_AC_ACCOUNT.party_id_mask"
                    )

    # ── party_id_mask 與 party_id 互相比較（= 等號）────────────────────
    for eq_node in tree.find_all(exp.EQ):
        left, right = eq_node.left, eq_node.right
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        ln = (left.name or "").upper()
        rn = (right.name or "").upper()
        if not ({ln, rn} == {"PARTY_ID", "PARTY_ID_MASK"}):
            continue
        lq = (left.table or "").lower()
        rq = (right.table or "").lower()
        lstr = f"{lq}.{left.name.lower()}" if lq else left.name.lower()
        rstr = f"{rq}.{right.name.lower()}" if rq else right.name.lower()
        key = f"eq_{lstr}_{rstr}"
        if key not in seen:
            seen.add(key)
            errors.append(
                f"[Data Redaction] 禁止 {lstr} = {rstr}："
                "party_id 與 party_id_mask 數值不同，不可互相比較；"
                "JOIN / WHERE 條件一律使用 party_id"
            )

    # ── party_id IN (SELECT party_id_mask ...) 或反向 ────────────────
    for in_node in tree.find_all(exp.In):
        outer_col = in_node.this
        if not isinstance(outer_col, exp.Column):
            continue
        outer_name = (outer_col.name or "").upper()
        if outer_name not in ("PARTY_ID", "PARTY_ID_MASK"):
            continue

        # 蒐集 IN 子查詢或清單裡出現的欄位名稱
        inner_names: set[str] = set()
        subquery = in_node.args.get("query")
        if subquery:
            for col in subquery.find_all(exp.Column):
                inner_names.add((col.name or "").upper())
        for expr in (in_node.args.get("expressions") or []):
            if isinstance(expr, exp.Column):
                inner_names.add((expr.name or "").upper())

        counterpart = "PARTY_ID_MASK" if outer_name == "PARTY_ID" else "PARTY_ID"
        if counterpart not in inner_names:
            continue

        oq = (outer_col.table or "").lower()
        ostr = f"{oq}.{outer_col.name.lower()}" if oq else outer_col.name.lower()
        key = f"in_{ostr}"
        if key not in seen:
            seen.add(key)
            errors.append(
                f"[Data Redaction] 禁止 {ostr} IN (... {counterpart.lower()} ...)："
                "party_id 與 party_id_mask 數值不同，不可互相比較；"
                f"請改用 EXISTS 或 JOIN 方式，子查詢改 SELECT 1 並以 party_id = party_id 條件關聯"
            )

    return errors


# ── SYSDATE 時效自動修正 ──────────────────────────────────────────────

def _fix_sysdate(sql: str) -> tuple[str, list[str]]:
    """資料庫每日 T-1 更新 + 日期等值比較需 TRUNC：自動修正以下情況。
    修正範圍（只作用於非 -- 注解部分）：
      T-1 規則：
        = SYSDATE              →  = TRUNC(SYSDATE)-1
        = TRUNC(SYSDATE)       →  = TRUNC(SYSDATE)-1
        DATE 'YYYY-MM-DD'      →  DATE 'YYYY-MM-DD' (yesterday)
        'YYYY-MM-DD'           →  'YYYY-MM-DD' (yesterday)
        'YYYYMMDD'             →  'YYYYMMDD' (yesterday)
      TRUNC 精確度規則：
        = SYSDATE-N            →  = TRUNC(SYSDATE)-N
        （SYSDATE 含時間成分，日期欄位等值比較必須 TRUNC 截至零時）
    回傳 (fixed_sql, notices)，notices 為非空時表示有自動修正。
    """
    import re
    from datetime import date, timedelta

    today = date.today()
    yesterday = today - timedelta(days=1)
    ymd = today.strftime("%Y-%m-%d")
    ymd8 = today.strftime("%Y%m%d")
    ymd_y = yesterday.strftime("%Y-%m-%d")
    ymd8_y = yesterday.strftime("%Y%m%d")

    applied: set[str] = set()

    def _fix_code(code: str) -> str:
        # 1. = SYSDATE (bare, no offset) → TRUNC + T-1
        new, n = re.subn(r'(=\s*)SYSDATE\b(?!\s*[-+])', r'\1TRUNC(SYSDATE)-1', code, flags=re.IGNORECASE)
        if n: applied.add("sysdate")
        code = new
        # 2. = TRUNC(SYSDATE) (no offset) → T-1
        new, n = re.subn(r'(=\s*TRUNC\s*\(\s*SYSDATE\s*\))(?!\s*[-+])', r'\1-1', code, flags=re.IGNORECASE)
        if n: applied.add("trunc_sysdate")
        code = new
        # 3. = SYSDATE-N (has offset, no TRUNC) → TRUNC for date accuracy
        #    Matches "= SYSDATE - 1", ">= SYSDATE-7" etc.; skips "= TRUNC(SYSDATE)-1"
        new, n = re.subn(r'(=\s*)SYSDATE\b(\s*-\s*\d+)', r'\1TRUNC(SYSDATE)\2', code, flags=re.IGNORECASE)
        if n: applied.add("sysdate_trunc")
        code = new
        # 4. DATE 'today' → DATE 'yesterday'  (must run before bare-string rule)
        new, n = re.subn(rf"(DATE\s+'){re.escape(ymd)}'", rf"\g<1>{ymd_y}'", code, flags=re.IGNORECASE)
        if n: applied.add("date_literal")
        code = new
        # 5. standalone 'YYYY-MM-DD' today
        if f"'{ymd}'" in code:
            applied.add("date_literal")
            code = code.replace(f"'{ymd}'", f"'{ymd_y}'")
        # 6. 'YYYYMMDD' today
        if f"'{ymd8}'" in code:
            applied.add("date8_literal")
            code = code.replace(f"'{ymd8}'", f"'{ymd8_y}'")
        return code

    lines = []
    for line in sql.split('\n'):
        m = re.search(r'--', line)
        if m:
            lines.append(_fix_code(line[:m.start()]) + line[m.start():])
        else:
            lines.append(_fix_code(line))

    notices: list[str] = []
    if "sysdate" in applied:
        notices.append("[資料時效自動修正] = SYSDATE → = TRUNC(SYSDATE)-1（T-1 + 截至零時）")
    if "trunc_sysdate" in applied:
        notices.append("[資料時效自動修正] = TRUNC(SYSDATE) → = TRUNC(SYSDATE)-1（T-1）")
    if "sysdate_trunc" in applied:
        notices.append("[資料時效自動修正] = SYSDATE-N → = TRUNC(SYSDATE)-N（SYSDATE 含時間成分，日期等值比較須 TRUNC）")
    if "date_literal" in applied or "date8_literal" in applied:
        notices.append(f"[資料時效自動修正] 今日日期 {ymd} → 昨日 {ymd_y}（T-1）")

    return '\n'.join(lines), notices


# ── 隱私與語意規則 ──────────────────────────────────────────────────

def _check_forbidden_tables(sql: str) -> list[str]:
    """禁止使用 PARTY_NAME 表格（隱私政策）。"""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, dialect="oracle")
    except Exception:
        return []

    errors: list[str] = []
    seen: set[str] = set()
    for tnode in tree.find_all(exp.Table):
        name = (tnode.name or "").upper()
        if name != "PARTY_NAME":
            continue
        full = f"{tnode.db.upper() + '.' if tnode.db else ''}{name}"
        if full not in seen:
            seen.add(full)
            errors.append(
                f"[隱私政策] 禁止使用 {full}：PARTY_NAME 表格因隱私規定不可查詢，"
                "請移除客戶姓名欄位"
            )
    return errors


def _check_mask_misuse(sql: str) -> list[str]:
    """偵測最終 SELECT 中 party_id_mask 被 alias 成姓名類欄位（如客戶姓名）。
    使用黑名單判斷：alias 含 姓名/名稱/NAME/CUSTOMER_NAME/CUST_NAME 才報錯，
    正常的「識別碼/MASK/ID」等 alias 一律放行。
    """
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, dialect="oracle")
    except Exception:
        return []

    _NAME_KEYWORDS = ("姓名", "名稱", "NAME", "CUSTOMER_NAME", "CUST_NAME")
    errors: list[str] = []
    for select_node in tree.find_all(exp.Select):
        if _is_intermediate_select(select_node):
            continue
        for expr in select_node.expressions:
            if not isinstance(expr, exp.Alias):
                continue
            col = expr.this
            if not isinstance(col, exp.Column):
                continue
            if (col.name or "").upper() != "PARTY_ID_MASK":
                continue
            alias = expr.alias or ""
            if any(kw in alias.upper() for kw in _NAME_KEYWORDS):
                errors.append(
                    f"[語意錯誤] party_id_mask 不可 alias 為 \"{alias}\"："
                    "party_id_mask 是加密識別碼，非客戶姓名；"
                    "請改為 NULL AS \"{alias}\" 並說明姓名因隱私不可查"
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
    errors += _check_forbidden_tables(sql)
    errors += _check_data_redaction(sql)
    errors += _check_mask_misuse(sql)
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


def _build_schema_hint_for_tables(table_names: list[str]) -> str:
    """回傳指定表格的欄位清單（供 LLM 修正時參考）。"""
    schema_lookup = _load_schema_lookup()
    lines = []
    for tname in table_names:
        key = tname.upper()
        if key in schema_lookup:
            cols = ", ".join(sorted(schema_lookup[key]))
            lines.append(f"{key}：{cols}")
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
                    "禁用其他資料庫方言（MySQL 的 LIMIT、PostgreSQL 的 ILIKE 等）。\n\n"
                    "【Data Redaction】party_id 受 Oracle Data Redaction 保護：\n"
                    "1. 最終報表 SELECT（最外層）禁止輸出 party_id；"
                    "CTE / subquery 內可保留 party_id 作為 JOIN / GROUP BY key，不需移除。\n"
                    "2. 需顯示個人識別碼時改用 party_id_mask：若來源表已有 party_id_mask 欄位，"
                    "直接 SELECT party_id_mask；若無，則 JOIN DM_S_VIEW.M_AC_ACCOUNT ON "
                    "<主表>.party_id = M_AC_ACCOUNT.party_id，再 SELECT M_AC_ACCOUNT.party_id_mask。\n"
                    "3. party_id 與 party_id_mask 數值不同，不可互換：JOIN / WHERE / IN 條件一律用 party_id；"
                    "禁止用 party_id_mask 去比對或 JOIN 其他表格的 party_id。\n"
                    "4. party_id_mask 是加密識別碼，禁止 alias 成姓名欄位（如 AS \"客戶姓名\"）；"
                    "若姓名來源表不存在或被禁用，一律改為 NULL AS \"客戶姓名\"，並加註解說明原因，不可用 party_id_mask 代替。\n\n"
                    "【隱私政策】禁止使用 PARTY_NAME 表格（任何 schema 下）：移除對該表的所有 JOIN，"
                    "並將姓名欄位改為 NULL AS \"客戶姓名\" -- 隱私規定不可查詢。\n\n"
                    "【資料時效】整個資料庫每日 T-1 更新：所有日期欄位的最新可用資料為昨日（SYSDATE-1）。"
                    "使用者說「今天」一律解讀為昨日；禁止以今日日期（SYSDATE 或等於今日的 DATE literal）"
                    "作為篩選上限，否則查詢結果為空。"
                    "取最新 SNAP_YYYYMM 時使用 TO_CHAR(TRUNC(SYSDATE-1,'MM'),'YYYYMM')；"
                    "禁止用 ADD_MONTHS(TRUNC(SYSDATE,'MM'),-1)，那會得到上個月而非最新月份。"
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
    max_iter: int = 1,
) -> tuple[str, list[dict], dict]:
    """
    全套驗證並自動修正，最多 max_iter 輪。
    回傳 (final_sql, log, total_tokens)。
    log 每筆：{"round": int, "errors": list[str], "passed": bool}
    """
    sql, auto_fixes = _fix_sysdate(_clean(sql))
    total_tokens: dict[str, int] = {}
    log: list[dict] = []
    if auto_fixes:
        log.append({"auto_fixes": auto_fixes})

    for i in range(max_iter):
        errors = _collect_all_errors(sql)
        passed = len(errors) == 0
        log.append({"round": i + 1, "errors": errors, "passed": passed})

        if passed:
            break

        has_hallucination = any(e.startswith("[幻覺]") for e in errors)
        has_redaction = any(e.startswith("[Data Redaction]") for e in errors)
        schema_hint = _build_schema_hint(sql) if has_hallucination else ""
        if has_redaction:
            mac_hint = _build_schema_hint_for_tables(["M_AC_ACCOUNT"])
            schema_hint = f"{schema_hint}\n{mac_hint}".strip() if schema_hint else mac_hint
        sql, tokens = _fix_with_llm(sql, errors, model, schema_hint=schema_hint)
        for k, v in tokens.items():
            total_tokens[k] = total_tokens.get(k, 0) + v

    return sql, log, total_tokens
