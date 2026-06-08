"""BlockRegistry: split a WITH-query SQL into named blocks with char offsets."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SqlBlock:
    name: str               # "cte:<cte_name>" or "final_select"
    body_sql: str           # SQL text of this block (CTE body without parens; final SELECT verbatim)
    body_start: int         # inclusive char offset in original SQL
    body_end: int           # exclusive char offset — sql[body_start:body_end] == body_sql
    outputs: set[str] = field(default_factory=set)      # uppercased column aliases output
    real_tables: set[str] = field(default_factory=set)  # uppercased non-CTE table names used
    depends_on: set[str] = field(default_factory=set)   # uppercased CTE names referenced


# ── text helpers ──────────────────────────────────────────────────────

def _find_matching_close(sql: str, open_pos: int) -> int:
    """Return position of ')' matching the '(' at open_pos, or -1."""
    depth = 0
    for i in range(open_pos, len(sql)):
        ch = sql[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1


def _cte_body_offsets(sql: str, cte_name: str) -> tuple[int, int] | None:
    """Return (start, end) so that sql[start:end] is the CTE body (parens excluded)."""
    pat = re.compile(r'\b' + re.escape(cte_name) + r'\b\s+AS\s*\(', re.IGNORECASE)
    m = pat.search(sql)
    if not m:
        return None
    open_paren = m.end() - 1
    close_paren = _find_matching_close(sql, open_paren)
    if close_paren == -1:
        return None
    return open_paren + 1, close_paren


# ── AST helpers ───────────────────────────────────────────────────────

def _select_outputs(sql_body: str) -> set[str]:
    """Uppercased column aliases from the outermost SELECT of sql_body."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql_body, dialect="oracle")
    except Exception:
        return set()
    for sel in tree.find_all(exp.Select):
        p = getattr(sel, "parent", None)
        inner = False
        while p:
            if isinstance(p, (exp.CTE, exp.Subquery)):
                inner = True
                break
            p = getattr(p, "parent", None)
        if not inner:
            cols: set[str] = set()
            for expr in sel.expressions:
                if isinstance(expr, exp.Alias):
                    cols.add(expr.alias.upper())
                elif isinstance(expr, exp.Column):
                    cols.add((expr.name or "").upper())
            return cols
    return set()


def _real_tables(sql_body: str, cte_names: set[str]) -> set[str]:
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql_body, dialect="oracle")
    except Exception:
        return set()
    tables: set[str] = set()
    for t in tree.find_all(exp.Table):
        n = (t.name or "").upper()
        if n and n not in cte_names and n != "DUAL":
            tables.add(n)
    return tables


def _cte_refs(sql_body: str, cte_names: set[str]) -> set[str]:
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql_body, dialect="oracle")
    except Exception:
        return set()
    refs: set[str] = set()
    for t in tree.find_all(exp.Table):
        n = (t.name or "").upper()
        if n in cte_names:
            refs.add(n)
    return refs


# ── BlockRegistry ─────────────────────────────────────────────────────

class BlockRegistry:
    """Parse a WITH-query SQL into named blocks with char offsets and metadata."""

    def __init__(self, sql: str):
        self.original_sql = sql
        self._ordered: list[SqlBlock] = []
        self._by_name: dict[str, SqlBlock] = {}
        self._cte_names: set[str] = set()
        self._parse()

    @property
    def blocks(self) -> list[SqlBlock]:
        return list(self._ordered)

    def get(self, name: str) -> SqlBlock | None:
        return self._by_name.get(name)

    def _parse(self) -> None:
        try:
            import sqlglot
            from sqlglot import exp
            tree = sqlglot.parse_one(self.original_sql, dialect="oracle")
        except Exception:
            return

        cte_nodes = list(tree.find_all(exp.CTE))
        self._cte_names = {c.alias_or_name.upper() for c in cte_nodes}

        for cte in cte_nodes:
            raw_name = cte.alias_or_name
            offsets = _cte_body_offsets(self.original_sql, raw_name)
            if offsets is None:
                continue
            start, end = offsets
            body = self.original_sql[start:end]
            block = SqlBlock(
                name=f"cte:{raw_name}",
                body_sql=body,
                body_start=start,
                body_end=end,
                outputs=_select_outputs(body),
                real_tables=_real_tables(body, self._cte_names),
                depends_on=_cte_refs(body, self._cte_names),
            )
            self._ordered.append(block)
            self._by_name[block.name] = block

        # final_select: first SELECT keyword after last CTE's closing paren
        if cte_nodes:
            last_end = max(
                (_cte_body_offsets(self.original_sql, c.alias_or_name) or (0, 0))[1]
                for c in cte_nodes
            )
            after = self.original_sql[last_end + 1:]
            m = re.search(r'\bSELECT\b', after, re.IGNORECASE)
            final_start = last_end + 1 + m.start() if m else last_end + 1
        else:
            final_start = 0

        final_body = self.original_sql[final_start:].rstrip()
        if final_body:
            block = SqlBlock(
                name="final_select",
                body_sql=final_body,
                body_start=final_start,
                body_end=final_start + len(final_body),
                outputs=_select_outputs(final_body),
                real_tables=_real_tables(final_body, self._cte_names),
                depends_on=_cte_refs(final_body, self._cte_names),
            )
            self._ordered.append(block)
            self._by_name["final_select"] = block

        self._ordered.sort(key=lambda b: b.body_start)

    # ── error tagging ────────────────────────────────────────────────

    def tag_errors(self, errors: list[str]) -> list[str]:
        """Prepend [block=<name>] to each error that can be attributed to a block."""
        return [self._tag_one(e) for e in errors]

    def _tag_one(self, error: str) -> str:
        if error.startswith("[block="):  # 已由 validator 標記，避免雙重前綴
            return error
        name = self._identify_block(error)
        return f"[block={name}] {error}" if name else error

    def _identify_block(self, error: str) -> str | None:
        err_up = error.upper()

        # Data Redaction and mask misuse only arise in final SELECT
        if "[DATA REDACTION]" in err_up or "[語意錯誤]" in err_up:
            return "final_select"

        # Oracle quirk includes "(CTE: <name>)"
        m = re.search(r'\(CTE:\s*(\w+)\)', error, re.IGNORECASE)
        if m:
            cte_n = m.group(1).upper()
            for k in self._by_name:
                if k.replace("cte:", "").upper() == cte_n:
                    return k

        # Hallucination / schema prefix: match any real table name mentioned in error.
        # Use word-boundary regex to avoid M_AC_ACCOUNT matching M_AC_ACCOUNT_INFO.
        for block in self._ordered:
            for tname in block.real_tables:
                if re.search(r'\b' + re.escape(tname) + r'\b', err_up):
                    return block.name

        # Hallucination column fallback: "欄位不存在於查詢中任何表格：COL_NAME"
        # Table-name matching above won't find it — search block bodies for the identifier.
        if "[幻覺]" in error:
            m = re.search(r'[：:]\s*(\S+)\s*$', error.strip())
            if m:
                ident = m.group(1).upper()
                col = ident.split(".")[-1]  # strip table prefix if present
                for block in self._ordered:
                    if re.search(r"\b" + re.escape(col) + r"\b", block.body_sql, re.IGNORECASE):
                        return block.name

        return None

    # ── block replacement ────────────────────────────────────────────

    def replace_block(self, block_name: str, new_body: str) -> str:
        """Return full SQL with block body replaced by new_body."""
        block = self._by_name.get(block_name)
        if not block:
            return self.original_sql
        return (
            self.original_sql[:block.body_start]
            + new_body
            + self.original_sql[block.body_end:]
        )

    # ── rewrite context ──────────────────────────────────────────────

    def rewrite_context(self, block_name: str) -> dict:
        """Return context dict for BlockRewriter prompt construction."""
        block = self._by_name.get(block_name)
        if not block:
            return {}
        short = block_name.replace("cte:", "").upper()
        downstream = {b.name for b in self._ordered if short in b.depends_on}
        upstream_outputs: dict[str, set[str]] = {}
        for dep in block.depends_on:
            for k, v in self._by_name.items():
                if k.replace("cte:", "").upper() == dep.upper():
                    upstream_outputs[dep] = v.outputs
                    break
        return {
            "block_name": block_name,
            "body_sql": block.body_sql,
            "outputs": block.outputs,
            "real_tables": block.real_tables,
            "depends_on": block.depends_on,
            "downstream_blocks": downstream,
            "upstream_outputs": upstream_outputs,
        }


def apply_replacements(sql: str, replacements: list[tuple[int, int, str]]) -> str:
    """Apply (start, end, new_body) replacements from end to start (preserves earlier offsets)."""
    for start, end, new_body in sorted(replacements, key=lambda r: r[0], reverse=True):
        sql = sql[:start] + new_body + sql[end:]
    return sql
