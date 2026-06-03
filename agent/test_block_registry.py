"""Unit tests for BlockRegistry: parse, tag_errors, replace_block, splice correctness."""
from __future__ import annotations

import sys
import textwrap

SEP = "─" * 65
WIDE = "═" * 65


# ── helpers ───────────────────────────────────────────────────────────

def _reg(sql: str):
    from agent.block_registry import BlockRegistry
    return BlockRegistry(textwrap.dedent(sql).strip())


def _check(desc: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}]  {desc}")
    if not condition and detail:
        print(f"          {detail}")
    return condition


# ── 1. parse ──────────────────────────────────────────────────────────

def test_parse() -> int:
    print(f"\n{WIDE}\n  1. BlockRegistry.parse\n{WIDE}")
    failed = 0

    # 1-a: single CTE
    sql = """
        WITH acct_base AS (
            SELECT party_id, acct_nbr FROM DM_S_VIEW.M_AC_ACCOUNT
        )
        SELECT party_id, acct_nbr FROM acct_base
    """
    reg = _reg(sql)
    names = [b.name for b in reg.blocks]
    if not _check("single CTE → 2 blocks", names == ["cte:acct_base", "final_select"],
                  f"got {names}"):
        failed += 1

    b_cte = reg.get("cte:acct_base")
    if not _check("CTE outputs include PARTY_ID",
                  b_cte is not None and "PARTY_ID" in b_cte.outputs,
                  f"outputs={b_cte.outputs if b_cte else None}"):
        failed += 1

    if not _check("CTE real_tables = {M_AC_ACCOUNT}",
                  b_cte is not None and b_cte.real_tables == {"M_AC_ACCOUNT"},
                  f"real_tables={b_cte.real_tables if b_cte else None}"):
        failed += 1

    b_fin = reg.get("final_select")
    if not _check("final_select depends_on ACCT_BASE",
                  b_fin is not None and "ACCT_BASE" in b_fin.depends_on,
                  f"depends_on={b_fin.depends_on if b_fin else None}"):
        failed += 1

    # 1-b: multiple CTEs
    sql2 = """
        WITH
        acct_base AS (
            SELECT party_id, party_id_mask, acct_nbr
            FROM DM_S_VIEW.M_AC_ACCOUNT
        ),
        txn_fund AS (
            SELECT ab.party_id_mask, SUM(t.txn_amt_twd) AS fund_amt
            FROM DM_S_VIEW.M_AT_FUND_TXN t
            JOIN acct_base ab ON ab.acct_nbr = t.acct_nbr
            GROUP BY ab.party_id_mask
        )
        SELECT m.party_id_mask, NVL(f.fund_amt, 0) AS fund_amt
        FROM acct_base m
        LEFT JOIN txn_fund f ON f.party_id_mask = m.party_id_mask
    """
    reg2 = _reg(sql2)
    names2 = [b.name for b in reg2.blocks]
    if not _check("two CTEs → 3 blocks in order",
                  names2 == ["cte:acct_base", "cte:txn_fund", "final_select"],
                  f"got {names2}"):
        failed += 1

    txn = reg2.get("cte:txn_fund")
    if not _check("txn_fund depends_on ACCT_BASE",
                  txn is not None and "ACCT_BASE" in txn.depends_on,
                  f"depends_on={txn.depends_on if txn else None}"):
        failed += 1

    if not _check("txn_fund real_tables = {M_AT_FUND_TXN}",
                  txn is not None and txn.real_tables == {"M_AT_FUND_TXN"},
                  f"real_tables={txn.real_tables if txn else None}"):
        failed += 1

    if not _check("txn_fund outputs include FUND_AMT",
                  txn is not None and "FUND_AMT" in txn.outputs,
                  f"outputs={txn.outputs if txn else None}"):
        failed += 1

    # 1-c: no CTE (plain SELECT)
    sql3 = "SELECT acct_nbr FROM DM_S_VIEW.M_AC_ACCOUNT WHERE branch_code = '123'"
    reg3 = _reg(sql3)
    names3 = [b.name for b in reg3.blocks]
    if not _check("no CTE → only final_select",
                  names3 == ["final_select"],
                  f"got {names3}"):
        failed += 1

    # 1-d: blocks are sorted by position
    sql4 = """
        WITH a AS (SELECT 1 AS x FROM DUAL),
             b AS (SELECT 2 AS y FROM DUAL)
        SELECT x, y FROM a JOIN b ON 1=1
    """
    reg4 = _reg(sql4)
    positions = [reg4.get(n).body_start for n in ["cte:a", "cte:b", "final_select"]]
    if not _check("blocks sorted by body_start",
                  positions == sorted(positions),
                  f"positions={positions}"):
        failed += 1

    return failed


# ── 2. tag_errors ─────────────────────────────────────────────────────

def test_tag_errors() -> int:
    print(f"\n{WIDE}\n  2. BlockRegistry.tag_errors\n{WIDE}")
    failed = 0

    sql = """
        WITH
        acct_base AS (
            SELECT party_id, party_id_mask, acct_nbr
            FROM DM_S_VIEW.M_AC_ACCOUNT
        ),
        txn_fund AS (
            SELECT ab.party_id_mask, SUM(t.txn_amt_twd) AS fund_amt
            FROM DM_S_VIEW.M_AT_FUND_TXN t
            JOIN acct_base ab ON ab.acct_nbr = t.acct_nbr
            GROUP BY ab.party_id_mask
        )
        SELECT m.party_id_mask, NVL(f.fund_amt, 0)
        FROM acct_base m
        LEFT JOIN txn_fund f ON f.party_id_mask = m.party_id_mask
    """
    reg = _reg(sql)

    cases = [
        (
            "hallucination on M_AT_FUND_TXN → cte:txn_fund",
            "[幻覺] 欄位不存在：M_AT_FUND_TXN.txn_amt_twd",
            "cte:txn_fund",
        ),
        (
            "hallucination on M_AC_ACCOUNT → cte:acct_base",
            "[幻覺] 欄位不存在：M_AC_ACCOUNT.acct_no",
            "cte:acct_base",
        ),
        (
            "Data Redaction → final_select",
            "[Data Redaction] 禁止 SELECT m.party_id",
            "final_select",
        ),
        (
            "mask misuse → final_select",
            '[語意錯誤] party_id_mask 不可 alias 為 "客戶姓名"',
            "final_select",
        ),
        (
            "Oracle quirk with CTE name → cte:txn_fund",
            "[Oracle quirk] SELECT 沒有 FROM (CTE: txn_fund)",
            "cte:txn_fund",
        ),
        (
            "sqlglot syntax error → untagged",
            "[sqlglot] parse error at line 1",
            None,
        ),
    ]

    for desc, error, expected_block in cases:
        tagged = reg.tag_errors([error])[0]
        if expected_block is None:
            ok = not tagged.startswith("[block=")
        else:
            ok = tagged.startswith(f"[block={expected_block}]")
        if not _check(desc, ok, f"tagged='{tagged}'"):
            failed += 1

    # column-only hallucination fallback: "欄位不存在於查詢中任何表格：COL"
    # table-name matching fails; body_sql search should find the block
    sql_plain = "SELECT cust_name FROM DM_S_VIEW.M_PT_CUSTOMER WHERE party_id = '123'"
    reg_plain = _reg(sql_plain)
    tagged_plain = reg_plain.tag_errors(["[幻覺] 欄位不存在於查詢中任何表格：CUST_NAME"])[0]
    if not _check(
        "unqualified hallucination col → body_sql fallback → final_select",
        tagged_plain.startswith("[block=final_select]"),
        f"tagged='{tagged_plain}'",
    ):
        failed += 1

    return failed


# ── 3. replace_block (splice-back) ────────────────────────────────────

def test_replace_block() -> int:
    print(f"\n{WIDE}\n  3. BlockRegistry.replace_block\n{WIDE}")
    failed = 0

    sql = (
        "WITH\n"
        "acct_base AS (\n"
        "  SELECT party_id, acct_nbr FROM DM_S_VIEW.M_AC_ACCOUNT\n"
        "),\n"
        "txn_fund AS (\n"
        "  SELECT ab.party_id_mask, SUM(t.txn_amt_twd) AS fund_amt\n"
        "  FROM DM_S_VIEW.M_AT_FUND_TXN t\n"
        "  JOIN acct_base ab ON ab.acct_nbr = t.acct_nbr\n"
        "  GROUP BY ab.party_id_mask\n"
        ")\n"
        "SELECT m.party_id_mask, NVL(f.fund_amt, 0)\n"
        "FROM acct_base m\n"
        "LEFT JOIN txn_fund f ON f.party_id_mask = m.party_id_mask"
    )
    reg = _reg(sql)

    # Replace txn_fund body (fix column name)
    new_txn_body = (
        "\n"
        "  SELECT ab.party_id_mask, SUM(t.txn_amount) AS fund_amt\n"
        "  FROM DM_S_VIEW.M_AT_FUND_TXN t\n"
        "  JOIN acct_base ab ON ab.acct_nbr = t.acct_nbr\n"
        "  GROUP BY ab.party_id_mask\n"
    )
    result = reg.replace_block("cte:txn_fund", new_txn_body)

    if not _check("acct_base unchanged after replace",
                  "SELECT party_id, acct_nbr FROM DM_S_VIEW.M_AC_ACCOUNT" in result):
        failed += 1

    if not _check("old column txn_amt_twd removed",
                  "txn_amt_twd" not in result):
        failed += 1

    if not _check("new column txn_amount present",
                  "txn_amount" in result):
        failed += 1

    if not _check("final_select unchanged after replace",
                  "LEFT JOIN txn_fund f ON f.party_id_mask = m.party_id_mask" in result):
        failed += 1

    if not _check("result is still valid SQL (sqlglot parses)",
                  _can_parse(result)):
        failed += 1

    # Replace final_select
    new_final = (
        "SELECT m.party_id_mask AS \"識別碼\", NVL(f.fund_amt, 0) AS \"基金金額\"\n"
        "FROM acct_base m\n"
        "LEFT JOIN txn_fund f ON f.party_id_mask = m.party_id_mask"
    )
    result2 = reg.replace_block("final_select", new_final)
    if not _check("final_select replaced with aliases",
                  '"基金金額"' in result2):
        failed += 1
    if not _check("CTE unchanged when final_select replaced",
                  "txn_amt_twd" in result2):
        failed += 1

    return failed


def _can_parse(sql: str) -> bool:
    try:
        import sqlglot
        sqlglot.parse_one(sql, dialect="oracle")
        return True
    except Exception:
        return False


# ── 4. apply_replacements (multi-block splice) ────────────────────────

def test_apply_replacements() -> int:
    print(f"\n{WIDE}\n  4. apply_replacements (multi-block)\n{WIDE}")
    failed = 0

    from agent.block_registry import BlockRegistry, apply_replacements

    sql = (
        "WITH\n"
        "a AS (\n"
        "  SELECT old_a FROM DM_S_VIEW.T1\n"
        "),\n"
        "b AS (\n"
        "  SELECT old_b FROM DM_S_VIEW.T2\n"
        ")\n"
        "SELECT old_final FROM a JOIN b ON 1=1"
    )
    reg = BlockRegistry(sql)

    blk_a = reg.get("cte:a")
    blk_b = reg.get("cte:b")
    blk_f = reg.get("final_select")

    reps = [
        (blk_a.body_start, blk_a.body_end, "\n  SELECT new_a FROM DM_S_VIEW.T1\n"),
        (blk_b.body_start, blk_b.body_end, "\n  SELECT new_b FROM DM_S_VIEW.T2\n"),
        (blk_f.body_start, blk_f.body_end, "SELECT new_final FROM a JOIN b ON 1=1"),
    ]
    result = apply_replacements(sql, reps)

    for token in ("new_a", "new_b", "new_final"):
        if not _check(f"'{token}' present after multi-block replace",
                      token in result, f"result snippet: {result[:200]}"):
            failed += 1

    for token in ("old_a", "old_b", "old_final"):
        if not _check(f"'{token}' removed after replace",
                      token not in result):
            failed += 1

    # Structural keywords preserved
    if not _check("WITH keyword preserved", "WITH" in result):
        failed += 1
    if not _check("AS ( ) structure preserved", "AS (" in result):
        failed += 1

    return failed


# ── 5. rewrite_context contract ───────────────────────────────────────

def test_rewrite_context() -> int:
    print(f"\n{WIDE}\n  5. BlockRegistry.rewrite_context\n{WIDE}")
    failed = 0

    sql = """
        WITH
        acct_base AS (
            SELECT party_id, party_id_mask, acct_nbr
            FROM DM_S_VIEW.M_AC_ACCOUNT
        ),
        txn_fund AS (
            SELECT ab.party_id_mask, SUM(t.txn_amt_twd) AS fund_amt
            FROM DM_S_VIEW.M_AT_FUND_TXN t
            JOIN acct_base ab ON ab.acct_nbr = t.acct_nbr
            GROUP BY ab.party_id_mask
        )
        SELECT m.party_id_mask, NVL(f.fund_amt, 0)
        FROM acct_base m
        LEFT JOIN txn_fund f ON f.party_id_mask = m.party_id_mask
    """
    reg = _reg(sql)

    ctx = reg.rewrite_context("cte:txn_fund")
    if not _check("rewrite_context returns non-empty dict", bool(ctx)):
        failed += 1
        return failed

    if not _check("context block_name correct",
                  ctx["block_name"] == "cte:txn_fund"):
        failed += 1

    if not _check("context real_tables includes M_AT_FUND_TXN",
                  "M_AT_FUND_TXN" in ctx["real_tables"]):
        failed += 1

    if not _check("context upstream_outputs has ACCT_BASE",
                  "ACCT_BASE" in ctx["upstream_outputs"]):
        failed += 1

    if not _check("context downstream_blocks includes final_select",
                  "final_select" in ctx["downstream_blocks"]):
        failed += 1

    # final_select context
    ctx_f = reg.rewrite_context("final_select")
    if not _check("final_select upstream_outputs has ACCT_BASE and TXN_FUND",
                  "ACCT_BASE" in ctx_f["upstream_outputs"]
                  and "TXN_FUND" in ctx_f["upstream_outputs"]):
        failed += 1

    return failed


# ── entry point ───────────────────────────────────────────────────────

def main() -> None:
    total_failed = 0
    total_failed += test_parse()
    total_failed += test_tag_errors()
    total_failed += test_replace_block()
    total_failed += test_apply_replacements()
    total_failed += test_rewrite_context()

    print(f"\n{WIDE}")
    if total_failed == 0:
        print("  BlockRegistry: all passed")
    else:
        print(f"  FAILED: {total_failed} tests")
    print(WIDE)
    sys.exit(total_failed)


if __name__ == "__main__":
    main()
