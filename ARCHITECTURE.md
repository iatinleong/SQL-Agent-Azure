# SQL Agent 系統架構說明

## 系統目標

業務員用自然語言描述報表需求，系統自動找出最相關的歷史案例與適合的資料庫表格，輔助工程師快速生成 Oracle SQL。

---

## 整體流程圖

```
業務員輸入需求
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  安全檢查（guardrail.py）                            │
│  LLM 判斷輸入是否安全，不安全則拒絕並回傳原因        │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  Phase 1：向量檢索（用原始需求，不用改寫版）          │
│  用 BGE-M3 對需求文字做 cosine 相似度搜尋            │
│  → 從歷史案例中找出 Top-5 最相似案例                 │
│  → Top-5 案例 union tables = 候選池基礎              │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  Phase 2：報表需求確認（report_planner.py）           │
│  LLM 閱讀需求 + Top-5 案例 SQL，判斷：               │
│                                                     │
│  資訊充足 → status="confirm"                        │
│    呈現完整理解（粒度、時間範圍、篩選條件等）         │
│    使用者確認或指出修正 → 更新理解重新 confirm        │
│                                                     │
│  有不確定關鍵資訊 → status="ask"                    │
│    提一個最重要的問題（業務員語言）                   │
│    收到回答 → 加入 Q&A 歷史重新分析                  │
│                                                     │
│  雙方確認完畢 → 報表需求理解注入 Step A prompt       │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  實體擷取（entity_extractor.py）                     │
│  product_catalog  → 商品代碼 + 商品專屬表格           │
│  concept_routing  → 業務概念 → 相關表格               │
│  code_mapping     → 分公司名稱 → BRANCH_CODE 提示     │
│  → extra_tables（追加進候選池）                       │
│  → enriched_entities 文字（注入 Step A）              │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  Step A：草稿生成                                    │
│  注入：                                              │
│    [報表結構] 報表需求理解（Phase 2 確認結果）        │
│    [實體] enriched_entities（商品/概念/分公司提示）  │
│    [規則] business_skills（場景/關鍵字觸發）         │
│    [指標] metrics.json（全部注入，~800 tokens）      │
│    [JOIN]  relationships.json（依候選池過濾）        │
│    [Schema] 候選池欄位定義 + 代碼提示                │
│             （code_mapping.json，≤30 種代碼自動附加）│
│  LLM 從候選池自由選表、寫 SQL，並給出思路            │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  Step B：自我批判                                    │
│  注入：Step A SQL + 思路                             │
│       Top-5 案例原始 SQL（僅供參考，非標準答案）     │
│       所有涉及表格欄位定義（含代碼提示）             │
│  LLM 自我批判 → 輸出最終 SQL                        │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  Step C：語法驗證 + 自動修正（sql_validator.py）     │
│  sqlglot（解析層）→ Oracle dialect parse 驗證       │
│  sqlfluff（規則層）→ Oracle dialect lint 驗證        │
│                                                     │
│  通過 → 直接輸出                                    │
│  失敗 → LLM (gpt-5-mini) 修正 → 重新驗證           │
│         最多 3 輪，仍失敗則回傳最後修正版本          │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
              [輸出：最終 Oracle SQL]
                       │
              （使用者追問時）
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  追問改寫（refiner.py）                              │
│  意圖分類：ADD_TABLE / REMOVE_TABLE / MODIFY_SQL     │
│           / NEW_QUERY（重新走完整流程）              │
│  改寫 SQL，輸出改法說明 + 最終思路 + 最終 SQL        │
└─────────────────────────────────────────────────────┘
```

---

## 各階段詳細說明

### 離線前置作業（一次性）

```
schema.csv (73張表)
      │
      │  schema_summarizer.py
      │  LLM 為每張表產出 150-200 字業務說明
      ▼
table_summaries/*.txt (32張表的說明)

all_cases.json (92筆歷史案例)
      │
      │  summarizer.py
      │  LLM 閱讀 SQL + 需求，產出業務摘要
      ▼
case_summaries/*.txt (92筆摘要)
      │
      │  retriever.py
      │  BGE-M3 向量化，存入 npz cache
      ▼
all_cases_embeddings.npz
```

---

### 安全檢查（guardrail.py）

**輸入：** 需求文字

**處理：**
- LLM 判斷輸入是否為正常業務需求，輸出 `{safe: true/false, reason: "..."}`
- 不安全（惡意注入、無關要求）時直接拒絕，不進入後續流程

**tokens 追蹤：** `guardrail_in` / `guardrail_out`

---

### Phase 1：向量檢索

**輸入：** 需求文字（原始，不加工）

**處理：**
1. 用 BGE-M3 將需求文字 embed 成 1024 維向量
2. 與歷史案例的 embedding（預先計算，cached in npz）做 cosine 相似度
3. 回傳 Top-5 最相似案例
4. 取 Top-5 案例 SQL 中出現的表格聯集，作為 Step A 候選池基礎

**為什麼用 LLM 摘要而非直接向量化原始需求？**

業務員的需求往往很簡短（「查3月台股前50大」），而歷史案例的 SQL 包含完整業務邏輯。
先用 LLM 將 SQL 轉成業務語言摘要，再向量化，讓兩邊都用相同的「業務語言」做比對。

**摘要原則：**
- 以 SQL 為主要依據（完整邏輯），需求文字為輔（可能簡略）
- 不寫具體年份（避免 2023 vs 2025 造成向量偏移）
- 不寫具體 Top-N 數字（寫「前 N 大客戶」）
- 允許寫分公司名稱（不同分公司報表風格不同，是有效資訊）

**相關檔案：** `retriever.py`, `summarizer.py`, `case_summaries/`

---

### Phase 2：報表需求確認（report_planner.py）

在 Phase 2 之後、Step A 之前，透過多輪對話確認報表需求細節。

**輸入：** 需求文字 + Top-5 案例 SQL + 累積的 Q&A 歷史

**互動邏輯：**

| 狀態 | 條件 | 行為 |
|------|------|------|
| `ask` | 有真正無法判斷的關鍵資訊 | 提一個最重要的問題（業務員語言）；收到回答後重新分析 |
| `confirm` | 資訊已足夠 | 呈現完整理解，使用者確認或修正；修正後重新分析 |

**確認內容：** 每列粒度（帳戶/客戶/營業員/分公司/其他）、時間範圍、篩選條件、排列方式等任何影響 SQL 結構的關鍵資訊。

**原則：** 顯而易見的事情不問；每次只問一個問題；盡量 confirm，只有真的不確定才 ask。

**輸出：** `report_plan_text`（注入 Step A 的首個 prompt 區塊）+ Q&A 歷史記錄

**tokens 追蹤：** `plan_in` / `plan_out`（多輪時累加）

**相關檔案：** `agent/report_planner.py`

---

### 實體擷取（entity_extractor.py）

在 Phase 2 之後、Step A 生成之前執行，為後續步驟提供結構化提示。

**輸入：** 需求文字

**偵測邏輯：**

| 來源 | 偵測方式 | 產出 |
|------|---------|------|
| `product_catalog.json` | 別名字串比對（台股、基金、複委託…） | PROD_TYPE_CODE / PROD_MTYPE_CODE 提示 + 商品專屬表 |
| `concept_routing.json` | 關鍵字比對（月均交易量、配息、庫存…） | 業務概念相關表格 |
| 分公司偵測 Pass 1 | 後綴偵測（XX分公司 / XX分行 等），取 4→2 字最長匹配 | BRANCH_CODE 精確代碼 |
| 分公司偵測 Pass 2 | 直接比對 `code_mapping.json[BRANCH_MAPPING]` 的所有 key（處理無後綴寫法如「竹東」「北高雄」）| BRANCH_CODE 精確代碼 |

**輸出：**
- `extra_tables`：追加進候選池（與 Top-5 union tables 聯集）
- `enriched_entities`：注入 Step A 的【偵測到的商品/業務概念/分公司/WHERE 提示】文字區塊
- `codes`：dict，如 `{"BRANCH_CODE": "9624", "PROD_TYPE_CODE": "100"}`

**注意：** Phase 2 向量檢索仍使用**原始需求**（不用擴充版），因為 BGE-M3 對中文語義已足夠，加入代碼反而損害相似度計算。

**相關檔案：** `agent/entity_extractor.py`, `product_catalog.json`, `concept_routing.json`, `code_mapping.json`

---

### Step A：草稿生成

**輸入（依注入順序）：**

| 區塊 | 來源 | 觸發方式 |
|------|------|---------|
| 報表需求 | 使用者原始輸入 | 永遠 |
| 報表需求理解 | `report_planner.py`（Phase 2 確認結果） | 永遠（使用者已確認）|
| 偵測到的實體 | `entity_extractor.py` | 有偵測到才注入 |
| 業務技能規則 | `business_skills.json` | 場景名稱 match OR 關鍵字 match |
| 業務指標計算規則 | `metrics.json` | 永遠（全部，~800 tokens） |
| 表格關聯關係 | `relationships.json` | 依候選池過濾（兩端表格都在候選池才注入） |
| 候選池欄位定義 | `schema.csv` + `code_mapping.json` | 候選池內所有表格；≤30 種代碼的欄位自動附加 `[001=男, 002=女]` |

**處理：**
- LLM 從候選池自由選擇合適的表格，寫出 Oracle SQL
- 同時輸出設計思路（選表原因、JOIN 條件、時間篩選、聚合邏輯）

**tokens 追蹤：** `step_a_in` / `step_a_out`

**輸出格式：**
```
--- SQL ---
（完整 Oracle SQL）

--- 思路 ---
（設計說明）
```

**相關檔案：** `generator.py`, `business_skills.json`, `metrics.json`, `relationships.json`, `code_mapping.json`

---

### Step B：自我批判

**輸入：**
- Step A 產出的 SQL + 思路
- Top-5 案例原始 SQL（語義相似，僅供參考，非標準答案）
- 所有涉及表格的完整欄位定義（候選池 + 案例中出現的表格，含代碼提示）

**處理：**
1. LLM 比較自身思路與參考案例的差異
2. 分析 JOIN 條件、篩選邏輯是否一致或有可改進之處
3. 以需求與欄位定義為最終判斷依據（案例僅供參考）
4. 輸出分析說明 + 最終思路 + 最終版 SQL

**tokens 追蹤：** `step_b_in` / `step_b_out`

**輸出格式：**
```
--- 分析 ---
（比較與改進說明）

--- 最終思路 ---
（完整設計決策說明）

--- 最終 SQL ---
（最終版完整 Oracle SQL）
```

**相關檔案：** `generator.py`

---

### Step C：語法驗證 + 自動修正（sql_validator.py）

**輸入：** Step B 輸出的最終 SQL

**雙重驗證：**

| 工具 | 層次 | 用途 |
|------|------|------|
| `sqlglot` | 解析層 | Oracle dialect parse，捕捉無法解析的語法結構（WITH 多 SELECT、非法函數等） |
| `sqlfluff` | 規則層 | Oracle dialect lint，捕捉語意/結構規則違反（過濾純樣式規則如縮排、空格）|

**設計原則：**
- sqlglot 先跑；若有 parse 錯誤，不跑 sqlfluff（SQL 連解析都過不了，lint 結果無意義）
- sqlfluff 過濾 LT / AL08 / CP / RF / CV10 / CV11 等純樣式前綴，只回報結構性問題

**自動修正迴圈（最多 3 輪）：**
1. 驗證 → 有錯 → 把錯誤訊息 + SQL 傳給 LLM (gpt-5-mini) 修正
2. 再驗證 → 有錯 → 再修正
3. 第 3 輪後不再修正，保留最後修正結果

**tokens 追蹤：** `fix_in` / `fix_out`（多輪時累加）

**相關檔案：** `agent/sql_validator.py`

---

### 追問改寫（refiner.py）

使用者對已生成的 SQL 提出修改要求時觸發。

**意圖分類（gpt-5-mini）：**

| 意圖 | 說明 |
|------|------|
| `ADD_TABLE` | 需要引入目前 SQL 沒有的新表格（加客戶年齡、加配息資料等） |
| `REMOVE_TABLE` | 移除某個表格、欄位或 JOIN |
| `MODIFY_SQL` | 只修改 SQL 邏輯（WHERE、聚合、排序、時間範圍等），不新增表格 |
| `NEW_QUERY` | 完全不同的新需求，重新走完整 Phase1+2+StepA+StepB 流程 |

**處理：**
- `ADD_TABLE` 時，自動載入 `target_tables` 的欄位定義注入 prompt
- 保留對話歷史摘要（避免 context 爆炸）
- 輸出：改法說明 + 最終思路 + 改寫後完整 SQL

**tokens 追蹤：** `classify_in/out`（意圖分類）+ `refine_in/out`（改寫）

**相關檔案：** `agent/refiner.py`

---

### 費用追蹤

每次完整生成流程（guardrail + plan × N輪 + step_a + step_b + fix × N輪）的 token 用量與 USD 費用均被計算並寫入 Supabase `experiments` 表的 `cost_usd` 欄位。

**計算方式：**
```
cost = (tokens_in / 1,000,000) × price_in + (tokens_out / 1,000,000) × price_out
```

費率由 `config.py` 的 `MODEL_PRICING` 維護（每百萬 token，USD）。

---

## MDL（Metadata Layer）說明

系統維護六個 metadata 檔案，分屬不同抽象層次：

| 檔案 | 層次 | 用途 | 觸發方式 |
|------|------|------|---------|
| `product_catalog.json` | 實體層 | 商品名稱 → 代碼 + 專屬表格 | 別名字串比對 |
| `concept_routing.json` | 實體層 | 業務概念關鍵字 → 相關表格 | 關鍵字比對 |
| `code_mapping.json` | 實體層 + Schema 層 | 分公司名稱↔代碼對照（BRANCH_MAPPING/BRANCH_CODE）；欄位代碼說明（SEX_CODE 等） | 實體擷取 + 欄位代碼注入 |
| `relationships.json` | JOIN 層 | 表格間的 JOIN 條件與注意事項 | 候選池過濾（兩端表格都在才注入）|
| `metrics.json` | 計算層 | 指標計算公式、欄位語意區分 | 永遠全部注入（避免遺漏關鍵規則）|
| `business_skills.json` | 模式層 | 複雜 SQL 結構的完整範本（CTE 結構、PIVOT 格式等）| 場景名稱 OR 關鍵字觸發 |

### 各檔案職責邊界

**product_catalog.json（9 個商品）**
- 「台股」/「複委託」/「基金」等別名 → PROD_TYPE_CODE / PROD_MTYPE_CODE / TXN_TYPE_CODE
- 每個商品對應的交易明細表（M_AT_STOCK_TXN、M_AT_FUND_TXN 等）
- 用途：讓 entity_extractor 在 Phase 1 前追加正確的商品專屬表到候選池

**concept_routing.json（32 個概念）**
- 「市佔率」→ M_RF_MARKET_SHARE、「配息」→ M_AT_DIV、「營業員」→ M_PT_SALES 等
- 業務概念關鍵字與資料表的直接對應
- 用途：補足向量檢索可能遺漏的低頻業務表格

**code_mapping.json**

兩類資料合一：

1. **欄位代碼說明**（扁平結構 `{欄位名: {代碼: 說明}}`）
   - 來源：`DM_S_VIEW.M_RF_CODE` 參考表（`代碼.xlsx`），共 103 個欄位
   - 注入規則：≤30 種代碼、說明不全相同、定義文字中尚未出現代碼，才附加
   - 範例：`SEX_CODE` → `[001=男, 002=女]`

2. **分公司對照**
   - `BRANCH_CODE`：`{代碼: 分公司名稱}`（59 筆），用於 schema 欄位說明
   - `BRANCH_MAPPING`：`{分公司名稱: 代碼}`（59 筆），用於 entity_extractor 查詢
   - 來源：`分公司代碼.xlsx`（從 DB `M_AC_ACCOUNT` 直接撈取）

**relationships.json**
- 表格間的 JOIN 條件（ON 欄位、JOIN 型態、注意事項）
- **重要：** ACCT_NBR + PROD_TYPE_CODE 複合 JOIN key 防止多商品帳號資料爆炸
- 候選池過濾：只注入兩端表格都在候選池的 JOIN 規則（LLM 不會用到 schema 以外的表）

**metrics.json（12 條規則）**
- 非直覺指標的計算公式，例如：
  - `rev_amt_twd` vs `txn_amt_twd`：同在 M_AC_ACCOUNT_REVENUE，語意完全不同
  - `COUNT(DISTINCT party_id_mask)` 不重複客戶 vs `COUNT(DISTINCT acct_nbr)` 不重複帳戶
  - `M_AC_ACCOUNT.last_txn_date` 直接用，不須 JOIN 交易表取 MAX
  - 市佔率分母來自 M_RF_MARKET_SHARE，不是自行 SUM
- 永遠全部注入，因為遺漏規則會導致 LLM 靜默產出錯誤公式

**business_skills.json（12 條規則）**
- 複雜場景的完整 SQL 結構範本（如例行性報表需要哪些 CTE、離職營業員查詢的三來源聯集等）
- 場景觸發：trigger_scenes（已移除，現僅靠 trigger_keywords 觸發）
- 關鍵字觸發：trigger_keywords 比對需求文字（月均、離職、促轉、開戶等）

---

## 資料流與檔案結構

```
SQLagentnew/
│
├── all_cases.json              # 歷史案例（需求 + SQL）
├── all_cases_embeddings.npz    # 案例的 BGE-M3 向量 cache
├── schema.csv                  # 73 張表格定義（欄位名、中文名、說明）
├── used_tables.txt             # 被 SQL 實際使用的表名清單
│
├── product_catalog.json        # MDL：9 個商品的別名、代碼、對應表格
├── concept_routing.json        # MDL：32 個業務概念關鍵字 → 相關表格
├── code_mapping.json           # MDL：欄位代碼說明 + 分公司代碼對照（BRANCH_CODE/BRANCH_MAPPING）
├── relationships.json          # MDL：表格 JOIN 條件（兩端表格都在候選池才注入）
├── metrics.json                # MDL：12 條指標計算規則（永遠全部注入）
├── business_skills.json        # MDL：12 條複雜 SQL 結構規則（場景/關鍵字觸發）
│
├── case_summaries/             # LLM 業務摘要（Phase 1 索引源）
│   ├── 113.txt
│   ├── 116.txt
│   └── ...
│
├── table_summaries/            # 32 張表格的業務說明（Table Selection 用）
│   ├── M_AC_ACCOUNT.txt
│   ├── M_AT_STOCK_TXN.txt
│   └── ...
│
├── experiment/                 # 每次實驗的 stdout log + JSON 結果
│   └── YYYYMMDD_HHMMSS_*.{txt,json}
│
├── app.py                      # Streamlit 前端（對話介面 + Supabase 寫入）
│
└── agent/
    ├── config.py               # 模型、路徑、費率設定（GENERATION_MODEL=o3）

    ├── guardrail.py            # 輸入安全檢查（回傳 is_safe, reason, tokens）
    ├── pool_filter.py          # 0.4 gap 規則 + 候選池建立
    ├── summarizer.py           # Case 業務摘要（LLM）
    ├── retriever.py            # BGE-M3 向量檢索
    ├── schema_summarizer.py    # Table 業務說明（LLM）+ raw schema 載入
    ├── entity_extractor.py     # 實體擷取：商品/概念/分公司 → extra_tables + 提示
    ├── generator.py            # Step A + Step B + Step C SQL 生成（含費用計算）
    ├── sql_validator.py        # Step C 語法驗證：sqlglot + sqlfluff + LLM 自動修正
    ├── report_planner.py       # Phase 2 報表需求確認：ask/confirm 多輪對話
    ├── refiner.py              # 追問改寫：意圖分類 + SQL 改寫（含費用計算）
    ├── experiment_logger.py    # 實驗 log（stdout + JSON 存 experiment/）
    ├── supabase_logger.py      # Supabase 寫入（experiments 表）
    ├── eval_table_selection.py         # Table selection 準確度評測
    ├── eval_retrieval.py               # 向量檢索準確度評測（無 LLM）
    ├── eval_retrieval_table_overlap.py # 檢索案例 table 聯集對 ground truth 的覆蓋率評測
    ├── batch_test.py                   # 10 案例批次評測（Phase 1 + Phase 2）
    └── main.py                         # CLI 入口
```

---

## CLI 指令速查

```bash
# 單筆查詢（Phase 1 向量檢索）
python -m agent "幫我拉南港分公司台股交易量排名"

# 完整生成（實體擷取 + Phase 1 + Phase 2 + Step A + B + C）
python -m agent --generate "幫我拉南港分公司台股交易量排名"
python -m agent --generate "需求文字" --model=o3      # 指定模型（預設 o3）

# 批次評測（10 筆固定案例，Phase 1 向量檢索）
python -m agent --test

# 全庫向量檢索評測（92 筆，無 LLM 花費）
python -m agent --eval-retrieval

# 檢索案例 table 聯集覆蓋率評測（讀已有的 eval_retrieval JSON，無 LLM）
python -m agent --eval-retrieval-overlap
python -m agent --eval-retrieval-overlap experiment/20260523_011716_eval_retrieval.json  # 指定檔案

# Table selection 評測（LLM summary 模式）
python -m agent --eval-table-selection

# Table selection 評測（raw schema 模式，費用約 15x）
python -m agent --eval-table-selection --raw-schema

# 產出案例業務摘要（需先跑一次）
python -m agent --summarize
python -m agent --summarize 143          # 單筆
python -m agent --summarize --force      # 強制重跑

# 產出表格業務說明
python -m agent --schema-summarize
python -m agent --schema-summarize M_AC_ACCOUNT --force
```

---

## 評測指標說明

### 向量檢索（eval_retrieval）
- **命中率**：用自身需求查詢，自身出現在 Top-5 的比例
- **平均排名**：命中案例的平均 rank
- 目前成績：**92/92 (100%)，平均排名 1.0**

### 檢索 Table 覆蓋率（eval_retrieval_table_overlap）
- **Union Recall**：Top-5 中排除自身的其餘案例，其 SQL tables 聯集能覆蓋幾張 ground truth table
- 邏輯：自身在 Top-5 → 排除自身取剩餘 4 筆；自身不在 Top-5 → 取全部 5 筆
- 資料來源：讀取已有的 eval_retrieval JSON，不重跑向量檢索
- 目的：驗證「把相似案例給 LLM 看」是否能提供足夠的 table 線索

### Table Selection（eval_table_selection）
- **Precision**：LLM 選出的表中，有幾張真的用到
- **Recall**：SQL 實際用到的表中，LLM 選到了幾張
- **F1**：P 與 R 的調和平均
- **Exact match**：LLM 選出的集合與 ground truth 完全一致
- Ground truth 來源：解析每個 case 的 SQL，找出實際引用且在 table_summaries/ 中的表格名稱
