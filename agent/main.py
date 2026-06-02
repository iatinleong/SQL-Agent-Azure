"""CLI 入口。

指令：
  python -m agent                          單筆範例（Phase 1 + Phase 2）
  python -m agent "需求文字"                自由文字（Phase 1 + Phase 2）
  python -m agent path/to/req.txt          讀檔（Phase 1 + Phase 2）
  python -m agent --generate "需求文字"    完整生成（Phase 1 + Phase 2 + Step A + Step B）
  python -m agent --summarize              批次產出所有案例的業務摘要（需先跑一次）
  python -m agent --test                   10 案例批次評測（Phase 1 + Phase 2）
  python -m agent --schema-summarize          批次產出所有 schema 表格說明
  python -m agent --schema-summarize TABLE    只跑指定表格
  python -m agent --eval-table-selection      LLM table selection 準確度評測
"""

from __future__ import annotations

import json
import sys
from typing import Union

import json as _json

from .classifier import classify_intent
from .config import ALL_CASES_PATH
from .experiment_logger import log_experiment
from .pool_filter import resolve_secondary_scene
from .reader import normalize_requirement
from .retriever import retrieve

SEP = "─" * 62


def run(requirement: Union[str, dict]) -> tuple | None:
    """Phase 1 + Phase 2。回傳 (hits, all_cases, primary_scene) 供後續生成使用。"""
    with open(ALL_CASES_PATH, encoding="utf-8") as f:
        all_cases = _json.load(f)

    req_text = normalize_requirement(requirement)
    print(f"需求：{req_text[:80]}{'...' if len(req_text) > 80 else ''}\n")

    # Phase 1
    print("=== Phase 1：場景分類 ===")
    classification, _clf_tok = classify_intent(requirement)
    print(f"主要場景：{classification.主要場景}")
    secondary = resolve_secondary_scene(classification)
    if classification.次要場景:
        status = "採用" if secondary else "捨棄（gap ≥ 0.4）"
        print(f"次要場景：{classification.次要場景}（{status}）")
    print(f"分類理由：{classification.分類理由}")
    print("\n各場景置信度：")
    for item in sorted(classification.各標籤置信度, key=lambda x: x.分數, reverse=True):
        bar = "█" * round(item.分數 * 24) + "░" * (24 - round(item.分數 * 24))
        tag = " <<主" if item.標籤 == classification.主要場景 else (" <<次" if item.標籤 == classification.次要場景 else "")
        print(f"  {item.標籤:<16} {item.分數:.2f}  {bar}{tag}")

    # Phase 2
    print("\n=== Phase 2：向量檢索 Top-5 ===")
    hits = retrieve(req_text, all_cases, top_k=5)
    if not hits:
        print("  （尚未建立摘要，請先執行 --summarize）")
        return None

    case_map = {str(c.get("資料夾")): c for c in all_cases}
    for hit in hits:
        c = case_map.get(hit.case_id, {})
        summary = (c.get("需求") or {}).get("需求摘要", "")
        scene = (c.get("業務場景") or {}).get("業務場景", "")
        print(f"  #{hit.rank}  [{hit.case_id}]  score={hit.score:.4f}")
        print(f"       {summary[:55]}")
        print(f"       場景：{scene}")

    return hits, all_cases, classification.主要場景


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]

    if args and args[0] == "--summarize":
        from .summarizer import build_summaries
        # --summarize 143        → 只跑 case 143
        # --summarize            → 全部
        # --summarize --force    → 強制重跑全部
        case_ids = [a for a in args[1:] if not a.startswith("--")] or None
        build_summaries(case_ids=case_ids, force="--force" in args)
        return

    if args and args[0] == "--test":
        from .batch_test import main as batch_main
        batch_main()
        return

    if args and args[0] == "--eval-retrieval":
        from .eval_retrieval import main as eval_main
        eval_main()
        return

    if args and args[0] == "--eval-retrieval-overlap":
        from .eval_retrieval_table_overlap import main as eval_overlap_main
        json_path = next((a for a in args[1:] if not a.startswith("--")), None)
        eval_overlap_main(retrieval_json=json_path)
        return

    if args and args[0] == "--eval-table-selection":
        from .eval_table_selection import main as eval_ts_main
        eval_ts_main(use_raw_schema="--raw-schema" in args)
        return

    if args and args[0] == "--schema-summarize":
        from .schema_summarizer import build_table_summaries
        table_names = [a for a in args[1:] if not a.startswith("--")] or None
        build_table_summaries(table_names=table_names, force="--force" in args)
        return

    if args and args[0] == "--generate":
        from .generator import generate
        from .config import GENERATION_MODEL
        rest = args[1:]
        model = next((a.split("=", 1)[1] for a in rest if a.startswith("--model=")), GENERATION_MODEL)
        text_args = [a for a in rest if not a.startswith("--")]
        if not text_args:
            print("用法：python -m agent --generate \"需求文字\"")
            return
        req_arg = text_args[0]
        try:
            with open(req_arg, encoding="utf-8") as f:
                requirement: Union[str, dict] = f.read().strip()
        except OSError:
            requirement = req_arg

        with log_experiment("generate") as log:
            result = run(requirement)
            if result is None:
                return
            hits, all_cases, primary_scene = result
            gen = generate(normalize_requirement(requirement), hits, all_cases, model=model, scene=primary_scene)
            log["candidate_tables"] = gen.candidate_tables
            log["step_a_sql"] = gen.step_a_sql
            log["step_a_reasoning"] = gen.step_a_reasoning
            log["final_reasoning"] = gen.final_reasoning
            log["final_sql"] = gen.final_sql
            log["tokens"] = gen.tokens
        return

    with log_experiment("single_query") as _:
        if not args:
            sample = "幫我拉南港分公司台股交易量排名"
            print(f"(使用內建範例需求：{sample})\n")
            run(sample)
            return

        arg = args[0]
        try:
            with open(arg, encoding="utf-8") as f:
                content = f.read().strip()
            try:
                requirement: Union[str, dict] = json.loads(content)
            except json.JSONDecodeError:
                requirement = content
        except OSError:
            requirement = arg

        run(requirement)
