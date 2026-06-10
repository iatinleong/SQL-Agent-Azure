"""全域設定：路徑、模型名稱、OpenAI client。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import AzureOpenAI

# ── stdout UTF-8（Windows cp950 終端）────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ── 路徑 ─────────────────────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

ALL_CASES_PATH: Path = BASE_DIR / "all_cases.json"
TAXONOMY_PATH: Path = BASE_DIR / "taxonomy.json"

# ── 模型 ─────────────────────────────────────────────────────────────────────
CLASSIFICATION_MODEL: str = "gpt-5.2"
GENERATION_MODEL: str = "gpt-5.2"
PLAN_MODEL: str = "gpt-5.2"
VALIDATOR_MODEL: str = "gpt-5.2"
GUARDRAIL_MODEL: str = "gpt-5.2"
REFINER_MODEL: str = "gpt-5.2"
PROFILE_MODEL: str = "gpt-5.2"

# ── Reasoning effort（各角色依複雜度設定）────────────────────────────────────
GENERATION_REASONING_EFFORT: str = "high"       # SQL 生成：最複雜
PLAN_REASONING_EFFORT: str = "high"             # 報表規劃
VALIDATOR_REASONING_EFFORT: str = "medium"      # SQL 修正
REFINER_CLASSIFY_REASONING_EFFORT: str = "low"  # 追問意圖分類
REFINER_REFINE_REASONING_EFFORT: str = "medium" # SQL 改寫
CLASSIFICATION_REASONING_EFFORT: str = "low"    # 場景分類
GUARDRAIL_REASONING_EFFORT: str = "low"         # 安全過濾
PROFILE_REASONING_EFFORT: str = "low"           # 個人化摘要

# ── 模型費率（每百萬 token，USD）────────────────────────────────────────────
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-5-mini":   (0.25,   2.00),
    "gpt-5.4-mini": (0.75,   4.50),
    "gpt-5":        (1.25,  10.00),
    "gpt-5.1":      (1.25,  10.00),
    "gpt-5.2":      (1.75,  14.00),
    
    "gpt-5.3":      (1.75,  14.00),
    "gpt-5.4":      (2.50,  15.00),
    "gpt-5.5":      (2.50,  15.00),
    "gpt-4o":       (2.50,  10.00),
    "gpt-4.1":      (2.00,   8.00),
    "o3":           (2.00,   8.00),
    "o3-mini":      (1.10,   4.40),
    "o4-mini":      (1.10,   4.40),
    "gpt-5-pro":    (15.00, 120.00),
    "gpt-5.4-pro":  (30.00, 180.00),
}


def get_model_pricing(model: str) -> tuple[float, float]:
    """回傳 (input_per_M, output_per_M)，找不到時警告並回退 gpt-5-mini 費率。"""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    for key, price in MODEL_PRICING.items():
        if model.startswith(key) or key.startswith(model):
            return price
    print(f"  [警告] 未知模型費率：{model}，使用 gpt-5-mini 費率")
    return MODEL_PRICING["gpt-5-mini"]


# BGE-M3：本機用快照路徑（避免 SSL 請求），Cloud 自動下載
_bge_local = os.getenv(
    "BGE_MODEL_PATH",
    r"C:\Users\user\.cache\huggingface\hub"
    r"\models--BAAI--bge-m3\snapshots\5617a9f61b028005a4858fdac845db406aefb181",
)
BGE_MODEL_PATH: str = _bge_local if Path(_bge_local).exists() else "BAAI/bge-m3"

# ── Azure OpenAI client ───────────────────────────────────────────────────────
openai_client = AzureOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", "https://aoai-stw-jpe-prd-aisales-01-az.openai.azure.com/"),
    api_key=os.getenv("AZURE_OPENAI_KEY", ""),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
    http_client=httpx.Client(verify=False),
)
