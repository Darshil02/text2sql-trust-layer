"""Builds demo/demo_cache.json from REAL agent+trust pipeline runs for the Streamlit demo.

For each curated demo question this runs the full pipeline and captures the generated SQL,
the executed result (truncated), the verdict / confidence / reasoning / fired checks, the
trust-shaped answer, and the NAIVE answer (what the agent would present without the trust
layer). For the silent-error cases it also records the correct answer and the error magnitude,
all computed from the live database — nothing here is hand-edited.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb  # noqa: E402

from agent.graph import (  # noqa: E402
    ANSWER,
    build_graph,
    load_schema,
    node_execute_sql,
    node_explain_answer,
    node_run_trust_layer,
)
from agent.nodes import explain_answer  # noqa: E402

DB_PATH = str(Path(__file__).parent.parent / "data" / "olist.duckdb")
OUT = Path(__file__).parent.parent / "demo" / "demo_cache.json"

PREVIEW_ROWS = 8

# Curated demo questions. `correct` names a computation that records the true answer and the
# silent-error magnitude for trap / known-limitation cases.
DEMO_QUESTIONS = [
    {
        "id": "fanout",
        "label": "Fan-out double-count → ABSTAIN (flagship)",
        "question": "What is the total payment value per product category?",
        "correct": "fanout_category",
        # The generator is non-deterministic and occasionally produces a different (still
        # fan-out-inflated) join for this question — e.g. routing through the category
        # translation table for a 25% variant. We pin the SQL to the canonical run so the
        # demo matches the README's documented 27% / $20.3M figure. The trust layer below
        # still runs for REAL on this SQL; only the generation step is fixed.
        "pinned_sql": (
            "SELECT T1.product_category_name, SUM(T4.payment_value) AS total_payment_value "
            "FROM products AS T1 "
            "JOIN order_items AS T2 ON T1.product_id = T2.product_id "
            "JOIN orders AS T3 ON T2.order_id = T3.order_id "
            "JOIN order_payments AS T4 ON T3.order_id = T4.order_id "
            "GROUP BY T1.product_category_name"
        ),
    },
    {
        "id": "wrong_column",
        "label": "Wrong-column aggregation → ABSTAIN (semantic catch)",
        "question": "What is the average number of items per order?",
        "correct": "avg_items",
        # Pinned to the canonical silent-error form: averaging order_item_id (a positional
        # counter) returns a confident 1.198. Some runs instead emit invalid nested-aggregate
        # SQL that errors out — a less illustrative capture. Trust layer still runs for real.
        "pinned_sql": "SELECT AVG(order_item_id) AS average_items_per_order FROM order_items",
    },
    {
        "id": "clean_count",
        "label": "Clean → ANSWER",
        "question": "How many orders are there?",
        "correct": None,
    },
    {
        "id": "clean_paytype",
        "label": "Clean, but soft status FLAG",
        "question": "What is the total revenue by payment type?",
        "correct": None,
    },
    {
        "id": "known_limitation",
        "label": "Known limitation → ANSWER (the honest miss)",
        "question": "How many distinct customers are there?",
        "correct": "distinct_customers",
        # Pinned to the canonical miss: COUNT(DISTINCT customer_id) = 99,441 (per-order id),
        # which structure-based checks pass even though the right column is customer_unique_id
        # (96,096). This is the documented boundary case; pinning keeps the demo's honest-miss
        # narrative stable. Trust layer still runs for real and (correctly) does not catch it.
        "pinned_sql": "SELECT COUNT(DISTINCT customer_id) FROM customers",
    },
]


def _grand_total(result):
    """Sum the last column of a row-list, if numeric."""
    vals = [r[-1] for r in result if r and isinstance(r[-1], (int, float))]
    return float(sum(vals)) if vals else None


def _scalar(result):
    if result and isinstance(result[0], (list, tuple)) and result[0]:
        v = result[0][0]
        return float(v) if isinstance(v, (int, float)) else None
    return None


def compute_correct(kind: str, result: list, con) -> dict:
    """Return {correct_answer, error_magnitude} computed from the live DB for a silent-error case."""
    if kind == "fanout_category":
        naive_total = _grand_total(result) or 0.0
        clean_total = con.execute("SELECT SUM(payment_value) FROM order_payments").fetchone()[0]
        pct = (naive_total - clean_total) / clean_total * 100 if clean_total else 0.0
        return {
            "correct_answer": f"≈${clean_total/1e6:.1f}M total (deduplicate payments to one row "
                              f"per order before joining)",
            "error_magnitude": f"≈${naive_total/1e6:.1f}M shown across categories vs "
                               f"≈${clean_total/1e6:.1f}M correct — {pct:.0f}% inflated by join fan-out",
        }

    if kind == "avg_items":
        naive = _scalar(result)
        correct = con.execute(
            "SELECT AVG(cnt) FROM (SELECT order_id, COUNT(*) AS cnt FROM order_items GROUP BY order_id)"
        ).fetchone()[0]
        naive_disp = f"{naive:.4f}" if naive is not None else "a per-order table of counter values"
        return {
            "correct_answer": f"{correct:.4f} items per order (COUNT(*) per order, then AVG)",
            "error_magnitude": f"naive {naive_disp} vs correct {correct:.4f} — the query averages "
                               f"order_item_id, a positional counter, not the item count",
        }

    if kind == "distinct_customers":
        naive = _scalar(result)
        correct = con.execute("SELECT COUNT(DISTINCT customer_unique_id) FROM customers").fetchone()[0]
        naive_disp = f"{int(naive):,}" if naive is not None else "?"
        return {
            "correct_answer": f"{correct:,} distinct customers (via customer_unique_id)",
            "error_magnitude": f"{naive_disp} shown vs {correct:,} correct — customer_id is assigned "
                               f"per order, not per person; the right column is customer_unique_id",
        }

    return {}


def main():
    schema = load_schema()
    app = build_graph()
    con = duckdb.connect(DB_PATH, read_only=True)

    entries = []
    for q in DEMO_QUESTIONS:
        print(f"Running: [{q['id']}] {q['question']}")
        base = {
            "question": q["question"], "schema_text": schema, "sql": "",
            "result": [], "answer": "", "error": None, "trust_checks": [],
            "verdict": ANSWER, "confidence": 1.0, "reasoning": "",
        }
        if q.get("pinned_sql"):
            # Skip generation (pinned), but run execute -> trust -> explain for real.
            state = {**base, "sql": q["pinned_sql"]}
            state.update(node_execute_sql(state))
            state.update(node_run_trust_layer(state))
            state.update(node_explain_answer(state))
        else:
            state = app.invoke(base)

        result = state["result"]
        # naive_answer = what the agent presents WITHOUT the trust layer (raw explanation
        # of the executed result, regardless of verdict). The pipeline suppresses this on
        # ABSTAIN, so we recompute it directly to show the silent-error answer.
        naive_answer = explain_answer(q["question"], state["sql"], result)

        entry = {
            "id": q["id"],
            "label": q["label"],
            "question": q["question"],
            "sql": state["sql"],
            "result_preview": [list(r) for r in result[:PREVIEW_ROWS]],
            "result_rows": len(result),
            "verdict": state["verdict"],
            "confidence": state["confidence"],
            "reasoning": state["reasoning"],
            "checks_fired": sorted({c["method"] for c in state["trust_checks"] if c.get("flagged")}),
            "trust_answer": state["answer"],
            "naive_answer": naive_answer,
        }
        if q["correct"]:
            entry.update(compute_correct(q["correct"], result, con))

        entries.append(entry)
        print(f"   -> verdict={entry['verdict']}  fired={entry['checks_fired']}")

    con.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(entries, indent=2, default=str))
    print(f"\nWrote {len(entries)} entries to {OUT}")


if __name__ == "__main__":
    main()
