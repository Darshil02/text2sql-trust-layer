"""Test question set with hand-verified ground-truth answers for evaluating the SQL agent pipeline.

Every ground_truth value below was produced by a hand-written correct query run directly
against data/olist.duckdb — no agent involvement. Questions are organized into three buckets:

  - "in_scope_trap"     A query the naive agent is prone to get silently wrong (fan-out,
                        wrong-grain, missing temporal filter, unconstrained status, etc.).
                        The trust layer is EXPECTED to catch these. They drive headline recall.

  - "clean"             A query with a correct, unambiguous answer that the agent should get
                        right and the trust layer should NOT flag. These drive the
                        false-positive rate. Includes "hard_clean" cases that look risky
                        (joins, aggregation) but are actually correct.

  - "known_limitation"  A documented failure the structure-based trust layer CANNOT catch,
                        because correctness depends on real-world knowledge of what a column
                        means (see docs/detectors.md, "semantic column-meaning errors").
                        These are reported SEPARATELY and are NOT counted in headline recall —
                        counting them would either understate recall (as misses) or dishonestly
                        pad it (if we pretended a check covered them).

Schema per dict: {id, question, ground_truth, bucket, trap_type, notes}.
ground_truth may be numeric (auto-comparable within tolerance) or a descriptive string
(needs manual confirmation — multi-row results, top-N spot-checks, etc.).
"""

QUESTIONS = [
    # ------------------------------------------------------------------ #
    # IN-SCOPE TRAPS — trust layer is expected to FLAG / ABSTAIN          #
    # ------------------------------------------------------------------ #
    {
        "id": "t1",
        "question": "What is the average payment value per order?",
        "ground_truth": 160.99,
        "bucket": "in_scope_trap",
        "trap_type": "wrong_grain",
        "notes": "Agent computes 154.10 by averaging over payment rows instead of per-order totals.",
    },
    {
        "id": "t2",
        "question": "What is the average number of items per order?",
        "ground_truth": 1.1417,
        "bucket": "in_scope_trap",
        "trap_type": "wrong_column_grain",
        "notes": "Agent averages order_item_id (a positional counter) instead of COUNT(*) per order.",
    },
    {
        "id": "t3",
        "question": "What is the total payment value per product category?",
        "ground_truth": "per-category totals require per-order dedup before join; "
                        "clean overall total ~16.0M, naive 4-table join inflates ~27% to ~20.3M",
        "bucket": "in_scope_trap",
        "trap_type": "fanout",
        "notes": "Joining order_payments (many rows per order) multiplies item rows before SUM.",
    },
    {
        "id": "t4",
        "question": "What is the total revenue per seller?",
        "ground_truth": "top seller 4869f7a5dfa277a7dca6462dcf3b52b2 = 229472.63 "
                        "(via direct join on order_items, no fan-out)",
        "bucket": "in_scope_trap",
        "trap_type": "fanout",
        "notes": "Correct via sellers JOIN order_items only; joining further tables inflates revenue.",
    },
    {
        "id": "t5",
        "question": "How many orders were placed in 2017?",
        "ground_truth": 45101,
        "bucket": "in_scope_trap",
        "trap_type": "missing_temporal",
        "notes": "Agent may omit the date filter and return the all-time count (99441).",
    },
    {
        "id": "t6",
        "question": "What is the total revenue from delivered orders only?",
        "ground_truth": 15422461.77,
        "bucket": "in_scope_trap",
        "trap_type": "status",
        "notes": "Agent may omit the order_status='delivered' filter and include cancelled/other rows.",
    },
    {
        "id": "t7",
        "question": "What is the number of orders per product category?",
        "ground_truth": "top category cama_mesa_banho = 9417 distinct orders "
                        "(correct uses COUNT(DISTINCT order_id))",
        "bucket": "in_scope_trap",
        "trap_type": "fanout_count",
        "notes": "Naive COUNT(*) over the item-level join overcounts orders with multiple items.",
    },

    # ------------------------------------------------------------------ #
    # CLEAN — correct answer, trust layer should NOT flag                 #
    # ------------------------------------------------------------------ #
    {
        "id": "c1",
        "question": "How many orders are there?",
        "ground_truth": 99441,
        "bucket": "clean",
        "trap_type": None,
        "notes": None,
    },
    {
        "id": "c2",
        "question": "What is the total revenue by payment type?",
        "ground_truth": "credit_card ~12.54M, boleto ~2.87M, voucher ~0.38M, debit_card ~0.22M",
        "bucket": "clean",
        "trap_type": None,
        "notes": "Multi-row result — needs manual confirmation. Direct GROUP BY on order_payments, no joins.",
    },
    {
        "id": "c3",
        "question": "How many sellers are there?",
        "ground_truth": 3095,
        "bucket": "clean",
        "trap_type": None,
        "notes": None,
    },
    {
        "id": "c4",
        "question": "How many products are there?",
        "ground_truth": 32951,
        "bucket": "clean",
        "trap_type": None,
        "notes": None,
    },
    {
        "id": "c5",
        "question": "How many delivered orders have payments?",
        "ground_truth": 96477,
        "bucket": "clean",
        "trap_type": "hard_clean",
        "notes": "Correct join orders→order_payments with status filter; COUNT(DISTINCT order_id) — no fan-out.",
    },
    {
        "id": "c6",
        "question": "What is the average per-order payment total?",
        "ground_truth": 160.99,
        "bucket": "clean",
        "trap_type": "hard_clean",
        "notes": "Pre-aggregated per order then averaged — the correct form of the t1 trap. Should pass.",
    },
    {
        "id": "c7",
        "question": "How many orders have been placed since the start of 2018?",
        "ground_truth": 54011,
        "bucket": "clean",
        "trap_type": "correct_temporal",
        "notes": "Question is temporal AND the correct query has a date filter — should not be flagged.",
    },

    # ------------------------------------------------------------------ #
    # KNOWN LIMITATION — structure-based checks cannot catch this         #
    # ------------------------------------------------------------------ #
    {
        "id": "k1",
        "question": "How many distinct customers are there?",
        "ground_truth": 96096,
        "bucket": "known_limitation",
        "trap_type": "semantic_column_meaning",
        "notes": "TRUE answer 96096 via COUNT(DISTINCT customer_unique_id). Agent likely answers "
                 "99441 via COUNT(DISTINCT customer_id), which is per-order, not per-person. "
                 "Structurally valid; no structural/consensus check can catch it. See docs/detectors.md.",
    },
]

# Bucket display order for reporting.
BUCKETS = ["in_scope_trap", "clean", "known_limitation"]


if __name__ == "__main__":
    by_bucket = {b: [q for q in QUESTIONS if q["bucket"] == b] for b in BUCKETS}

    print(f"Evaluation set: {len(QUESTIONS)} questions across {len(BUCKETS)} buckets\n")
    for bucket in BUCKETS:
        items = by_bucket[bucket]
        print("=" * 78)
        print(f"{bucket.upper()}  ({len(items)} question{'s' if len(items) != 1 else ''})")
        print("=" * 78)
        for q in items:
            gt = q["ground_truth"]
            gt_disp = gt if isinstance(gt, (int, float)) else f'"{str(gt)[:60]}..."' if len(str(gt)) > 60 else f'"{gt}"'
            tag = f" [{q['trap_type']}]" if q["trap_type"] else ""
            print(f"  {q['id']:<4} {q['question']}{tag}")
            print(f"       ground_truth: {gt_disp}")
        print()

    print("Counts by bucket:",
          ", ".join(f"{b}={len(by_bucket[b])}" for b in BUCKETS))
    print("Note: known_limitation questions are reported separately and are NOT counted "
          "in headline recall.")
