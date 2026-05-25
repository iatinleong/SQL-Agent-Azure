"""實體擷取：從報表需求文字偵測商品、業務概念、分公司，擴充候選池並生成 WHERE 提示。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import BASE_DIR

# ── 資料載入（module-level cache）────────────────────────────────────

def _load_json(filename: str) -> object:
    path = BASE_DIR / filename
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_product_catalog: list[dict] | None = None
_concept_routing: dict | None = None
_branch_mapping: dict[str, str] | None = None


def _get_product_catalog() -> list[dict]:
    global _product_catalog
    if _product_catalog is None:
        _product_catalog = _load_json("product_catalog.json") or []
    return _product_catalog


def _get_concept_routing() -> dict:
    global _concept_routing
    if _concept_routing is None:
        _concept_routing = _load_json("concept_routing.json") or {}
    return _concept_routing


def _get_branch_mapping() -> dict[str, str]:
    """從 code_mapping.json 的 BRANCH_MAPPING 取得 {name: code}。
    JSON 儲存格式為 {code: name}，此處自動反轉為 {name: code} 供查詢用。
    """
    global _branch_mapping
    if _branch_mapping is None:
        loaded = _load_json("code_mapping.json")
        if isinstance(loaded, dict):
            raw = loaded.get("BRANCH_MAPPING") or {}
            # raw 是 {code: name}，反轉為 {name: code}；跳過 Excel 標題列
            _branch_mapping = {
                v: k for k, v in raw.items() if k != "BRANCH_CODE"
            }
        else:
            _branch_mapping = {}
    return _branch_mapping


# ── 分公司偵測 ─────────────────────────────────────────────────────

_CJK_RE = re.compile(r"[一-鿿]")
_BRANCH_SUFFIXES = ("分公司", "分行", "辦事處", "分部")


def _detect_branches(query: str, branch_mapping: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """擷取 '竹北分公司' 這類名稱，回傳 (full_name, stem) 列表。
    優先嘗試 4→2 字的地名，以 branch_mapping 命中最長的為準；無 mapping 時取 2 字。
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for suffix in _BRANCH_SUFFIXES:
        start = 0
        while True:
            idx = query.find(suffix, start)
            if idx < 0:
                break
            stem = None
            for length in range(4, 1, -1):
                if idx >= length:
                    candidate = query[idx - length : idx]
                    if all(_CJK_RE.match(c) for c in candidate):
                        if branch_mapping and candidate in branch_mapping:
                            stem = candidate
                            break
            if stem is None and idx >= 2:
                candidate = query[idx - 2 : idx]
                if all(_CJK_RE.match(c) for c in candidate):
                    stem = candidate
            if stem:
                branch = stem + suffix
                if branch not in seen:
                    seen.add(branch)
                    results.append((branch, stem))
            start = idx + 1
    return results


# ── 主函式 ─────────────────────────────────────────────────────────

@dataclass
class ExtractionResult:
    detected_products: list[str] = field(default_factory=list)
    detected_concepts: list[str] = field(default_factory=list)
    detected_branches: list[str] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)  # 追加進候選池
    codes: dict[str, str] = field(default_factory=dict)    # WHERE 提示
    enriched_entities: str = ""                             # 注入 Step A 的文字區塊


def extract_entities(query: str) -> ExtractionResult:
    result = ExtractionResult()
    extra_tables: set[str] = set()

    catalog = _get_product_catalog()
    concept_routing = _get_concept_routing()
    branch_mapping = _get_branch_mapping()

    # ── 1. 商品偵測 ──────────────────────────────────────────────
    product_lines: list[str] = []
    for entry in catalog:
        name = entry.get("name", "")
        aliases: list[str] = entry.get("aliases", [])
        codes: dict = entry.get("codes", {})
        tables: list[str] = entry.get("tables", [])

        matched = next((a for a in aliases if a in query), None)
        if matched is None:
            continue

        result.detected_products.append(name)
        extra_tables.update(tables)

        code_hints = ", ".join(f"{k}='{v}'" for k, v in codes.items())
        table_hint = ", ".join(tables)
        product_lines.append(
            f"  商品：{name}（由「{matched}」觸發）→ {code_hints}；可用表格：{table_hint}"
        )
        # 最高優先的 code 注入（若尚未設定同名 key）
        for k, v in codes.items():
            result.codes.setdefault(k, v)

    # ── 2. 業務概念偵測 ──────────────────────────────────────────
    concept_lines: list[str] = []
    for keyword, info in concept_routing.items():
        if keyword.lower() not in query.lower():
            continue
        tables: list[str] = info.get("tables", [])
        desc: str = info.get("desc", keyword)
        result.detected_concepts.append(keyword)
        extra_tables.update(tables)
        concept_lines.append(f"  概念「{keyword}」→ {desc}；可用表格：{', '.join(tables)}")

    # ── 3. 分公司偵測 ────────────────────────────────────────────
    # Pass 1：後綴偵測（XX分公司 / XX分行 等）
    branch_lines: list[str] = []
    seen_stems: set[str] = set()
    for branch_name, stem in _detect_branches(query, branch_mapping):
        result.detected_branches.append(branch_name)
        seen_stems.add(stem)
        seen_stems.add(branch_name)  # 避免 Pass 2 重複偵測同一個全名
        code = branch_mapping.get(branch_name) or branch_mapping.get(stem)
        if code:
            result.codes["BRANCH_CODE"] = code
            branch_lines.append(f"  分公司：{branch_name} → BRANCH_CODE='{code}'")
        else:
            result.codes.setdefault("BRANCH_NAME", branch_name)
            branch_lines.append(f"  分公司：{branch_name} → BRANCH_NAME='{branch_name}'（可直接用文字比對）")

    # Pass 2：比對全名或去後綴的 stem（處理「竹東」「北高雄」等無後綴寫法）
    for key, code in branch_mapping.items():
        if key in seen_stems:
            continue  # 已由 Pass 1 處理
        # 先試全名
        if key in query:
            result.detected_branches.append(key)
            seen_stems.add(key)
            result.codes["BRANCH_CODE"] = code
            branch_lines.append(f"  分公司：{key} → BRANCH_CODE='{code}'")
            continue
        # 再試去掉後綴的 stem（如「竹東分公司」→「竹東」）
        stem = key
        for suffix in _BRANCH_SUFFIXES:
            if key.endswith(suffix):
                stem = key[: -len(suffix)]
                break
        if stem != key and stem not in seen_stems and stem in query:
            result.detected_branches.append(key)
            seen_stems.add(stem)
            seen_stems.add(key)
            result.codes["BRANCH_CODE"] = code
            branch_lines.append(f"  分公司：{key}（由「{stem}」觸發）→ BRANCH_CODE='{code}'")

    # ── 4. 組合 enriched_entities ───────────────────────────────
    sections: list[str] = []
    if product_lines:
        sections.append("【偵測到的商品】\n" + "\n".join(product_lines))
    if concept_lines:
        sections.append("【偵測到的業務概念】\n" + "\n".join(concept_lines))
    if branch_lines:
        sections.append("【偵測到的分公司】\n" + "\n".join(branch_lines))
    if result.codes:
        code_strs = [f"{k}='{v}'" for k, v in result.codes.items()]
        sections.append("【建議 WHERE 條件提示】\n  " + "，".join(code_strs))

    result.enriched_entities = "\n\n".join(sections)
    result.extra_tables = sorted(extra_tables)
    return result
