"""Tests for the F1 structural schema-existence check (trust/structural.py).

Regression guard for the t2 false positive: a correct nested-aggregate query that
references a subquery/CTE-derived column (e.g. `AVG(count)` where `count` is the
SELECT alias of an inner aggregate) must NOT be flagged as referencing a missing
column. Genuine hallucinations (unknown table / unknown real column) must still flag.
"""

import duckdb
import pytest

from trust.structural import check_schema_exists


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE order_items ("
        "order_id VARCHAR, order_item_id BIGINT, product_id VARCHAR, "
        "seller_id VARCHAR, price DOUBLE, freight_value DOUBLE)"
    )
    try:
        yield c
    finally:
        c.close()


# --- The t2 false positive: derived columns must be recognized --------------- #

def test_subquery_derived_alias_not_flagged(con):
    # `count` is the subquery's SELECT alias, not a real order_items column.
    sql = (
        "SELECT AVG(count) AS average_items_per_order "
        "FROM (SELECT order_id, COUNT(order_item_id) AS count "
        "FROM order_items GROUP BY order_id) AS subquery"
    )
    res = check_schema_exists(sql, con)
    assert res["flagged"] is False, res


def test_cte_derived_alias_not_flagged(con):
    # CTE name must be treated as a valid table, and `cnt` as a valid derived column.
    sql = (
        "WITH per_order AS ("
        "SELECT order_id, COUNT(*) AS cnt FROM order_items GROUP BY order_id) "
        "SELECT AVG(cnt) AS avg_items FROM per_order"
    )
    res = check_schema_exists(sql, con)
    assert res["flagged"] is False, res


# --- Regression guards: real hallucinations must still flag ------------------- #

def test_real_missing_column_still_flagged(con):
    res = check_schema_exists("SELECT customer_name FROM order_items", con)
    assert res["flagged"] is True
    assert "customer_name" in res["missing_columns"]


def test_real_missing_table_still_flagged(con):
    res = check_schema_exists("SELECT * FROM nonexistent_table", con)
    assert res["flagged"] is True
    assert "nonexistent_table" in res["missing_tables"]


def test_real_column_in_simple_query_passes(con):
    res = check_schema_exists("SELECT order_id, price FROM order_items", con)
    assert res["flagged"] is False, res
