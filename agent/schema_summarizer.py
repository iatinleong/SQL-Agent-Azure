"""批次作業：用 LLM 對 schema.csv 中每張表產出業務說明，存入 table_summaries/<表格名稱>.txt。"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from .config import BASE_DIR, CLASSIFICATION_MODEL, CLASSIFICATION_REASONING_EFFORT, openai_client

SCHEMA_CSV: Path = BASE_DIR / "schema.csv"
TABLE_SUMMARIES_DIR: Path = BASE_DIR / "table_summaries"

# ── Prompt ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是一位熟悉金融業務與資料庫設計的資深分析師。

【背景說明】
我們在建立一套智能 SQL 生成系統：當業務員描述報表需求時，系統需要自動判斷應該使用資料庫中哪些表格來撰寫 Oracle SQL。
你的任務是為每張表格產出一段業務說明，這些說明之後會一次性提供給 LLM，讓它根據業務需求選出適合的表格。
因此說明必須用業務員和分析師都能理解的語言，清楚傳達這張表的用途，讓 LLM 能正確判斷何時應該使用這張表。

【任務】
閱讀表格定義，寫一段 150–200 字的業務說明，涵蓋以下四個面向：
1. 這張表儲存什麼業務資料、屬於哪個業務域（帳戶、交易、庫存、客戶、人員、商品等）
2. 2–4 個最重要欄位的業務意義（用中文名稱，說明這個欄位代表什麼業務概念）
3. 這張表與其他表的關聯方式（例如：以帳號關聯客戶表、以員工代號關聯人員表）
4. 常見的查詢情境（例如：篩選有效帳戶、計算業績、追蹤庫存損益、找出特定商品持有者）

【格式要求】
- 用繁體中文，連貫的段落文字，不要條列式
- 每句話之間要有適當標點（逗號、句號），不可省略標點符號
- 150–200 字
- 不要出現英文欄位名稱
"""


def _build_user_prompt(table_name: str, table_cn: str, columns: list[dict]) -> str:
    col_lines = "\n".join(
        f"- {c['欄位名稱']}（{c['欄位中文名稱']}）：{c['欄位定義說明']}"
        for c in columns
    )
    return f"""\
表格名稱：{table_name}
表格中文名稱：{table_cn}
欄位清單：
{col_lines}

請說明這張表的業務用途。"""


# ── 讀取 schema.csv ───────────────────────────────────────────────────────────

def load_schema() -> dict[str, dict]:
    """
    回傳 {table_name: {"table_cn": str, "columns": [{"欄位名稱":..., "欄位中文名稱":..., "欄位定義說明":...}]}}
    """
    tables: dict[str, dict] = {}

    with open(SCHEMA_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = row["表格名稱"].strip()
            if name not in tables:
                tables[name] = {
                    "table_cn": row["表格中文名稱"].strip(),
                    "columns": [],
                }
            tables[name]["columns"].append({
                "欄位名稱": row["欄位名稱"].strip(),
                "欄位中文名稱": row["欄位中文名稱"].strip(),
                "欄位定義說明": row["欄位定義說明"].strip(),
            })

    return tables


# ── LLM 呼叫 ─────────────────────────────────────────────────────────────────

def summarize_table(table_name: str, table_cn: str, columns: list[dict]) -> str:
    response = openai_client.chat.completions.create(
        model=CLASSIFICATION_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(table_name, table_cn, columns)},
        ],
        max_completion_tokens=8000,
        reasoning_effort=CLASSIFICATION_REASONING_EFFORT,
    )
    return response.choices[0].message.content.strip()


# ── 批次產出 ──────────────────────────────────────────────────────────────────

def build_table_summaries(
    table_names: list[str] | None = None,
    force: bool = False,
) -> dict[str, str]:
    """
    Args:
        table_names: 指定只跑哪幾張表（None = 全部 73 張）
        force:       True 時覆蓋已存在的摘要
    """
    TABLE_SUMMARIES_DIR.mkdir(exist_ok=True)
    schema = load_schema()

    targets = table_names if table_names is not None else list(schema.keys())
    total = len(targets)
    results: dict[str, str] = {}

    for i, name in enumerate(targets, 1):
        if name not in schema:
            print(f"  [{i:3}/{total}] {name} 不在 schema 中，跳過")
            continue

        path = TABLE_SUMMARIES_DIR / f"{name}.txt"
        if path.exists() and not force:
            print(f"  [{i:3}/{total}] {name} 已存在，跳過")
            results[name] = path.read_text(encoding="utf-8")
            continue

        info = schema[name]
        print(f"  [{i:3}/{total}] {name}（{info['table_cn']}）  {len(info['columns'])} 欄...")
        summary = summarize_table(name, info["table_cn"], info["columns"])
        path.write_text(summary, encoding="utf-8")
        results[name] = summary
        print(f"           → {summary[:60]}...")

    print(f"\n完成。共處理 {total} 張表 → {TABLE_SUMMARIES_DIR}")
    return results


def load_raw_schema_as_text(
    table_names: list[str] | None = None,
) -> dict[str, str]:
    """將 schema.csv 原始定義格式化為文字，回傳 {table_name: text}。

    格式：表格中文名稱 + 每欄的英文名、中文名、定義說明。
    用於與 load_table_summaries() 做 A/B 比較——不經 LLM summarize，直接餵 raw schema。
    """
    schema = load_schema()
    if table_names is not None:
        schema = {k: v for k, v in schema.items() if k in table_names}

    result: dict[str, str] = {}
    for name, info in schema.items():
        col_lines = "\n".join(
            f"  - {c['欄位名稱']}（{c['欄位中文名稱']}）：{c['欄位定義說明']}"
            for c in info["columns"]
        )
        result[name] = f"表格中文名稱：{info['table_cn']}\n欄位定義：\n{col_lines}"
    return result


def load_table_summaries() -> dict[str, str]:
    """載入所有已產出的表格說明，回傳 {table_name: summary_text}。"""
    if not TABLE_SUMMARIES_DIR.exists():
        return {}
    return {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(TABLE_SUMMARIES_DIR.glob("*.txt"))
    }
