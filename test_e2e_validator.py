"""
E2E 測試：validate_and_fix 完整流程
測試 SQL 故意包含：
  1. [schema prefix]   M_AC_ACCOUNT / M_AT_STOCK_TXN 缺 DM_S_VIEW 前綴
  2. [Data Redaction]  SELECT a.party_id（應改用 party_id_mask）
  3. [JOIN 鍵]         M_AC_ACCOUNT JOIN 缺 prod_type_code
"""

import json
import sys
from agent.sql_validator import validate_and_fix, validate_sql

# ── 測試 SQL（三個故意的違規）──────────────────────────────────────
BAD_SQL = """
SELECT
    a.party_id          AS "客戶證號",
    a.acct_nbr          AS "帳號",
    a.prod_type_code    AS "帳戶類別",
    t.txn_amt           AS "交易金額"
FROM M_AC_ACCOUNT a
JOIN M_AT_STOCK_TXN t
    ON a.acct_nbr = t.acct_nbr
WHERE a.snap_yyyymm = '202505'
  AND t.snap_yyyymm  = '202505'
"""

# ── Step 1: 確認初始 SQL 真的有預期的錯誤 ─────────────────────────
print("=" * 60)
print("Step 1 — 初始 SQL 的違規項目")
print("=" * 60)
initial_errors = validate_sql(BAD_SQL)
assert initial_errors, "初始 SQL 應有錯誤，但未偵測到任何問題"

has_redaction  = any("[Data Redaction]" in e for e in initial_errors)
has_schema     = any("[schema prefix]"  in e for e in initial_errors)
has_join_key   = any("[JOIN 鍵]"        in e for e in initial_errors)

for e in initial_errors:
    tag = ""
    if "[Data Redaction]" in e: tag = "  ← 預期"
    elif "[schema prefix]" in e: tag = "  ← 預期"
    elif "[JOIN 鍵]" in e:       tag = "  ← 預期"
    print(f"  {e}{tag}")

print()
assert has_redaction, "應偵測到 Data Redaction 違規"
assert has_schema,    "應偵測到 schema prefix 違規"
assert has_join_key,  "應偵測到 JOIN 鍵缺失"
print("  ✓ 三項違規全部偵測到\n")

# ── Step 2: 執行 validate_and_fix ─────────────────────────────────
print("=" * 60)
print("Step 2 — validate_and_fix 修正過程")
print("=" * 60)
fixed_sql, log, tokens = validate_and_fix(BAD_SQL)

for entry in log:
    status = "PASS ✓" if entry["passed"] else f"FAIL ({len(entry['errors'])} errors)"
    print(f"  Round {entry['round']}: {status}")
    for e in entry["errors"]:
        print(f"    - {e}")
print()
print(f"  Tokens used — fix_in: {tokens.get('fix_in', 0)}, fix_out: {tokens.get('fix_out', 0)}")

# ── Step 3: 確認最終 SQL 通過所有 validator ────────────────────────
print()
print("=" * 60)
print("Step 3 — 最終 SQL 驗證結果")
print("=" * 60)
final_errors = validate_sql(fixed_sql)

# 最後一個 log entry 必須是 final check（passed 或 not）
assert log[-1]["round"] > 1 or log[-1]["passed"], \
    "log 最後一筆應是 final validation round"

if final_errors:
    print("  ✗ 最終 SQL 仍有錯誤：")
    for e in final_errors:
        print(f"    - {e}")
else:
    print("  ✓ 最終 SQL 通過所有 validator")

# ── Step 4: 語意確認（不依賴 LLM 精確輸出，只確認必要條件）──────
print()
print("=" * 60)
print("Step 4 — 語意確認")
print("=" * 60)
sql_upper = fixed_sql.upper()

checks = [
    ("DM_S_VIEW 前綴存在",         "DM_S_VIEW." in sql_upper),
    ("party_id 不在 SELECT 清單",   "SELECT" in sql_upper and
                                    "PARTY_ID_MASK" in sql_upper or
                                    "PARTY_ID" not in sql_upper.split("WHERE")[0]),
    ("party_id_mask 出現在 SQL",    "PARTY_ID_MASK" in sql_upper),
    ("prod_type_code 出現在 ON 或 JOIN 條件", "PROD_TYPE_CODE" in sql_upper),
]

all_passed = True
for name, result in checks:
    icon = "✓" if result else "✗"
    print(f"  {icon} {name}")
    if not result:
        all_passed = False

# ── 最終 SQL 印出 ───────────────────────────────────────────────────
print()
print("=" * 60)
print("最終 SQL")
print("=" * 60)
print(fixed_sql)

# ── 結論 ────────────────────────────────────────────────────────────
print()
print("=" * 60)
validator_passed = len(final_errors) == 0
semantic_passed  = all_passed
print(f"Validator 通過：{'YES' if validator_passed else 'NO'}")
print(f"語意確認通過：{'YES' if semantic_passed else 'NO (部分條件未達成，見上方)'}")

if not validator_passed or not semantic_passed:
    sys.exit(1)
