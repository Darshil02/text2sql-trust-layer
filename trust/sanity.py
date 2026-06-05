"""F2 sanity checks: join explosion detection, missing filters, magnitude anomalies, and null ratios."""

from __future__ import annotations

from typing import Any

import re

import sqlglot
import sqlglot.expressions as exp

_MANY_THRESHOLD = 1.01   # COUNT(*)/COUNT(DISTINCT key) above this → many-side
_REEXEC_TOLERANCE = 0.005  # 0.5% relative difference triggers a flag


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def get_join_cardinality(con, table_a: str, key_a: str, table_b: str, key_b: str) -> dict[str, Any]:
    """
    Probe each side of a join key pair for fan-out potential.

    Computes COUNT(*) / COUNT(DISTINCT key) on each table. A ratio > 1.0 means
    that table has multiple rows per key value — the "many" side of a 1:N join.
    Schema-agnostic: operates entirely on table and column names passed in.
    """
    def _ratio(table: str, key: str) -> float:
        row = con.execute(
            f'SELECT COUNT(*)::FLOAT / NULLIF(COUNT(DISTINCT "{key}"), 0) FROM "{table}"'
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else 1.0

    ratio_a = _ratio(table_a, key_a)
    ratio_b = _ratio(table_b, key_b)
    return {
        "table_a": table_a, "key_a": key_a,
        "ratio_a": round(ratio_a, 4), "a_is_many": ratio_a > _MANY_THRESHOLD,
        "table_b": table_b, "key_b": key_b,
        "ratio_b": round(ratio_b, 4), "b_is_many": ratio_b > _MANY_THRESHOLD,
    }


# ---------------------------------------------------------------------------
# Internal AST helpers
# ---------------------------------------------------------------------------

def _alias_map(select: exp.Select) -> dict[str, str]:
    """Return {alias_or_name -> real_table_name} for top-level FROM/JOIN tables only."""
    result: dict[str, str] = {}
    from_clause = select.args.get("from_")
    if from_clause:
        tbl = from_clause.this
        if isinstance(tbl, exp.Table):
            result[tbl.alias or tbl.name] = tbl.name
    for join in (select.args.get("joins") or []):
        tbl = join.this
        if isinstance(tbl, exp.Table):
            result[tbl.alias or tbl.name] = tbl.name
    return result


def _join_key_pairs(select: exp.Select) -> list[tuple[str, str, str, str]]:
    """
    Extract (alias_left, col_left, alias_right, col_right) from each JOIN ON
    equality predicate in the query.
    """
    pairs: list[tuple[str, str, str, str]] = []
    for join in (select.args.get("joins") or []):
        on = join.args.get("on")
        if not on:
            continue
        for eq in on.find_all(exp.EQ):
            left, right = eq.left, eq.right
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                pairs.append((left.table or "", left.name, right.table or "", right.name))
    return pairs


def _agg_columns(select: exp.Select) -> list[tuple[str, str, str]]:
    """
    Return (func_name, table_alias, col_name) for each SUM, AVG, or
    non-DISTINCT COUNT in the query.
    """
    found: list[tuple[str, str, str]] = []
    for agg_cls, label in [(exp.Sum, "SUM"), (exp.Avg, "AVG")]:
        for node in select.find_all(agg_cls):
            for col in node.find_all(exp.Column):
                found.append((label, col.table or "", col.name))
    for node in select.find_all(exp.Count):
        if not node.args.get("distinct"):
            for col in node.find_all(exp.Column):
                found.append(("COUNT", col.table or "", col.name))
    return found


def _agg_col_index(select: exp.Select) -> int:
    """Index of the first SUM/AVG expression in the SELECT list (fallback: last column)."""
    for i, expr in enumerate(select.expressions):
        if expr.find(exp.Sum) or expr.find(exp.Avg):
            return i
    return max(0, len(select.expressions) - 1)


# ---------------------------------------------------------------------------
# Detector A — AST + cardinality metadata, no re-execution
# ---------------------------------------------------------------------------

def detect_fanout_ast(sql: str, con) -> dict[str, Any]:
    """
    Detect fan-out SUM/AVG using the query AST structure and live join cardinality
    probes against the database. Never re-executes the original query.

    Steps:
      1. Parse SQL to extract alias map, join key pairs, and aggregated columns.
      2. For each join pair, call get_join_cardinality to identify many-side tables.
      3. Flag if any SUM/AVG column belongs to a many-side table.

    Returns: {flagged: bool, reason: str, method: "ast"}
    """
    try:
        select = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception as exc:
        return {"flagged": False, "reason": f"parse error: {exc}", "method": "ast"}

    if not isinstance(select, exp.Select):
        return {"flagged": False, "reason": "not a SELECT statement", "method": "ast"}

    alias_to_table = _alias_map(select)
    join_pairs = _join_key_pairs(select)
    agg_cols = _agg_columns(select)

    if not join_pairs or not agg_cols:
        return {"flagged": False, "reason": "no joins or aggregates found", "method": "ast"}

    # Determine which aliases sit on the many-side of at least one join
    many_aliases: set[str] = set()
    for al_a, col_a, al_b, col_b in join_pairs:
        tbl_a = alias_to_table.get(al_a)
        tbl_b = alias_to_table.get(al_b)
        if not tbl_a or not tbl_b:
            continue
        try:
            card = get_join_cardinality(con, tbl_a, col_a, tbl_b, col_b)
        except Exception:
            continue
        if card["a_is_many"]:
            many_aliases.add(al_a)
        if card["b_is_many"]:
            many_aliases.add(al_b)

    # Flag if any SUM/AVG column's table is a many-side table
    for func, t_alias, col in agg_cols:
        if func in ("SUM", "AVG") and t_alias in many_aliases:
            real_table = alias_to_table.get(t_alias, t_alias)
            return {
                "flagged": True,
                "reason": (
                    f"{func}({t_alias}.{col}) aggregates a column from '{real_table}', "
                    f"which is on the many-side of a join — "
                    f"the aggregate will be inflated by the join fan-out"
                ),
                "method": "ast",
            }

    return {
        "flagged": False,
        "reason": "no SUM/AVG on a many-side table detected",
        "method": "ast",
    }


# ---------------------------------------------------------------------------
# Detector B — re-execution with grain-corrected baseline
# ---------------------------------------------------------------------------

def detect_fanout_reexec(sql: str, con) -> dict[str, Any]:
    """
    Detect fan-out by comparing the original query's aggregate total to a
    grain-corrected baseline derived from the many-side table alone.

    The baseline is: SUM(agg_col) pre-aggregated to join-key grain on the
    many-side table. This equals the true total without fan-out inflation.
    If the original total exceeds this baseline by more than _REEXEC_TOLERANCE,
    fan-out inflation is confirmed.

    Wraps all execution in try/except — never crashes, returns flagged=False
    with reason "could not verify" on any failure.

    Returns: {flagged, reason, method="reexec", original_value, corrected_value, pct_difference}
    """
    try:
        select = sqlglot.parse_one(sql, dialect="duckdb")
        if not isinstance(select, exp.Select):
            return {"flagged": False, "reason": "not a SELECT statement", "method": "reexec"}

        alias_to_table = _alias_map(select)
        join_pairs = _join_key_pairs(select)
        agg_cols = _agg_columns(select)

        if not join_pairs or not agg_cols:
            return {"flagged": False, "reason": "no joins or aggregates to check", "method": "reexec"}

        # Find SUM/AVG columns that land on a many-side table, and record the join key
        candidates: list[tuple[str, str, str, str]] = []  # (alias, real_table, join_key_col, agg_col)
        for al_a, col_a, al_b, col_b in join_pairs:
            tbl_a = alias_to_table.get(al_a)
            tbl_b = alias_to_table.get(al_b)
            if not tbl_a or not tbl_b:
                continue
            try:
                card = get_join_cardinality(con, tbl_a, col_a, tbl_b, col_b)
            except Exception:
                continue
            for func, t_alias, agg_col in agg_cols:
                if func not in ("SUM", "AVG"):
                    continue
                if card["b_is_many"] and t_alias == al_b:
                    candidates.append((al_b, tbl_b, col_b, agg_col))
                if card["a_is_many"] and t_alias == al_a:
                    candidates.append((al_a, tbl_a, col_a, agg_col))

        if not candidates:
            return {
                "flagged": False,
                "reason": "no SUM/AVG on a many-side table found — could not verify",
                "method": "reexec",
            }

        _, many_table, join_key, agg_col = candidates[0]

        # Run the original query and sum all values at the aggregate column position
        orig_rows = con.execute(sql).fetchall()
        if not orig_rows:
            return {"flagged": False, "reason": "original query returned no rows", "method": "reexec"}

        agg_idx = _agg_col_index(select)
        original_total = sum(
            float(row[agg_idx]) for row in orig_rows if row[agg_idx] is not None
        )

        # Grain-corrected baseline: pre-aggregate many-side table to join-key grain, then SUM.
        # SUM(SUM(col) GROUP BY join_key) == SUM(col) but makes the grain explicit.
        clean_sql = (
            f'SELECT SUM(_g."{agg_col}") FROM '
            f'(SELECT "{join_key}", SUM("{agg_col}") AS "{agg_col}" '
            f'FROM "{many_table}" GROUP BY "{join_key}") AS _g'
        )
        clean_row = con.execute(clean_sql).fetchone()
        corrected_value = float(clean_row[0]) if clean_row and clean_row[0] is not None else 0.0

        if corrected_value == 0.0:
            return {
                "flagged": False,
                "reason": "corrected baseline is zero — could not verify",
                "method": "reexec",
            }

        pct_diff = (original_total - corrected_value) / corrected_value
        flagged = pct_diff > _REEXEC_TOLERANCE

        return {
            "flagged": flagged,
            "reason": (
                f"original total {original_total:,.2f} exceeds grain-corrected baseline "
                f"{corrected_value:,.2f} by {pct_diff:.1%} — fan-out inflation confirmed"
                if flagged else
                f"original total {original_total:,.2f} is within {_REEXEC_TOLERANCE:.1%} "
                f"of baseline {corrected_value:,.2f} — no fan-out detected"
            ),
            "method": "reexec",
            "original_value": round(original_total, 2),
            "corrected_value": round(corrected_value, 2),
            "pct_difference": round(pct_diff, 4),
        }

    except Exception as exc:
        return {
            "flagged": False,
            "reason": f"could not verify: {exc}",
            "method": "reexec",
        }


# ---------------------------------------------------------------------------
# Wrong-grain shared helper
# ---------------------------------------------------------------------------

def detect_child_grain(con, table: str, column: str) -> tuple[str, str] | None:
    """
    Heuristically determine whether `table` is a child-grain table relative to
    some parent table, excluding the aggregated `column` from consideration.

    Strategy:
      For each column C in `table` (other than `column`):
        1. Compute COUNT(*) / COUNT(DISTINCT C) in `table`.
           If ratio > _MANY_THRESHOLD, C is a FK candidate (non-unique here).
        2. Check every other table T for a column also named C.
           If C is unique in T (ratio ≤ 1.001), T is the parent and C is the key.

    Among all valid (parent, key) candidates, the one with the LOWEST fan-out
    ratio is returned — the most direct parent relationship.

    Returns (parent_table, key_column) or None. Schema-agnostic: all names
    are discovered dynamically from database metadata.
    """
    own_cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
    all_tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    other_tables = [t for t in all_tables if t != table]

    # Precompute column lists for other tables (avoids repeated PRAGMA calls)
    other_schemas: dict[str, list[str]] = {
        t: [r[1] for r in con.execute(f'PRAGMA table_info("{t}")').fetchall()]
        for t in other_tables
    }

    best: tuple[str, str] | None = None
    best_ratio = float("inf")

    for col in own_cols:
        if col == column:
            continue

        row = con.execute(
            f'SELECT COUNT(*)::FLOAT / NULLIF(COUNT(DISTINCT "{col}"), 0) FROM "{table}"'
        ).fetchone()
        ratio = float(row[0]) if row and row[0] is not None else 1.0

        # Only FK candidates with a lower ratio than the current best
        if ratio <= _MANY_THRESHOLD or ratio >= best_ratio:
            continue

        # Look for a table where this column is a unique key (PK-like)
        for other, other_cols in other_schemas.items():
            if col not in other_cols:
                continue
            row2 = con.execute(
                f'SELECT COUNT(*)::FLOAT / NULLIF(COUNT(DISTINCT "{col}"), 0) FROM "{other}"'
            ).fetchone()
            ratio2 = float(row2[0]) if row2 and row2[0] is not None else 2.0
            if ratio2 <= 1.001:
                best_ratio = ratio
                best = (other, col)
                break  # found a parent for this col; keep searching other cols for a better one

    return best


# ---------------------------------------------------------------------------
# Wrong-grain Detector A — AST + child-grain metadata, no re-execution
# ---------------------------------------------------------------------------

def detect_wrong_grain_ast(sql: str, con) -> dict[str, Any]:
    """
    Detect AVG computed at a finer grain than the question's entity grain.

    Checks whether AVG is applied directly to a column in a child-grain table
    (a table with a FK-like column pointing to a coarser parent table) without
    a GROUP BY that collapses to the parent grain first.

    Only inspects the top-level SELECT — AVG inside a subquery or pre-aggregated
    CTE is intentionally not flagged (see Known Limitations in docs/detectors.md).

    Returns: {flagged, reason, method="ast"}
    """
    try:
        select = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception as exc:
        return {"flagged": False, "reason": f"parse error: {exc}", "method": "ast"}

    if not isinstance(select, exp.Select):
        return {"flagged": False, "reason": "not a SELECT statement", "method": "ast"}

    from_clause = select.args.get("from_")
    if not from_clause:
        return {"flagged": False, "reason": "no FROM clause", "method": "ast"}

    # Exit early if FROM is a subquery — grain is handled inside it
    from_table = from_clause.this
    if not isinstance(from_table, exp.Table):
        return {"flagged": False, "reason": "FROM is a subquery — grain is pre-handled", "method": "ast"}

    base_table_name = from_table.name
    alias_to_table = _alias_map(select)

    agg_cols = _agg_columns(select)
    avg_cols = [(func, t_alias, col) for func, t_alias, col in agg_cols if func == "AVG"]

    if not avg_cols:
        return {"flagged": False, "reason": "no AVG aggregates found", "method": "ast"}

    # Collect GROUP BY column names for parent-key check
    group_by = select.args.get("group")
    group_col_names: set[str] = set()
    if group_by:
        group_col_names = {c.name for c in group_by.find_all(exp.Column)}

    for _, t_alias, col in avg_cols:
        agg_table = alias_to_table.get(t_alias, t_alias) if t_alias else base_table_name

        try:
            parent_info = detect_child_grain(con, agg_table, col)
        except Exception:
            continue

        if parent_info is None:
            continue

        parent_table, parent_key = parent_info

        # Not wrong-grain if GROUP BY pre-aggregates to the parent key
        if parent_key in group_col_names:
            continue

        return {
            "flagged": True,
            "reason": (
                f"AVG({col}) is computed at '{agg_table}' row grain "
                f"(detected: {agg_table}.{parent_key} is a FK to '{parent_table}' "
                f"with fan-out ratio > {_MANY_THRESHOLD}). "
                f"Result is row-weighted, not {parent_table}-weighted. "
                f"Pre-aggregate to '{parent_table}' grain before applying AVG."
            ),
            "method": "ast",
        }

    return {"flagged": False, "reason": "no wrong-grain AVG detected", "method": "ast"}


# ---------------------------------------------------------------------------
# Wrong-grain Detector B — re-execution with grain-corrected AVG
# ---------------------------------------------------------------------------

def detect_wrong_grain_reexec(sql: str, con) -> dict[str, Any]:
    """
    Detect wrong-grain AVG by comparing the raw result to a grain-corrected variant.

    Corrected variant: pre-aggregate the child table to the parent grain first
    (SUM per parent key in a subquery), then AVG the per-parent totals.
    If raw AVG and corrected AVG differ beyond _REEXEC_TOLERANCE, the original
    is at the wrong grain.

    Wraps all logic in try/except — never crashes, returns flagged=False with
    reason "could not verify" on any failure.

    Returns: {flagged, reason, method="reexec", original_value, corrected_value, pct_difference}
    """
    try:
        select = sqlglot.parse_one(sql, dialect="duckdb")
        if not isinstance(select, exp.Select):
            return {"flagged": False, "reason": "not a SELECT statement", "method": "reexec"}

        from_clause = select.args.get("from_")
        if not from_clause or not isinstance(from_clause.this, exp.Table):
            return {"flagged": False, "reason": "FROM is a subquery — grain is pre-handled", "method": "reexec"}

        base_table_name = from_clause.this.name
        alias_to_table = _alias_map(select)

        agg_cols = _agg_columns(select)
        avg_cols = [(func, t_alias, col) for func, t_alias, col in agg_cols if func == "AVG"]

        if not avg_cols:
            return {"flagged": False, "reason": "no AVG aggregates found", "method": "reexec"}

        _, t_alias, agg_col = avg_cols[0]
        agg_table = alias_to_table.get(t_alias, t_alias) if t_alias else base_table_name

        parent_info = detect_child_grain(con, agg_table, agg_col)
        if parent_info is None:
            return {
                "flagged": False,
                "reason": "no child-grain relationship detected — could not verify",
                "method": "reexec",
            }

        parent_table, parent_key = parent_info

        # Run original query (expects a scalar AVG result)
        orig_row = con.execute(sql).fetchone()
        if orig_row is None or orig_row[0] is None:
            return {"flagged": False, "reason": "original query returned no result", "method": "reexec"}
        original_value = float(orig_row[0])

        # Grain-corrected variant: SUM per parent key first, then AVG those sums
        corrected_sql = (
            f'SELECT AVG(_g."{agg_col}") FROM '
            f'(SELECT "{parent_key}", SUM("{agg_col}") AS "{agg_col}" '
            f'FROM "{agg_table}" GROUP BY "{parent_key}") AS _g'
        )
        corr_row = con.execute(corrected_sql).fetchone()
        if corr_row is None or corr_row[0] is None:
            return {"flagged": False, "reason": "could not compute corrected variant", "method": "reexec"}
        corrected_value = float(corr_row[0])

        if corrected_value == 0:
            return {"flagged": False, "reason": "corrected value is zero — could not verify", "method": "reexec"}

        pct_diff = (original_value - corrected_value) / abs(corrected_value)
        flagged = abs(pct_diff) > _REEXEC_TOLERANCE

        return {
            "flagged": flagged,
            "reason": (
                f"raw AVG({agg_col}) = {original_value:.4f} vs "
                f"grain-corrected AVG(SUM per {parent_table}.{parent_key}) = {corrected_value:.4f} "
                f"— differ by {pct_diff:.1%}, wrong grain confirmed"
                if flagged else
                f"raw AVG({agg_col}) = {original_value:.4f} matches "
                f"corrected value {corrected_value:.4f} within tolerance"
            ),
            "method": "reexec",
            "original_value": round(original_value, 4),
            "corrected_value": round(corrected_value, 4),
            "pct_difference": round(pct_diff, 4),
        }

    except Exception as exc:
        return {"flagged": False, "reason": f"could not verify: {exc}", "method": "reexec"}


# ---------------------------------------------------------------------------
# Temporal-filter check — constants and helpers
# ---------------------------------------------------------------------------

_STATUS_CARDINALITY_MAX = 15   # VARCHAR columns with ≤ this many distinct values are "status-like"

# Matches temporal language in natural-language questions.
_TEMPORAL_RE = re.compile(
    r"""
    \b(19|20)\d{2}\b                                             # 4-digit year: 2017, 2018 …
    | \b(january|february|march|april|may|june|july|             # full month names
         august|september|october|november|december)\b
    | \b(jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b          # abbreviated months
    | \b(last|past|previous)\s+(day|week|month|year|quarter)\b   # "last year" etc.
    | \b(this|current)\s+(week|month|year|quarter)\b             # "this month" etc.
    | \bq[1-4]\b                                                 # Q1 … Q4
    | \b(quarter|recent|latest|ytd|mtd|yesterday|today)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _has_temporal_language(question: str) -> bool:
    return bool(_TEMPORAL_RE.search(question))


def _is_temporal_type(col_type: str) -> bool:
    """Return True for DATE, TIME, TIMESTAMP, DATETIME column types."""
    # Strip precision suffixes like TIMESTAMP(6) before comparing
    base = col_type.upper().split("(")[0].strip()
    return any(base.startswith(t) for t in ("TIMESTAMP", "DATE", "DATETIME", "TIME"))


def _column_types(con) -> dict[str, dict[str, str]]:
    """Return {table_name: {col_name: type_str}} for all tables."""
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    return {
        t: {r[1]: r[2] for r in con.execute(f'PRAGMA table_info("{t}")').fetchall()}
        for t in tables
    }


# ---------------------------------------------------------------------------
# F2 check: missing temporal filter
# ---------------------------------------------------------------------------

def check_missing_temporal_filter(question: str, sql: str, con) -> dict[str, Any]:
    """
    Soft-flag when the question contains temporal language (year, month, quarter,
    "last N", etc.) but the SQL has no predicate on a DATE or TIMESTAMP column.

    Steps:
      1. Scan the question with _TEMPORAL_RE. If no match, return not-flagged.
      2. Parse SQL; find WHERE/HAVING clauses.
      3. For each Column node inside those clauses, resolve its type from metadata.
      4. If any DATE/TIMESTAMP column is found in a predicate, return not-flagged.
      5. Otherwise, soft-flag.

    Returns: {flagged, reason, severity="soft", method="temporal"}
    """
    if not _has_temporal_language(question):
        return {
            "flagged": False,
            "reason": "no temporal language detected in question",
            "severity": "soft",
            "method": "temporal",
        }

    try:
        ast = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception as exc:
        return {"flagged": False, "reason": f"parse error: {exc}", "severity": "soft", "method": "temporal"}

    # Build alias → real table map across the full AST
    alias_to_real: dict[str, str] = {}
    for tbl in ast.find_all(exp.Table):
        if tbl.name:
            alias_to_real[tbl.alias or tbl.name] = tbl.name

    schema_types = _column_types(con)

    # Check WHERE and HAVING clauses for any DATE/TIMESTAMP predicate
    for clause_cls in (exp.Where, exp.Having):
        clause = ast.find(clause_cls)
        if not clause:
            continue
        for col in clause.find_all(exp.Column):
            col_name = col.name
            t_qualifier = col.table

            col_type: str | None = None
            if t_qualifier:
                real_table = alias_to_real.get(t_qualifier)
                if real_table and real_table in schema_types:
                    col_type = schema_types[real_table].get(col_name)
            else:
                # Unqualified: check all referenced real tables
                for real_table in alias_to_real.values():
                    if real_table in schema_types and col_name in schema_types[real_table]:
                        col_type = schema_types[real_table][col_name]
                        break

            if col_type and _is_temporal_type(col_type):
                return {
                    "flagged": False,
                    "reason": f"temporal predicate found on column '{col_name}' ({col_type})",
                    "severity": "soft",
                    "method": "temporal",
                }

    return {
        "flagged": True,
        "reason": (
            "question contains temporal language but SQL has no predicate "
            "on any DATE or TIMESTAMP column"
        ),
        "severity": "soft",
        "method": "temporal",
    }


# ---------------------------------------------------------------------------
# F2 check: unconstrained status column
# ---------------------------------------------------------------------------

def check_unconstrained_status(question: str, sql: str, con) -> dict[str, Any]:
    """
    Soft-flag when an aggregate query references a table with a status-like column
    (low-cardinality VARCHAR, 2–15 distinct values) that is not filtered in the
    WHERE clause. Checks the primary FROM table only; JOIN-table status columns
    are out of scope for this heuristic.

    Returns: {flagged, reason, severity="soft", method="status"}
    """
    try:
        ast = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception as exc:
        return {"flagged": False, "reason": f"parse error: {exc}", "severity": "soft", "method": "status"}

    if not isinstance(ast, exp.Select):
        return {"flagged": False, "reason": "not a SELECT", "severity": "soft", "method": "status"}

    # Only trigger for SUM/AVG — status filtering is about value aggregates, not row counts.
    # COUNT(*) queries ("how many X?") genuinely want all rows and don't need a status guard.
    has_value_agg = (
        next(ast.find_all(exp.Sum), None) is not None
        or next(ast.find_all(exp.Avg), None) is not None
    )
    if not has_value_agg:
        return {"flagged": False, "reason": "no SUM/AVG aggregate — status check not applicable", "severity": "soft", "method": "status"}

    # Get primary FROM table
    from_clause = ast.args.get("from_")
    if not from_clause or not isinstance(from_clause.this, exp.Table):
        return {"flagged": False, "reason": "FROM is not a base table", "severity": "soft", "method": "status"}

    from_tbl = from_clause.this
    from_real = from_tbl.name
    from_alias = from_tbl.alias or from_real

    # Find status-like columns in the FROM table: VARCHAR with 2–STATUS_CARDINALITY_MAX distinct values
    try:
        cols_info = con.execute(f'PRAGMA table_info("{from_real}")').fetchall()
    except Exception:
        return {"flagged": False, "reason": "could not read table metadata", "severity": "soft", "method": "status"}

    status_cols: list[tuple[str, int]] = []
    for row in cols_info:
        col_name, col_type = row[1], row[2]
        if "VARCHAR" not in col_type.upper() and "TEXT" not in col_type.upper():
            continue
        try:
            distinct = con.execute(
                f'SELECT COUNT(DISTINCT "{col_name}") FROM "{from_real}"'
            ).fetchone()[0]
        except Exception:
            continue
        if 1 < distinct <= _STATUS_CARDINALITY_MAX:
            status_cols.append((col_name, distinct))

    if not status_cols:
        return {
            "flagged": False,
            "reason": f"no status-like columns found in '{from_real}'",
            "severity": "soft",
            "method": "status",
        }

    # Collect column names and (alias, name) pairs present in the WHERE clause
    where = ast.find(exp.Where)
    where_names: set[str] = set()
    where_qualified: set[tuple[str, str]] = set()
    if where:
        for col in where.find_all(exp.Column):
            where_names.add(col.name)
            if col.table:
                where_qualified.add((col.table, col.name))

    # Check which status columns are unconstrained
    unconstrained = [
        f"{from_real}.{col} ({n} distinct values)"
        for col, n in status_cols
        if col not in where_names and (from_alias, col) not in where_qualified
    ]

    if unconstrained:
        return {
            "flagged": True,
            "reason": (
                f"aggregate on '{from_real}' has unconstrained status-like column(s): "
                f"{unconstrained}. Result may include rows with unintended status values "
                f"(e.g. cancelled, pending)."
            ),
            "severity": "soft",
            "method": "status",
        }

    return {
        "flagged": False,
        "reason": f"all status-like columns in '{from_real}' are constrained in WHERE",
        "severity": "soft",
        "method": "status",
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    from pathlib import Path
    import duckdb

    DB_PATH = str(Path(__file__).parent.parent / "data" / "olist.duckdb")
    con = duckdb.connect(DB_PATH, read_only=True)

    # -----------------------------------------------------------------------
    # True positive: known fan-out query (should be flagged by both detectors)
    # -----------------------------------------------------------------------
    FANOUT_SQL = (
        "SELECT p.product_category_name, SUM(op.payment_value) "
        "FROM products p "
        "JOIN order_items oi ON p.product_id = oi.product_id "
        "JOIN orders o ON oi.order_id = o.order_id "
        "JOIN order_payments op ON o.order_id = op.order_id "
        "GROUP BY p.product_category_name"
    )

    print("=" * 60)
    print("TRUE POSITIVE — known fan-out (expect: flagged=true)")
    print("=" * 60)
    print("  Detector A:", json.dumps(detect_fanout_ast(FANOUT_SQL, con)))
    print("  Detector B:", json.dumps(detect_fanout_reexec(FANOUT_SQL, con)))

    # -----------------------------------------------------------------------
    # False positive checks: correct queries that must NOT be flagged
    # -----------------------------------------------------------------------
    CLEAN_QUERIES = [
        (
            "no join, simple SUM on source table",
            "SELECT SUM(payment_value) FROM order_payments",
        ),
        (
            "join present but aggregate uses DISTINCT — no fan-out risk",
            "SELECT COUNT(DISTINCT o.order_id) "
            "FROM orders o JOIN order_payments op ON o.order_id = op.order_id",
        ),
        (
            "correctly grain-handled: AVG over pre-aggregated subquery",
            "SELECT AVG(order_total) FROM ("
            "SELECT order_id, SUM(payment_value) AS order_total "
            "FROM order_payments GROUP BY order_id"
            ")",
        ),
    ]

    print()
    print("=" * 60)
    print("FALSE POSITIVE CHECKS — correct queries (expect: flagged=false)")
    print("=" * 60)
    false_positives = []
    for label, sql in CLEAN_QUERIES:
        ra = detect_fanout_ast(sql, con)
        rb = detect_fanout_reexec(sql, con)
        a_flag = ra["flagged"]
        b_flag = rb["flagged"]
        fp = a_flag or b_flag
        if fp:
            false_positives.append(label)
        status = "FALSE POSITIVE" if fp else "OK"
        print(f"\n  [{status}] {label}")
        print(f"    A: flagged={a_flag}  reason={ra['reason']}")
        print(f"    B: flagged={b_flag}  reason={rb['reason']}")

    print()
    print("=" * 60)
    if false_positives:
        print(f"RESULT: {len(false_positives)} false positive(s):")
        for fp in false_positives:
            print(f"  - {fp}")
    else:
        print("RESULT: 0 false positives — all clean queries passed correctly.")
    print("=" * 60)

    # -----------------------------------------------------------------------
    # Wrong-grain: true positives (should flag)
    # -----------------------------------------------------------------------
    WRONG_GRAIN_TP = [
        (
            "Q2 baseline failure: AVG over payment rows, not order grain "
            "(agent=154.10, correct=160.99)",
            "SELECT AVG(payment_value) FROM order_payments",
        ),
        (
            "Q6 baseline failure: AVG of order_item_id (a position counter, not a count) "
            "(agent=1.198, correct=1.1417)",
            "SELECT AVG(order_item_id) FROM order_items",
        ),
    ]

    print()
    print("=" * 60)
    print("WRONG-GRAIN TRUE POSITIVES (expect: flagged=true)")
    print("=" * 60)
    for label, sql in WRONG_GRAIN_TP:
        ra = detect_wrong_grain_ast(sql, con)
        rb = detect_wrong_grain_reexec(sql, con)
        print(f"\n  {label}")
        print(f"    A: flagged={ra['flagged']}  reason={ra['reason']}")
        print(f"    B: flagged={rb['flagged']}  reason={rb['reason']}")

    # -----------------------------------------------------------------------
    # Wrong-grain: true negative (should NOT flag)
    # -----------------------------------------------------------------------
    WRONG_GRAIN_TN = [
        (
            "correctly grain-handled: AVG over per-order subquery (expect: flagged=false)",
            "SELECT AVG(order_total) FROM ("
            "SELECT order_id, SUM(payment_value) AS order_total "
            "FROM order_payments GROUP BY order_id"
            ")",
        ),
    ]

    print()
    print("=" * 60)
    print("WRONG-GRAIN TRUE NEGATIVE (expect: flagged=false)")
    print("=" * 60)
    wg_fps = []
    for label, sql in WRONG_GRAIN_TN:
        ra = detect_wrong_grain_ast(sql, con)
        rb = detect_wrong_grain_reexec(sql, con)
        fp = ra["flagged"] or rb["flagged"]
        if fp:
            wg_fps.append(label)
        status = "FALSE POSITIVE" if fp else "OK"
        print(f"\n  [{status}] {label}")
        print(f"    A: flagged={ra['flagged']}  reason={ra['reason']}")
        print(f"    B: flagged={rb['flagged']}  reason={rb['reason']}")

    print()
    print("=" * 60)
    print(f"WRONG-GRAIN RESULT: {len(wg_fps)} false positive(s)" if wg_fps
          else "WRONG-GRAIN RESULT: 0 false positives.")
    print("=" * 60)

    # -----------------------------------------------------------------------
    # Temporal filter check
    # -----------------------------------------------------------------------
    TEMPORAL_CASES = [
        # (label, question, sql, expect_flagged)
        (
            "SHOULD FLAG: temporal question, no date predicate",
            "How many orders in 2018?",
            "SELECT COUNT(*) FROM orders",
            True,
        ),
        (
            "SHOULD NOT FLAG: temporal question, date predicate present",
            "How many orders in 2018?",
            "SELECT COUNT(*) FROM orders "
            "WHERE order_purchase_timestamp >= '2018-01-01' "
            "AND order_purchase_timestamp < '2019-01-01'",
            False,
        ),
        (
            "SHOULD NOT FLAG: no temporal language in question",
            "How many orders are there?",
            "SELECT COUNT(*) FROM orders",
            False,
        ),
    ]

    print()
    print("=" * 60)
    print("TEMPORAL FILTER CHECK")
    print("=" * 60)
    temporal_errors = []
    for label, question, sql, expect in TEMPORAL_CASES:
        result = check_missing_temporal_filter(question, sql, con)
        ok = result["flagged"] == expect
        if not ok:
            temporal_errors.append(label)
        verdict = "OK" if ok else ("FALSE POSITIVE" if result["flagged"] else "FALSE NEGATIVE")
        print(f"\n  [{verdict}] {label}")
        print(f"    flagged={result['flagged']}  reason={result['reason']}")

    # -----------------------------------------------------------------------
    # Unconstrained status check
    # -----------------------------------------------------------------------
    STATUS_CASES = [
        # (label, question, sql, expect_flagged)
        (
            "SHOULD FLAG: aggregate with unconstrained order_status",
            "What is total revenue?",
            "SELECT SUM(payment_value) FROM orders o "
            "JOIN order_payments op ON o.order_id=op.order_id",
            True,
        ),
        (
            "SHOULD NOT FLAG: order_status constrained in WHERE",
            "What is delivered revenue?",
            "SELECT SUM(payment_value) FROM orders o "
            "JOIN order_payments op ON o.order_id=op.order_id "
            "WHERE o.order_status='delivered'",
            False,
        ),
    ]

    print()
    print("=" * 60)
    print("UNCONSTRAINED STATUS CHECK")
    print("=" * 60)
    status_errors = []
    for label, question, sql, expect in STATUS_CASES:
        result = check_unconstrained_status(question, sql, con)
        ok = result["flagged"] == expect
        if not ok:
            status_errors.append(label)
        verdict = "OK" if ok else ("FALSE POSITIVE" if result["flagged"] else "FALSE NEGATIVE")
        print(f"\n  [{verdict}] {label}")
        print(f"    flagged={result['flagged']}  reason={result['reason']}")

    print()
    print("=" * 60)
    total_new = len(temporal_errors) + len(status_errors)
    print(f"FILTER CHECKS RESULT: {total_new} error(s)" if total_new
          else "FILTER CHECKS RESULT: all 5 cases classified correctly.")
    print("=" * 60)

    con.close()
