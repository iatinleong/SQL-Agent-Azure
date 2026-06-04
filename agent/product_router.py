"""商品路由：偵測需求中的模糊商品詞，強制進 QA 取得明確路由後才生成 SQL。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ProductRoute:
    label: str                                    # "台股ETF"
    table: str                                    # "M_AT_STOCK_TXN"
    filters: dict[str, str]                       # {"PROD_TYPE_CODE": "100", "PROD_STYPE_CODE": "019"}
    explicit_keywords: list[str] = field(default_factory=list)  # ["台股ETF", "國內ETF"]
    context_all: list[str] = field(default_factory=list)        # all must appear within ±20 chars of scan_keyword

    def to_dict(self) -> dict:
        return {"label": self.label, "table": self.table, "filters": self.filters}


@dataclass
class ProductTerm:
    term: str           # "ETF" — used as key in resolved_product_routes
    scan_keyword: str   # keyword to search in requirement text (word-boundary)
    routes: list[ProductRoute]
    clarification: str  # question text to show user


@dataclass
class RouteResolution:
    resolved: dict[str, ProductRoute]   # term → definite route (auto-resolved from text)
    ambiguous: list[ProductTerm]        # terms that need user clarification
    unmatched: list[str]               # appeared in text but not in catalog (for logging)


class ProductRouter:
    def __init__(self, catalog: list[ProductTerm]):
        self._catalog = catalog

    def get_term(self, name: str) -> ProductTerm | None:
        return next((t for t in self._catalog if t.term == name), None)

    def resolve(
        self,
        text: str,
        already_resolved: dict[str, str] | None = None,
    ) -> RouteResolution:
        """
        Scan text for known product terms.
        already_resolved: {term → chosen_label} from prior QA answers.
        Returns three-state result: resolved / ambiguous / unmatched.
        """
        already_resolved = already_resolved or {}
        resolved: dict[str, ProductRoute] = {}
        ambiguous: list[ProductTerm] = []

        for term in self._catalog:
            # Use ASCII-only word boundary so Chinese characters don't block matching
            # e.g. "ETF交易金額" should still detect "ETF"
            _scan_pat = r"(?<![A-Za-z\d])" + re.escape(term.scan_keyword) + r"(?![A-Za-z\d])"
            if not re.search(_scan_pat, text, re.IGNORECASE):
                continue

            if term.term in already_resolved:
                chosen = already_resolved[term.term]
                route = next((r for r in term.routes if r.label == chosen), None)
                if route:
                    resolved[term.term] = route
                continue

            # Check how many routes have explicit keyword matches in text
            matched: list[ProductRoute] = []
            for route in term.routes:
                for kw in route.explicit_keywords:
                    _kw_pat = r"(?<![A-Za-z\d])" + re.escape(kw) + r"(?![A-Za-z\d])"
                    if re.search(_kw_pat, text, re.IGNORECASE):
                        matched.append(route)
                        break

            if len(matched) == 1:
                resolved[term.term] = matched[0]
            elif len(matched) >= 2:
                both = next((r for r in term.routes if r.label == "兩者都要"), None)
                resolved[term.term] = both if both else matched[0]
            elif len(term.routes) == 1:
                resolved[term.term] = term.routes[0]
            else:
                # No explicit keyword matched — try context_all proximity check
                _m = re.search(_scan_pat, text, re.IGNORECASE)
                if _m:
                    _ws = max(0, _m.start() - 5)
                    _we = min(len(text), _m.end() + 20)
                    _win = text[_ws:_we]
                    _ctx_match = next(
                        (r for r in term.routes if r.context_all and
                         all(re.search(re.escape(q), _win, re.IGNORECASE) for q in r.context_all)),
                        None,
                    )
                    if _ctx_match:
                        resolved[term.term] = _ctx_match
                    else:
                        ambiguous.append(term)
                else:
                    ambiguous.append(term)

        return RouteResolution(resolved=resolved, ambiguous=ambiguous, unmatched=[])

    @staticmethod
    def format_constraints(resolved_plain: dict[str, dict]) -> str:
        """Format resolved routes as hard constraints injected into the generator prompt."""
        if not resolved_plain:
            return ""
        lines = ["【已確認商品路由，禁止自行修改以下設定】"]
        for term, r in resolved_plain.items():
            f = "、".join(f"{k}='{v}'" for k, v in r["filters"].items())
            lines.append(
                f"  {term} → {r['label']}　"
                f"資料表：{r['table']}　"
                f"篩選條件：{f}"
            )
        return "\n".join(lines)


# ── Catalog ──────────────────────────────────────────────────────────

PRODUCT_CATALOG: list[ProductTerm] = [
    ProductTerm(
        term="ETF",
        scan_keyword="ETF",
        routes=[
            ProductRoute(
                label="台股ETF",
                table="M_AT_STOCK_TXN",
                filters={"PROD_TYPE_CODE": "100", "PROD_STYPE_CODE": "019"},
                explicit_keywords=["台股ETF", "國內ETF"],
            ),
            ProductRoute(
                label="海外ETF",
                table="M_AT_STOCK_TXN",
                filters={"PROD_TYPE_CODE": "200", "PROD_MTYPE_CODE": "210", "PROD_STYPE_CODE": "019"},
                explicit_keywords=["海外ETF", "境外ETF", "複委託ETF"],
            ),
            ProductRoute(
                label="兩者都要",
                table="M_AT_STOCK_TXN",
                filters={},
                explicit_keywords=[],
            ),
        ],
        clarification="需求中提到 ETF，請問是台股ETF、海外ETF，還是兩者都要？",
    ),
    ProductTerm(
        term="結構型",
        scan_keyword="結構型",
        routes=[
            ProductRoute(
                label="境內結構型",
                table="M_AT_SN_TXN",
                filters={"PROD_MTYPE_CODE": "540"},
                explicit_keywords=["境內結構型", "國內結構型"],
            ),
            ProductRoute(
                label="境外結構型",
                table="M_AT_SN_TXN",
                filters={"PROD_MTYPE_CODE": "240"},
                explicit_keywords=["境外結構型", "海外結構型"],
            ),
            ProductRoute(
                label="兩者都要",
                table="M_AT_SN_TXN",
                filters={},
                explicit_keywords=["境內及境外結構型", "境內與境外結構型"],
                context_all=["境內", "境外"],  # catches "結構型（境內、境外）" pattern
            ),
        ],
        clarification="需求中提到結構型商品，請問是境內結構型、境外結構型，還是兩者都要？",
    ),
]

PRODUCT_ROUTER = ProductRouter(PRODUCT_CATALOG)
