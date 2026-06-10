"""Phase 1 核心：呼叫 LLM 將報表需求分類到業務場景。"""

from __future__ import annotations

import json
from typing import Union

from .config import CLASSIFICATION_MODEL, CLASSIFICATION_REASONING_EFFORT, openai_client
from .models import ClassificationResult
from .reader import normalize_requirement
from .taxonomy import build_taxonomy_section, get_category_names, load_taxonomy


def classify_intent(requirement: Union[str, dict]) -> tuple[ClassificationResult, dict]:
    """
    Phase 1 主函式：將報表需求分類到業務場景。

    Returns:
        (ClassificationResult, tokens_dict) — tokens_dict keys: classify_in, classify_out
    """
    taxonomy = load_taxonomy()
    category_names = get_category_names(taxonomy)
    req_text = normalize_requirement(requirement)

    system_prompt = _build_system_prompt(taxonomy, category_names)
    user_prompt = f"請分類以下報表需求：\n\n{req_text}"

    response = openai_client.beta.chat.completions.parse(
        model=CLASSIFICATION_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format=ClassificationResult,
        reasoning_effort=CLASSIFICATION_REASONING_EFFORT,
    )
    tokens = {
        "classify_in": response.usage.prompt_tokens,
        "classify_out": response.usage.completion_tokens,
    }
    return response.choices[0].message.parsed, tokens


def _build_system_prompt(taxonomy: list[dict], category_names: list[str]) -> str:
    return f"""你是一個金融報表需求分析專家，負責將報表需求精準分類到對應的業務場景。

===== 業務場景定義 =====
{build_taxonomy_section(taxonomy)}
===== END =====

分類規則：
1. 主要場景：選擇置信度最高的場景名稱（必須完全符合上列名稱之一）。
2. 次要場景：選擇置信度第二高的場景名稱（必須完全符合上列名稱之一，且不能與主要場景相同）。
   被必要不一定要填，除非報表需求的業務場景很難界定。
3. 分類理由：2~3 句話，說明主要場景的選擇依據。
4. 各標籤置信度：對全部 {len(category_names)} 個場景各給 0.0~1.0，總和 = 1.0。

可選場景（只能從這裡選）：
{json.dumps(category_names, ensure_ascii=False)}"""
