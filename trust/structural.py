"""F1 structural checks: schema existence and SQL validity via sqlglot AST parsing."""

from __future__ import annotations

from typing import Any

import sqlglot
import sqlglot.expressions as exp


# ---------------------------------------------------------------------------
# Schema helper
# ---------------------------------------------------------------------------

def get_real_schema(con) -> dict[str, set[str]]:
    """
    Return {table_name: {col_name, ...}} for every table in the database.
    Reads from live DuckDB metadata — no names hardcoded.
    """
    tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
    return {
        table: {row[1] for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
        for table in tables
    }


# ---------------------------------------------------------------------------
# F1 check
# ---------------------------------------------------------------------------

def check_schema_exists(sql: str, con) -> dict[str, Any]:
    """
    Validate that every table and column referenced in the SQL actually exists
    in the database schema. Schema-agnostic: the real schema is read from the
    live DuckDB connection, never hardcoded.

    Resolution rules:
      - Qualified references (alias.column): alias is resolved to its real table
        via the alias map; the column is then checked against that table's columns.
      - Unqualified references (bare column name): checked against ALL real tables
        referenced anywhere in the query; flagged only if absent from every one.
      - Columns referencing a missing table are skipped (the table is already flagged).
      - Subquery-derived aliases that resolve to no real table are skipped.
      - Derived names the query introduces are valid and never flagged: CTE names
        and subquery aliases (derived tables), and explicit SELECT-list aliases
        like `COUNT(x) AS n` (derived columns). Bare column projections are still
        validated, so a genuinely missing column still flags.
      - SELECT * and positional GROUP BY (GROUP BY 1) are not checked.

    Returns:
      {flagged, reason, missing_tables, missing_columns, method="structural"}
    """
    real_schema = get_real_schema(con)
    real_table_names = set(real_schema.keys())

    try:
        ast = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception as exc:
        return {
            "flagged": True,
            "reason": f"SQL failed to parse: {exc}",
            "missing_tables": [],
            "missing_columns": [],
            "method": "structural",
        }

    # -----------------------------------------------------------------------
    # 0. Collect names the query INTRODUCES itself — valid to reference even
    #    though they are not in the base schema, and must not be reported as
    #    hallucinations:
    #      - derived TABLES:  CTE names and subquery aliases
    #      - derived COLUMNS: explicit SELECT-list aliases (`expr AS name`)
    #    Bare column projections are NOT treated as derived, so a genuinely
    #    missing column (e.g. SELECT customer_name FROM orders) still flags.
    # -----------------------------------------------------------------------
    derived_tables: set[str] = set()
    for cte in ast.find_all(exp.CTE):
        if cte.alias:
            derived_tables.add(cte.alias)
    for sub in ast.find_all(exp.Subquery):
        if sub.alias:
            derived_tables.add(sub.alias)

    derived_columns: set[str] = set()
    for sel in ast.find_all(exp.Select):
        for proj in sel.expressions:
            if isinstance(proj, exp.Alias) and proj.alias:
                derived_columns.add(proj.alias)

    # -----------------------------------------------------------------------
    # 1. Collect all Table nodes (recurses into subqueries) and build alias map.
    #    Flag any base-table reference not in the real schema; skip references to
    #    CTEs / derived tables the query produced.
    # -----------------------------------------------------------------------
    alias_to_real: dict[str, str] = {}   # alias_or_name -> real table name
    referenced_real_tables: set[str] = set()
    missing_tables: list[str] = []

    for tbl in ast.find_all(exp.Table):
        real_name = tbl.name
        if not real_name or real_name in derived_tables:
            continue
        alias = tbl.alias or real_name
        alias_to_real[alias] = real_name

        if real_name not in real_table_names:
            if real_name not in missing_tables:
                missing_tables.append(real_name)
        else:
            referenced_real_tables.add(real_name)

    # -----------------------------------------------------------------------
    # 2. Check every Column node. Derived columns (SELECT aliases) are valid
    #    identifiers regardless of qualification, so skip them up front.
    # -----------------------------------------------------------------------
    missing_columns_set: set[str] = set()

    for col in ast.find_all(exp.Column):
        col_name = col.name
        t_qualifier = col.table   # table alias or name prefix; empty string if absent

        if not col_name or col_name in derived_columns:
            continue

        if t_qualifier:
            # Qualified: resolve the alias to a real table name.
            real_table = alias_to_real.get(t_qualifier)
            if real_table is None:
                # Unknown alias — likely a subquery/CTE alias; skip.
                continue
            if real_table not in real_table_names:
                # Table itself is missing; already flagged, skip column.
                continue
            if col_name not in real_schema[real_table]:
                missing_columns_set.add(f"{real_table}.{col_name}")
        else:
            # Unqualified: acceptable if the column exists in ANY referenced
            # real table. If no real tables are known, we cannot verify — skip.
            if not referenced_real_tables:
                continue
            found = any(
                col_name in real_schema[rt]
                for rt in referenced_real_tables
                if rt in real_schema
            )
            if not found:
                missing_columns_set.add(col_name)

    missing_columns = sorted(missing_columns_set)
    flagged = bool(missing_tables or missing_columns)

    if not flagged:
        reason = "all referenced tables and columns exist in the schema"
    else:
        parts = []
        if missing_tables:
            parts.append(f"unknown table(s): {missing_tables}")
        if missing_columns:
            parts.append(f"unknown column(s): {missing_columns}")
        reason = "; ".join(parts)

    return {
        "flagged": flagged,
        "reason": reason,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "method": "structural",
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

    SHOULD_NOT_FLAG = [
        (
            "clean: unqualified columns, single table",
            "SELECT order_id, order_status FROM orders",
        ),
        (
            "clean: qualified columns, JOIN, positional GROUP BY",
            "SELECT p.product_category_name, SUM(oi.price) "
            "FROM products p "
            "JOIN order_items oi ON p.product_id = oi.product_id "
            "GROUP BY 1",
        ),
    ]

    SHOULD_FLAG = [
        (
            "hallucinated column on real table",
            "SELECT customer_name FROM orders",
        ),
        (
            "hallucinated table",
            "SELECT * FROM order_history",
        ),
        (
            "two hallucinated columns on real table",
            "SELECT signup_date, total_spent FROM customers",
        ),
        (
            "qualified hallucinated column in JOIN query",
            "SELECT o.fake_column FROM orders o "
            "JOIN order_items oi ON o.order_id = oi.order_id",
        ),
    ]

    SHOULD_NOT_FLAG.append((
        "clean: qualified real columns from two-table JOIN",
        "SELECT o.order_status, oi.price FROM orders o "
        "JOIN order_items oi ON o.order_id = oi.order_id",
    ))

    print("=" * 60)
    print("SHOULD NOT FLAG (expect: flagged=false)")
    print("=" * 60)
    false_positives = []
    for label, sql in SHOULD_NOT_FLAG:
        result = check_schema_exists(sql, con)
        ok = not result["flagged"]
        if not ok:
            false_positives.append(label)
        print(f"\n  [{'OK' if ok else 'FALSE POSITIVE'}] {label}")
        print(f"    flagged={result['flagged']}")
        print(f"    reason={result['reason']}")

    print()
    print("=" * 60)
    print("SHOULD FLAG (expect: flagged=true)")
    print("=" * 60)
    false_negatives = []
    for label, sql in SHOULD_FLAG:
        result = check_schema_exists(sql, con)
        ok = result["flagged"]
        if not ok:
            false_negatives.append(label)
        print(f"\n  [{'OK' if ok else 'FALSE NEGATIVE'}] {label}")
        print(f"    flagged={result['flagged']}")
        print(f"    missing_tables={result['missing_tables']}")
        print(f"    missing_columns={result['missing_columns']}")
        print(f"    reason={result['reason']}")

    print()
    print("=" * 60)
    total_errors = len(false_positives) + len(false_negatives)
    if total_errors:
        print(f"RESULT: {len(false_positives)} false positive(s), {len(false_negatives)} false negative(s)")
    else:
        print("RESULT: all 5 queries classified correctly — 0 errors.")
    print("=" * 60)

    con.close()
