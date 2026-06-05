# Baseline Failures: Naive Agent (Before Trust Layer)

The failures documented here are silent failures. In each case, the SQL agent produced a query that executed without error, returned a result, and generated a confident plain-English answer — yet the answer was measurably wrong. There was no exception, no NULL, no warning from the database. The numbers looked plausible. Without a ground-truth reference query to compare against, a user would have no reason to doubt them.

All three failures were produced by the agent responding to ordinary natural-language questions with no adversarial prompting, no schema tricks, and no ambiguous wording. They arise from structural patterns that a capable LLM reliably falls into when translating English aggregation questions into SQL: joining tables for context without accounting for fan-out, and aggregating at the wrong grain because the question does not make grain explicit.

This document is the "before" baseline. The trust layer's job is to catch and abstain on exactly these cases before an answer is returned. Effectiveness will be measured as recall on this growing failure set: a trust layer that passes all three cases is no better than the naive agent.

---

## Confirmed Failures

| # | Question | Agent SQL (summary) | Agent answer | Correct answer | Error | Failure class |
|---|---|---|---|---|---|---|
| 1 | "What is the total payment value per product category?" | `SUM(payment_value)` after 4-table join: `products → order_items → orders → order_payments` | $20,308,135 total (across categories) | $16,008,872 | **+27.0%** | Fan-out double-count (Trap 1) |
| 2 | "What is the average payment value per order?" | `AVG(payment_value) FROM order_payments` | $154.10 | $160.99 | **−4.3%** | Wrong-grain AVG (Trap 3) |
| 3 | "What is the average number of items per order?" | `AVG(order_item_id) FROM order_items` | 1.198 | 1.1417 | **+4.9%** | Wrong column + wrong grain (Trap 3) |

---

## Notes on Each Failure

**Failure 1** is the most dangerous by magnitude. The agent joined `order_payments` to get category context, but `order_payments` has multiple rows per order (one per payment method or installment split). The join multiplied `order_items` rows before the SUM, inflating every category's total by a factor of ~1.27. The agent reported a $4.3M surplus that does not exist.

**Failure 2** is subtler. `AVG(payment_value)` over the `order_payments` table computes the average payment *row* value, not the average payment per *order*. Orders paid in multiple installments or with a mix of credit card and voucher contribute multiple rows, each with a smaller value, pulling the average down relative to the true order-level mean ($160.99). The correct query requires a pre-aggregation subquery: `AVG(order_total) FROM (SELECT order_id, SUM(payment_value) ... GROUP BY order_id)`.

**Failure 3** compounds two errors. `order_item_id` is a sequential position counter within an order (1 for the first item, 2 for the second, etc.), not a count of items. Averaging it produces a weighted average of item positions, not items-per-order. The correct approach is `AVG(item_count) FROM (SELECT order_id, COUNT(*) ... GROUP BY order_id)`. Neither error triggered any database warning.

---

## Growing Failure Set

This table will be extended as new failure patterns are identified during evaluation. The trust layer's recall on this set is the primary correctness metric: every row here is a case where the trust layer must issue FLAG or ABSTAIN rather than ANSWER.
