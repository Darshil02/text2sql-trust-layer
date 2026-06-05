# Silent SQL Error Patterns (Trap Specification)

SQL agents fail in a distinctive way: the query runs, returns a number, and the LLM presents it confidently — even when the number is silently wrong by 2×, 10×, or orders of magnitude. These are not syntax errors or runtime exceptions. They are semantic errors that produce plausible-looking output with no signal of failure.

This document specifies five canonical trap patterns, their Olist instantiations, and the schema-agnostic detection logic the trust layer uses to catch them. All detection operates on AST structure, row counts, column types, and column cardinality. No table names, column names, or domain constants are hardcoded anywhere.

---

## Trap 1: Fan-Out Double-Count on SUM

### Pattern
A query JOINs a fact table to a second table that has a **one-to-many relationship** with it, then SUMs a numeric column from the fact table. Each fact row is replicated once per matching row in the second table, so the SUM is inflated by the average fan-out factor.

### Why it fails silently
The JOIN completes without error. The SUM completes without error. The result is a real number — just the wrong one. There is no NULL, no exception, no warning. If the fan-out is 3×, the agent reports a revenue figure three times too large and it looks entirely plausible.

### Olist instantiation
```sql
-- Question: "What is the total revenue?"
-- Wrong query (agent forgets payments is one-to-many with orders):
SELECT SUM(oi.price)
FROM order_items oi
JOIN order_payments op ON oi.order_id = op.order_id
```
`order_payments` has multiple rows per `order_id` (one per payment method or installment). Joining on `order_id` without deduplication replicates every `order_items` row, inflating the SUM by the average number of payment rows per order (~1.04× on this dataset, but up to 29× for heavily installment-split orders).

### Schema-agnostic detection
1. **AST inspection:** Parse the query. Identify every JOIN. For each joined table, check whether the join key appears in an aggregation context (SUM, AVG) on the *other* table's columns.
2. **Cardinality probe:** For each join key, execute `SELECT COUNT(*) / COUNT(DISTINCT <key>)` on each side. If either side has a ratio > 1.0 and a SUM is being computed over the other side, flag as potential fan-out inflation.
3. **Row-count ratio check:** Compare `COUNT(*)` of the joined result to `COUNT(DISTINCT <join_key>)` in the base fact table. A ratio significantly above 1.0 confirms multiplication.

---

## Trap 2: Fan-Out Inflated COUNT (Missing DISTINCT)

### Pattern
A query JOINs a fact table to a lookup or detail table, then counts rows from the fact table without `DISTINCT`. Each fact-side entity is counted once per matching detail row instead of once per entity.

### Why it fails silently
`COUNT(*)` and `COUNT(col)` never return an error regardless of row multiplication. The result is a whole number, which feels authoritative. The agent has no reason to doubt a count that happens to be exactly 3× the correct answer.

### Olist instantiation
```sql
-- Question: "How many unique orders had at least one review?"
-- Wrong query:
SELECT COUNT(o.order_id)
FROM orders o
JOIN order_reviews r ON o.order_id = r.order_id
```
`order_reviews` can contain multiple reviews per order (review re-submissions exist in the dataset). Without `COUNT(DISTINCT o.order_id)`, orders with multiple reviews are counted multiple times.

### Schema-agnostic detection
1. **AST inspection:** Identify COUNT expressions that lack the DISTINCT quantifier. Check whether the counted column is also a join key in the same query.
2. **Cardinality probe:** On the joined result, compare `COUNT(<col>)` to `COUNT(DISTINCT <col>)`. If they differ by more than a small tolerance (e.g., > 0.1%), flag as probable overcounting.
3. **Join-side ratio:** As in Trap 1, compute `COUNT(*) / COUNT(DISTINCT <join_key>)` on the many-side table. A ratio > 1.0 combined with a non-DISTINCT COUNT on the one-side key is a direct signal.

---

## Trap 3: Aggregate at Wrong Grain (AVG Over Finer Table)

### Pattern
A question asks for the average of a quantity at entity level (e.g., per order), but the query computes AVG over a table that has multiple rows per entity (e.g., per item). The AVG is computed at item grain, not order grain — producing a quantity-weighted average when an order-level average was intended.

### Why it fails silently
AVG is a valid aggregate over any numeric column. The query produces a real number. If the true order-level average is $120 but item prices average $35, the agent returns $35 with full confidence and no indication that the grain is wrong.

### Olist instantiation
```sql
-- Question: "What is the average order value?"
-- Wrong query:
SELECT AVG(price)
FROM order_items
```
This computes the average *item* price, not the average *order* total. The correct query aggregates item prices to order level first:
```sql
SELECT AVG(order_total)
FROM (SELECT order_id, SUM(price) AS order_total FROM order_items GROUP BY order_id)
```
The wrong query returns ~$120 (item avg); the right query returns ~$154 (order avg) — a 28% difference on this dataset.

### Schema-agnostic detection
1. **AST grain analysis:** Parse the query. If AVG is applied directly to a column in a table that has a foreign key to a coarser entity (detectable by a VARCHAR ID column that also appears as a join key in other queries), and no GROUP BY or subquery pre-aggregates to that coarser grain, flag a grain mismatch.
2. **Cardinality ratio:** Compute `COUNT(*) / COUNT(DISTINCT <id_column>)` on the table being averaged. A ratio significantly above 1.0 means multiple rows exist per entity — averaging without pre-aggregation is likely wrong.
3. **Heuristic:** Any table where a numeric column is AVG'd and the table's primary-key-equivalent column has cardinality < total row count is a candidate for grain mismatch.

---

## Trap 4: Missing Temporal Filter

### Pattern
A question asks about a specific time window ("last year", "in 2017", "Q3") but the generated SQL omits the WHERE clause filtering on the timestamp column. The query runs over the entire history of the table and returns an all-time aggregate instead of the requested window.

### Why it fails silently
A full-table aggregate is never an error — it is a valid query. The number returned is real and often larger than expected, but an LLM agent will rationalize it ("business must be growing") rather than flag it. Temporal filters are purely semantic: the database has no concept of what time window the user intended.

### Olist instantiation
```sql
-- Question: "What was the total revenue in 2017?"
-- Wrong query:
SELECT SUM(oi.price)
FROM order_items oi
JOIN orders o ON oi.order_id = o.order_id
```
The filter `WHERE YEAR(o.order_purchase_timestamp) = 2017` is absent. The query returns the all-time total (~$13.6M) instead of the 2017 total (~$6.9M).

### Schema-agnostic detection
1. **AST inspection:** Parse the query for columns with TIMESTAMP or DATE types (inferrable from schema metadata). Check whether any such column appears in a WHERE clause, a HAVING clause, or a JOIN condition that is equivalent to a range filter. If none is present and the query contains an aggregate (SUM, COUNT, AVG), flag as temporally unconstrained.
2. **Question–query alignment (F3):** If the original natural-language question contains temporal language (year, month, quarter, "last N", "since", "between"), verify that a corresponding filter exists in the AST. Absence is a strong signal of hallucination.
3. **Result-magnitude check:** Compare the returned aggregate to a sampled single-period baseline. An all-time figure will typically be a round multiple of a per-period figure; large round multiples are suspicious.

---

## Trap 5: Unconstrained Status Column

### Pattern
A question implicitly asks about a subset of rows defined by a categorical status column (e.g., completed orders, paid invoices, active users), but the SQL omits the WHERE filter on that column. The query aggregates over all statuses including cancelled, pending, returned, and fraudulent rows — silently inflating or corrupting the result.

### Why it fails silently
The status column is just a VARCHAR. Nothing in the database enforces that "completed" rows should be the only ones counted for revenue. The query executes cleanly over all rows and returns a larger, wrong number. A user who does not know the status distribution has no way to spot the error.

### Olist instantiation
```sql
-- Question: "How many orders were delivered?"
-- Wrong query:
SELECT COUNT(*) FROM orders
```
`orders.order_status` takes values: `delivered`, `shipped`, `canceled`, `unavailable`, `invoiced`, `processing`, `created`, `approved`. The unfiltered count returns 99,441; the delivered count is 96,478. More critically, for revenue questions, including `canceled` orders inflates the figure by the full value of all cancellations.

### Schema-agnostic detection
1. **AST inspection:** Identify VARCHAR columns in the queried tables whose cardinality is low (e.g., fewer than 20 distinct values) relative to total row count — these are likely categorical status or type enumerators. Check whether any such column appears in a WHERE predicate. If an aggregate is computed without filtering on any low-cardinality VARCHAR column, flag as potentially unconstrained on status.
2. **Cardinality probe:** Execute `SELECT <col>, COUNT(*) FROM <table> GROUP BY <col> ORDER BY COUNT(*) DESC LIMIT 20` on each low-cardinality VARCHAR column. If one value dominates (e.g., 97% of rows are "delivered"), then queries that omit the filter over-count by the full tail of non-dominant values.
3. **Question–query alignment (F3):** If the natural-language question contains a status qualifier ("delivered", "completed", "paid"), verify the AST contains a corresponding equality predicate. Absence is a near-certain hallucination signal.

---

## Generality Note

Every detection mechanism above is expressed in terms of:
- **AST structure** — JOIN topology, presence/absence of DISTINCT, WHERE predicates, aggregation expressions
- **Row counts and ratios** — computed dynamically against the live database at query time
- **Column types** — TIMESTAMP/DATE for temporal checks, BIGINT/DOUBLE for numeric aggregates
- **Column cardinality** — low-cardinality VARCHAR as a proxy for categorical status; high-ratio `COUNT(*) / COUNT(DISTINCT key)` as a proxy for fan-out

No table names, column names, domain values, or numeric thresholds are hardcoded. The trust layer will be validated against datasets entirely unrelated to Olist — including at least one financial dataset and one healthcare schema — to confirm that detection generalises across domains without retraining or reconfiguration.
