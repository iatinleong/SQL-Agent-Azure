"""Data Redaction 兩種 e2e 測試：
1. 靜態 validator 單元測試（不呼叫 LLM）
2. validate_and_fix 修正迴圈測試（呼叫 LLM，驗證壞 SQL 能被修好）
"""

from __future__ import annotations

import sys
import textwrap

# ── 1. 靜態 validator 測試（不呼叫 LLM）─────────────────────────────

def run_static_tests() -> int:
    from agent.sql_validator import _check_data_redaction

    cases = [
        # (描述, sql, 期望至少包含的錯誤關鍵字, 期望無錯)
        (
            "SELECT party_id，來源表無 party_id_mask → 建議 JOIN M_AC_ACCOUNT",
            "SELECT c.party_id, c.cust_name FROM DM_S_VIEW.M_PT_CUSTOMER c",
            ["禁止 SELECT c.party_id", "party_id_mask"],
            False,
        ),
        (
            "t.party_id = w.party_id_mask JOIN 比較 → 報錯",
            textwrap.dedent("""
                SELECT t.acct_nbr FROM DM_S_VIEW.M_AT_STOCK_TXN t
                JOIN DM_S_VIEW.M_PT_CUSTOMER w ON t.party_id = w.party_id_mask
            """),
            ["禁止 t.party_id = w.party_id_mask"],
            False,
        ),
        (
            "party_id IN (SELECT party_id_mask ...) → 報錯",
            textwrap.dedent("""
                SELECT cust_name FROM DM_S_VIEW.M_PT_CUSTOMER
                WHERE party_id IN (
                    SELECT party_id_mask FROM DM_S_VIEW.M_AC_ACCOUNT
                    WHERE acct_valid_flag = 'Y'
                )
            """),
            ["IN (... party_id_mask ...)"],
            False,
        ),
        (
            "反向：party_id_mask IN (SELECT party_id ...) → 報錯",
            textwrap.dedent("""
                SELECT acct_nbr FROM DM_S_VIEW.M_AC_ACCOUNT
                WHERE party_id_mask IN (
                    SELECT party_id FROM DM_S_VIEW.M_PT_CUSTOMER WHERE valid_flag = 'Y'
                )
            """),
            ["IN (... party_id ...)"],
            False,
        ),
        (
            "正確：SELECT party_id_mask，party_id 只用於 JOIN → 無錯",
            textwrap.dedent("""
                SELECT pc.party_id_mask, COUNT(*) AS acct_cnt
                FROM DM_S_VIEW.M_PT_CUSTOMER pc
                JOIN DM_S_VIEW.M_AC_ACCOUNT a ON pc.party_id = a.party_id
                WHERE pc.valid_flag = 'Y'
                GROUP BY pc.party_id_mask
            """),
            [],
            True,
        ),
        (
            "正確：EXISTS + party_id JOIN → 無錯",
            textwrap.dedent("""
                SELECT cust_name FROM DM_S_VIEW.M_PT_CUSTOMER c
                WHERE EXISTS (
                    SELECT 1 FROM DM_S_VIEW.M_AC_ACCOUNT a
                    WHERE a.party_id = c.party_id AND a.acct_valid_flag = 'Y'
                )
            """),
            [],
            True,
        ),
        (
            "正確：party_id 只在 OVER(PARTITION BY) 內，SELECT 輸出用 party_id_mask → 無錯",
            textwrap.dedent("""
                WITH base AS (
                    SELECT
                        a.party_id_mask,
                        MIN(a.open_date) OVER (PARTITION BY a.party_id) AS min_open_date,
                        ROW_NUMBER() OVER (PARTITION BY a.party_id ORDER BY a.open_date) AS rn
                    FROM DM_S_VIEW.M_AC_ACCOUNT a
                    JOIN DM_S_VIEW.M_PT_CUSTOMER b ON a.party_id = b.party_id
                )
                SELECT party_id_mask, min_open_date FROM base WHERE rn = 1
            """),
            [],
            True,
        ),
    ]

    passed = failed = 0
    SEP = "─" * 65
    print(f"\n{'═' * 65}")
    print("  靜態 validator 測試（不呼叫 LLM）")
    print(f"{'═' * 65}")

    for desc, sql, expect_keywords, expect_no_error in cases:
        errors = _check_data_redaction(sql)
        if expect_no_error:
            ok = len(errors) == 0
        else:
            ok = all(any(kw in e for e in errors) for kw in expect_keywords)

        mark = "✅" if ok else "❌"
        print(f"{SEP}\n{mark}  {desc}")
        if not ok:
            print(f"   期望關鍵字：{expect_keywords}")
            print(f"   實際錯誤  ：{errors}")
            failed += 1
        else:
            passed += 1

    print(f"\n{'═' * 65}")
    print(f"  結果：{passed} 通過 / {failed} 失敗")
    print(f"{'═' * 65}\n")
    return failed


# ── 2. validate_and_fix 修正迴圈測試（呼叫 LLM）────────────────────

def run_fix_loop_tests() -> int:
    from agent.sql_validator import validate_and_fix
    from agent.config import VALIDATOR_MODEL

    bad_sqls = [
        (
            "壞 SQL 1：SELECT party_id + t.party_id = w.party_id_mask JOIN",
            textwrap.dedent("""
                SELECT t.party_id, COUNT(*) AS txn_cnt
                FROM DM_S_VIEW.M_AT_STOCK_TXN t
                JOIN DM_S_VIEW.M_PT_CUSTOMER w
                  ON t.party_id = w.party_id_mask
                GROUP BY t.party_id
            """),
        ),
        (
            "壞 SQL 2：party_id IN (SELECT party_id_mask ...)",
            textwrap.dedent("""
                SELECT cust_name FROM DM_S_VIEW.M_PT_CUSTOMER
                WHERE party_id IN (
                    SELECT party_id_mask FROM DM_S_VIEW.M_AC_ACCOUNT
                    WHERE acct_valid_flag = 'Y'
                )
            """),
        ),
    ]

    passed = failed = 0
    SEP = "─" * 65
    print(f"\n{'═' * 65}")
    print(f"  validate_and_fix 修正迴圈測試（model: {VALIDATOR_MODEL}）")
    print(f"{'═' * 65}")

    for desc, bad_sql in bad_sqls:
        print(f"{SEP}\n📝  {desc}")
        final_sql, log, _ = validate_and_fix(bad_sql, model=VALIDATOR_MODEL)

        round_entries = [e for e in log if "round" in e]
        last_round = round_entries[-1] if round_entries else {"errors": [], "passed": False}
        final_errors = last_round.get("errors", [])
        ok = last_round.get("passed", False)

        for entry in log:
            if "auto_fixes" in entry:
                for msg in entry["auto_fixes"]:
                    print(f"   [auto-fix] {msg}")
                continue
            status = "✅" if entry["passed"] else f"❌ {len(entry['errors'])} 個問題"
            print(f"   Round {entry['round']}：{status}")
            if not entry["passed"]:
                for e in entry["errors"]:
                    print(f"      {e}")

        if ok:
            print(f"   最終 SQL（前3行）：")
            for line in final_sql.strip().splitlines()[:3]:
                print(f"      {line}")
            passed += 1
        else:
            print(f"   ❌ 修正失敗，殘留錯誤：{final_errors}")
            failed += 1

    print(f"\n{'═' * 65}")
    print(f"  結果：{passed} 通過 / {failed} 失敗")
    print(f"{'═' * 65}\n")
    return failed


# ── 入口 ─────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    run_llm = "--llm" in args or "--all" in args
    run_static = "--static" in args or "--all" in args or not args

    total_failed = 0
    if run_static:
        total_failed += run_static_tests()
    if run_llm:
        total_failed += run_fix_loop_tests()

    if not run_llm:
        print("（LLM 修正迴圈測試未執行，加 --llm 或 --all 啟用）")

    sys.exit(total_failed)


if __name__ == "__main__":
    main()
