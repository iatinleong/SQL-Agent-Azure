"""sync_cases.py — 同步 SQL題目/ 新資料夾到系統。

每次執行流程：
1. 掃描 SQL題目/ 找出 all_cases.json 中缺少的資料夾
2. 對每個新案例：
   a. 讀取 報表需求.txt（JSON 或純文字皆可）
   b. 遞迴收集資料夾內所有 .sql 檔
   c. 呼叫 classify_intent() 決定業務場景
   d. 寫入 all_cases.json
   e. 呼叫 summarize_case() 產出摘要 → case_summaries/<id>.txt
3. 重建 all_cases_embeddings.npz（向量索引）

用法：
    python sync_cases.py            # 只處理新增案例
    python sync_cases.py --dry-run  # 只列出缺少的資料夾，不實際執行
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Windows cp950 終端 → 強制 UTF-8 輸出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ── 路徑常數 ─────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
SQL_CASES_DIR = BASE_DIR / "SQL題目"
ALL_CASES_PATH = BASE_DIR / "all_cases.json"
SUMMARIES_DIR = BASE_DIR / "case_summaries"
EMBEDDINGS_PATH = BASE_DIR / "all_cases_embeddings.npz"

SEP = "─" * 62


# ── 工具函式 ─────────────────────────────────────────────────────────

def load_all_cases() -> list[dict]:
    with open(ALL_CASES_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_all_cases(cases: list[dict]) -> None:
    with open(ALL_CASES_PATH, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)


def find_missing_folders(all_cases: list[dict]) -> list[Path]:
    """回傳 SQL題目/ 中有但 all_cases.json 沒有的資料夾清單。"""
    existing_ids = {str(c.get("資料夾", "")) for c in all_cases}
    missing = []
    for folder in sorted(SQL_CASES_DIR.iterdir()):
        if folder.is_dir() and folder.name not in existing_ids:
            missing.append(folder)
    return missing


def read_requirement(folder: Path) -> dict:
    """讀取 報表需求.txt，自動判斷 JSON 或純文字格式。"""
    req_file = folder / "報表需求.txt"
    if not req_file.exists():
        return {"需求摘要": f"（{folder.name}）", "欄位": [], "篩選條件": []}

    text = req_file.read_text(encoding="utf-8").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 純文字：整段視為需求摘要
    return {"需求摘要": text, "欄位": [], "篩選條件": []}


def collect_sql_files(folder: Path) -> list[dict]:
    """遞迴收集資料夾內所有 .sql 檔，回傳 [{"檔名": ..., "內容": ...}]。"""
    results = []
    for sql_file in sorted(folder.rglob("*.sql")):
        content = sql_file.read_text(encoding="utf-8", errors="replace")
        results.append({"檔名": sql_file.name, "內容": content})
    return results


def classify_scene(requirement: dict) -> dict:
    """呼叫 LLM 分類業務場景，回傳符合 all_cases.json 格式的 業務場景 dict。"""
    from agent.classifier import classify_intent

    req_text = requirement.get("需求摘要", "")
    if not req_text:
        return {"業務場景": "（未分類）", "分類理由": "", "各標籤置信度": []}

    clf, _ = classify_intent(req_text)
    return {
        "業務場景": clf.主要場景,
        "分類理由": clf.分類理由,
        "各標籤置信度": [
            {"標籤": item.標籤, "分數": item.分數}
            for item in clf.各標籤置信度
        ],
    }


def build_case_entry(folder: Path, requirement: dict, sql_list: list[dict], scene: dict) -> dict:
    return {
        "資料夾": folder.name,
        "需求": requirement,
        "SQL": sql_list,
        "業務場景": scene,
    }


def rebuild_embeddings() -> None:
    """重建向量索引（刪除舊 npz，重新 encode 所有摘要）。"""
    from agent.embedding import encode
    from agent.summarizer import load_summaries
    import numpy as np

    if EMBEDDINGS_PATH.exists():
        EMBEDDINGS_PATH.unlink()

    summaries = load_summaries()
    if not summaries:
        print("  [Embeddings] 無摘要可向量化，略過")
        return

    print(f"  [Embeddings] 重新 encode {len(summaries)} 筆摘要...")
    ids = list(summaries.keys())
    vecs = encode(
        [summaries[cid] for cid in ids],
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    import numpy as np
    np.savez(EMBEDDINGS_PATH, ids=np.array(ids), vecs=vecs)
    print(f"  [Embeddings] 已存至 {EMBEDDINGS_PATH.name}（{len(ids)} 筆）")


# ── 主流程 ───────────────────────────────────────────────────────────

def sync(dry_run: bool = False) -> None:
    SUMMARIES_DIR.mkdir(exist_ok=True)

    all_cases = load_all_cases()
    missing = find_missing_folders(all_cases)

    if not missing:
        print("所有 SQL題目/ 資料夾已在 all_cases.json 中，無需同步。")
        return

    print(f"找到 {len(missing)} 個新資料夾：")
    for f in missing:
        print(f"  • {f.name}")

    if dry_run:
        print("\n（dry-run 模式，不實際執行）")
        return

    print()
    new_cases: list[dict] = []
    for folder in missing:
        print(f"{SEP}")
        print(f"處理：{folder.name}")

        # 1. 讀取需求
        requirement = read_requirement(folder)
        req_summary = requirement.get("需求摘要", "")[:60]
        print(f"  需求：{req_summary}{'...' if len(requirement.get('需求摘要','')) > 60 else ''}")

        # 2. 收集 SQL
        sql_list = collect_sql_files(folder)
        print(f"  SQL 檔：{len(sql_list)} 個（{', '.join(s['檔名'] for s in sql_list)}）")
        if not sql_list:
            print("  ⚠️  無 SQL 檔，跳過此資料夾")
            continue

        # 3. 分類業務場景
        print("  分類業務場景中...")
        scene = classify_scene(requirement)
        print(f"  → {scene.get('業務場景', '（未分類）')}")

        # 4. 建立 case entry 並寫入 all_cases.json
        entry = build_case_entry(folder, requirement, sql_list, scene)
        all_cases.append(entry)
        save_all_cases(all_cases)
        print(f"  ✅ 已寫入 all_cases.json")

        # 5. 產出摘要
        from agent.summarizer import summarize_case, get_summary_path
        print("  產出業務摘要中...")
        summary = summarize_case(entry)
        summary_path = get_summary_path(folder.name)
        summary_path.write_text(summary, encoding="utf-8")
        print(f"  ✅ 摘要已存至 {summary_path.name}")
        print(f"     {summary[:80]}...")

        new_cases.append(entry)

    if new_cases:
        print(f"\n{SEP}")
        print(f"共新增 {len(new_cases)} 個案例，重建向量索引...")
        rebuild_embeddings()
        print(f"\n✅ 同步完成。")
    else:
        print("\n無新案例成功寫入（所有資料夾皆無 SQL 檔）。")


# ── 入口 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    sync(dry_run=dry_run)
