"""Test question set with verified ground-truth answers for evaluating the SQL agent pipeline."""

QUESTIONS = [
    {
        "id": "q1",
        "question": "How many orders are there?",
        "ground_truth": 99441,
        "is_trap": False,
        "trap_type": None,
        "notes": None,
    },
    {
        "id": "q2",
        "question": "What is the total revenue by payment type?",
        "ground_truth": "credit_card ~12.54M, boleto ~2.87M, voucher ~0.38M, debit_card ~0.22M",
        "is_trap": False,
        "trap_type": None,
        "notes": "Agent handled this cleanly before — direct GROUP BY on order_payments, no joins.",
    },
    {
        "id": "q3",
        "question": "What is the average payment value per order?",
        "ground_truth": 160.99,
        "is_trap": True,
        "trap_type": "wrong_grain",
        "notes": "Agent computed 154.10 by averaging over payment rows rather than per-order totals.",
    },
    {
        "id": "q4",
        "question": "What is the average number of items per order?",
        "ground_truth": 1.1417,
        "is_trap": True,
        "trap_type": "wrong_column_grain",
        "notes": "Agent averaged order_item_id (a positional counter) instead of counting items per order.",
    },
    {
        "id": "q5",
        "question": "What is the total payment value per product category?",
        "ground_truth": "requires per-order dedup before join; naive 4-table join inflates ~27%",
        "is_trap": True,
        "trap_type": "fanout",
        "notes": "Clean total is ~16.0M; naive 4-table join inflates to ~20.3M (+27%).",
    },
    {
        "id": "q6",
        "question": "How many distinct customers are there?",
        "ground_truth": 96096,
        "is_trap": False,
        "trap_type": None,
        "notes": "Requires COUNT(DISTINCT customer_unique_id), not customer_id. Verify count when harness runs.",
    },
]
