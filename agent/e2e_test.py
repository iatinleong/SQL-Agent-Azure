"""端對端測試：planner 母體確立 + 完整 SQL 生成品質驗證。

測試策略：
  e2e-01  planner only：母體不明確 → status="ask"
  e2e-02  planner only：母體明確   → base_population 不為空
  e2e-03  validator unit：未來 YYYYMM → rule-based [語意] 錯誤
  e2e-04  full pipeline：ABC客群 + bps市佔率 → SQL 含正確公式與表格
  e2e-05  full pipeline：最新快照  → MAX(snap_yyyymm) + 無未來月份
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ALL_CASES_PATH, GENERATION_MODEL
from .experiment_logger import log_experiment

SEP = "─" * 65
WIDE_SEP = "═" * 65
OK = "✓"
NG = "✗"


# ── 資料結構 ───────────────────────────────────────────────────────

@dataclass
class _Check:
    label: str
    ok: bool
    detail: str = ""


@dataclass
class E2ECase:
    id: str
    label: str
    requirement: str = ""
    # planner 驗證
    expect_plan_status: str = "confirm"
    check_base_population: bool = True
    skip_generate: bool = False          # planner only，不生成 SQL
    # SQL 驗證（full pipeline）
    sql_must_match: list[str] = field(default_factory=list)     # re.search pattern
    sql_must_not_match: list[str] = field(default_factory=list)
    check_no_future_yyyymm: bool = True
    # 直接 validator 單元測試（不走 pipeline）
    direct_sql: str | None = None
    direct_expect_error_prefixes: list[str] = field(default_factory=list)


# ── 測試案例定義 ───────────────────────────────────────────────────

CASES: list[E2ECase] = [
    E2ECase(
        id="e2e-01",
        label="母體不明確 → planner 應 ask",
        requirement="幫我出一份客戶名單",
        expect_plan_status="ask",
        check_base_population=False,
        skip_generate=True,
    ),
    E2ECase(
        id="e2e-02",
        label="母體明確 → base_population 不為空",
        requirement="查本月南港分公司所有有效台股帳號及最後交易日",
        expect_plan_status="confirm",
        check_base_population=True,
        skip_generate=True,
    ),
    E2ECase(
        id="e2e-03",
        label="未來 YYYYMM → rule-based [語意] 偵測",
        direct_sql="""
            WITH base AS (
                SELECT acct_nbr, branch_code
                FROM DM_S_VIEW.M_AC_ACCOUNT
                WHERE snap_yyyymm = '202612'
                  AND acct_valid_flag = 'Y'
            )
            SELECT * FROM base
        """,
        direct_expect_error_prefixes=["[語意]"],
    ),
    E2ECase(
        id="e2e-06",
        label="外部 schema 表欄位 → 不報幻覺（S_ARIELSHAO.* / S_CHIAHSUANHSU.*）",
        direct_sql="""
            WITH grp AS (
                SELECT acct_nbr
                FROM S_ARIELSHAO.CUSTOMER_GROUP_2026Q1
                WHERE NVL(customer_group,'Z') IN ('A','B','C')
            ),
            names AS (
                SELECT DISTINCT acct_nbr, party_name
                FROM S_CHIAHSUANHSU.PARTY_NAME
            ),
            base AS (
                SELECT a.acct_nbr, a.branch_code
                FROM DM_S_VIEW.M_AC_ACCOUNT a
                INNER JOIN grp g ON a.acct_nbr = g.acct_nbr
                WHERE a.prod_type_code = '100'
                  AND a.acct_valid_flag = 'Y'
            )
            SELECT b.acct_nbr, n.party_name
            FROM base b
            LEFT JOIN names n ON b.acct_nbr = n.acct_nbr
        """,
        direct_expect_error_prefixes=[],  # 不應有任何幻覺錯誤
    ),
    E2ECase(
        id="e2e-04",
        label="bps市佔率（完整流程）",
        requirement=(
            "查2025年整年度南港分公司所有有台股現貨交易的帳號，"
            "計算每個帳號2025全年成交量加總，"
            "以及以全市場2025全年台股現貨總成交量為分母算出的年度市佔率（單位bps，即×10000）"
        ),
        expect_plan_status="confirm",
        check_base_population=True,
        sql_must_match=[
            r"10000",               # bps = × 10000
            r"MARKET_STOCK_TXNS",   # 市場總成交量
            r"M_RF_MARKET_SHARE",   # 市佔參考表
        ],
        check_no_future_yyyymm=True,
    ),
    E2ECase(
        id="e2e-05",
        label="最新快照 → SNAP_YYYYMM 降序或 MAX + 無未來月份（完整流程）",
        requirement=(
            "查最新期全公司所有有效台股帳號清單，"
            "顯示帳號、客戶姓名、最後交易日"
        ),
        expect_plan_status="confirm",
        check_base_population=True,
        sql_must_match=[
            # 接受 MAX(SNAP_YYYYMM) 或 ORDER BY SNAP_YYYYMM DESC（兩者都是正確的最新快照寫法）
            r"(?:MAX\s*\(\s*SNAP_YYYYMM\s*\)|SNAP_YYYYMM\s+DESC)",
            r"SYSDATE",             # T-1 邏輯
        ],
        check_no_future_yyyymm=True,
    ),
]


# ── Pipeline helpers ───────────────────────────────────────────────

def _load_all_cases() -> list[dict]:
    with open(ALL_CASES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _run_planner(requirement: str, all_cases: list[dict]):
    """Phase 1 + Phase 2 + plan_report。回傳 (plan, phase1_log_str)。"""
    from .reader import normalize_requirement
    from .retriever import retrieve
    from .entity_extractor import extract_entities
    from .table_retriever import retrieve_tables
    from .schema_summarizer import load_table_summaries
    from .generator import (
        _get_union_tables,
        _load_schema_for_tables,
        _get_case_sql_text,
        _load_metrics_text,
        _load_business_skills_text,
    )
    from .report_planner import plan_report

    req_text = normalize_requirement(requirement)
    available = set(load_table_summaries().keys())

    hits = retrieve(req_text, all_cases, top_k=5)
    if not hits:
        raise RuntimeError("retrieve() 回傳空結果，請先執行 --summarize")

    extraction = extract_entities(req_text)
    semantic_tables = retrieve_tables(req_text, top_n=5)

    candidate_set = set(_get_union_tables(hits, all_cases, available))
    candidate_set.update(t for t in semantic_tables if t in available)
    for t in extraction.extra_tables:
        if t in available:
            candidate_set.add(t)

    schema_for_plan = _load_schema_for_tables(sorted(candidate_set))
    case_sqls = [_get_case_sql_text(h.case_id, all_cases) for h in hits]
    metrics_text = _load_metrics_text(req_text)
    skills_text = _load_business_skills_text(req_text, scene="")

    plan = plan_report(
        req_text, case_sqls,
        entities_text=extraction.enriched_entities,
        schema_text=schema_for_plan,
        metrics_text=metrics_text,
        skills_text=skills_text,
    )

    phase1_info = (
        f"hits=[{', '.join(h.case_id for h in hits)}]  "
        f"candidates={len(candidate_set)}"
    )
    return plan, hits, all_cases, phase1_info


def _run_full(requirement: str, all_cases: list[dict]):
    """Full pipeline: planner + generate。回傳 (plan, gen)。"""
    from .generator import generate
    from .report_planner import fmt_plan_for_prompt
    from .classifier import classify_intent
    from .pool_filter import resolve_secondary_scene

    plan, hits, _cases, _ = _run_planner(requirement, all_cases)

    classification, _ = classify_intent(requirement)
    scene = classification.主要場景

    report_plan_text = fmt_plan_for_prompt(plan) if plan.status == "confirm" else ""
    gen = generate(
        requirement, hits, _cases,
        model=GENERATION_MODEL,
        scene=scene,
        report_plan_text=report_plan_text,
        forced_tables=plan.tables if plan.tables else None,
    )
    return plan, gen


# ── 單案執行 ──────────────────────────────────────────────────────

def _run_case(case: E2ECase, all_cases: list[dict]) -> list[_Check]:
    checks: list[_Check] = []

    # ── 直接 validator 單元測試 ─────────────────────────────────────
    if case.direct_sql is not None:
        from .sql_validator import validate_sql, _clean
        errors = validate_sql(_clean(case.direct_sql))
        hallucination_errors = [e for e in errors if e.startswith("[幻覺]")]
        if case.direct_expect_error_prefixes:
            # 有指定應出現的錯誤
            for prefix in case.direct_expect_error_prefixes:
                hit = next((e for e in errors if e.startswith(prefix)), None)
                checks.append(_Check(
                    f"validator 出現 {prefix}",
                    hit is not None,
                    hit or f"errors={errors[:3]}",
                ))
        else:
            # 空 list 代表期望無幻覺錯誤
            checks.append(_Check(
                "無 [幻覺] 錯誤（外部 schema 表白名單正常）",
                len(hallucination_errors) == 0,
                "; ".join(hallucination_errors) if hallucination_errors else "(clean)",
            ))
        return checks

    # ── Planner only ────────────────────────────────────────────────
    plan, hits, _cases, phase1_info = _run_planner(case.requirement, all_cases)
    print(f"  planner: status={plan.status}  base='{plan.base_population[:40]}...' "
          f"  {phase1_info}")
    if plan.status == "ask":
        print(f"  question: {plan.question}")

    checks.append(_Check(
        f"plan.status == '{case.expect_plan_status}'",
        plan.status == case.expect_plan_status,
        f"actual={plan.status}  q={plan.question[:60] if plan.question else ''}",
    ))
    if case.check_base_population:
        bp = (plan.base_population or "").strip()
        checks.append(_Check(
            "plan.base_population 不為空",
            bool(bp),
            f"'{bp[:60]}'" if bp else "空字串",
        ))

    if case.skip_generate:
        return checks

    # ── Full pipeline ────────────────────────────────────────────────
    if plan.status == "ask":
        checks.append(_Check(
            "跳過 generate（planner 仍在 ask）",
            False,
            "planner 回傳 ask，無法進入生成",
        ))
        return checks

    from .generator import generate
    from .report_planner import fmt_plan_for_prompt
    from .classifier import classify_intent

    classification, _ = classify_intent(case.requirement)
    scene = classification.主要場景
    report_plan_text = fmt_plan_for_prompt(plan)
    gen = generate(
        case.requirement, hits, _cases,
        model=GENERATION_MODEL,
        scene=scene,
        report_plan_text=report_plan_text,
        forced_tables=plan.tables if plan.tables else None,
    )
    sql = gen.final_sql or ""

    for pattern in case.sql_must_match:
        found = bool(re.search(pattern, sql, re.IGNORECASE))
        checks.append(_Check(
            f"SQL 含 /{pattern}/i",
            found,
            "(found)" if found else f"(not found, sql length={len(sql)})",
        ))

    for pattern in case.sql_must_not_match:
        found = bool(re.search(pattern, sql, re.IGNORECASE))
        checks.append(_Check(
            f"SQL 不含 /{pattern}/i",
            not found,
            "(absent)" if not found else "(FOUND — unexpected)",
        ))

    if case.check_no_future_yyyymm:
        from .sql_validator import _check_future_yyyymm, _clean
        future_errs = _check_future_yyyymm(_clean(sql))
        checks.append(_Check(
            "無未來 YYYYMM",
            len(future_errs) == 0,
            "; ".join(future_errs) if future_errs else "(clean)",
        ))

    return checks


# ── 批次執行 + 報告 ────────────────────────────────────────────────

def run_e2e() -> list[dict]:
    all_cases = _load_all_cases()
    summary: list[dict] = []

    for case in CASES:
        print(f"\n{SEP}")
        print(f"  [{case.id}] {case.label}")
        if case.requirement:
            print(f"  需求：{case.requirement[:80]}")

        try:
            checks = _run_case(case, all_cases)
        except Exception as exc:
            checks = [_Check(f"執行失敗：{exc}", False)]

        for chk in checks:
            icon = OK if chk.ok else NG
            detail = f"  → {chk.detail}" if chk.detail else ""
            print(f"    {icon}  {chk.label}{detail}")

        passed = sum(1 for c in checks if c.ok)
        total = len(checks)
        summary.append({
            "id": case.id,
            "label": case.label,
            "passed": passed,
            "total": total,
            "all_ok": passed == total,
        })

    # ── 摘要 ────────────────────────────────────────────────────────
    print(f"\n{WIDE_SEP}")
    print("  E2E 測試結果")
    print(f"  {'ID':<10}  {'通過/總計':<10}  {'標籤'}")
    print(f"  {'─'*10}  {'─'*10}  {'─'*30}")
    all_pass = True
    for r in summary:
        icon = OK if r["all_ok"] else NG
        print(f"  {icon}  {r['id']:<8}  {r['passed']}/{r['total']:<8}  {r['label']}")
        if not r["all_ok"]:
            all_pass = False
    total_checks = sum(r["total"] for r in summary)
    total_passed = sum(r["passed"] for r in summary)
    print(f"\n  總計：{total_passed}/{total_checks} 通過  "
          f"{'全部通過 ✓' if all_pass else '有失敗項目 ✗'}")
    print(WIDE_SEP)

    return summary


def main() -> None:
    with log_experiment("e2e_test") as log:
        results = run_e2e()
        log["results"] = results


if __name__ == "__main__":
    main()
