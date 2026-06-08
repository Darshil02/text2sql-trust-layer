# Trust Layer Detectors

Technical documentation for each implemented detector: approach, validation results, and known limitations. Each detector is a standalone function that accepts a SQL string and a live database connection, returns a structured result object, and must never raise an exception.

---

## F2-A / F2-B: Fan-Out Detector (`trust/sanity.py`)

Fan-out is the failure mode where a JOIN multiplies rows on the aggregate-side table before a SUM or AVG is computed, silently inflating the result. The two detectors are independent implementations of the same check, designed to be compared in eval.

### Shared foundation: `get_join_cardinality`

Both detectors call the same helper to measure fan-out potential for a given join key pair:

```
ratio = COUNT(*) / COUNT(DISTINCT key)
```

A ratio of 1.0 means the key is unique on that side (one-side). A ratio above 1.0 means multiple rows share a key value (many-side). The threshold for flagging as "many" is `ratio > 1.01` to absorb floating-point noise. The helper is schema-agnostic: it receives table and column names from the AST and queries the database dynamically with no hardcoded names.

---

### Detector A — AST + cardinality (`detect_fanout_ast`)

**Approach:** Parses the SQL with sqlglot, extracts the alias-to-table map, JOIN key pairs, and aggregated columns from the AST structure. For each join key pair, calls `get_join_cardinality` against the live database to label each table alias as one-side or many-side. Flags if any SUM or AVG column belongs to a many-side alias.

**No query re-execution.** The original SQL is never run. Detection cost is proportional to the number of join pairs × two COUNT queries per pair.

**Tradeoffs:**
- Fast and safe — no risk of running an expensive or side-effecting query
- Catches fan-out before execution, enabling pre-emptive FLAG before showing the user any result
- Requires the aggregated column to carry a table alias qualifier (`op.payment_value`) to resolve which table it comes from; unqualified column references are a blind spot
- Returns a binary `flagged` signal with a structural explanation, but no measured magnitude

---

### Detector B — Re-execution with grain-corrected baseline (`detect_fanout_reexec`)

**Approach:** Parses the SQL to identify the many-side table, its join key, and the aggregated column. Executes the original query and sums all values at the aggregate column position (summing across GROUP BY groups to get a grand total). Constructs and executes a grain-corrected baseline:

```sql
SELECT SUM(_g."<agg_col>") FROM (
  SELECT "<join_key>", SUM("<agg_col>") AS "<agg_col>"
  FROM "<many_table>" GROUP BY "<join_key>"
) AS _g
```

Pre-aggregating to join-key grain then summing yields the correct total free of fan-out. If `original_total > corrected_value × (1 + 0.005)`, the excess is fan-out inflation. Returns both values and the percentage difference.

**Tradeoffs:**
- Produces a quantified measurement (e.g., +26.9%), not just a binary signal — useful for downstream severity scoring
- Independently verifiable: the corrected baseline can be spot-checked manually
- Runs the original query, which may be expensive on large tables
- Conservative on filtered queries: if the original query has WHERE conditions that reduce the aggregate below the many-side table's clean total, the comparison correctly avoids a false flag — but this means filtered fan-out may go undetected by this detector alone (Detector A still catches it structurally)
- Wrapped in `try/except`; returns `flagged=False, reason="could not verify"` on any failure rather than crashing

---

### Validation results

**True positive — known fan-out query:**

```sql
SELECT p.product_category_name, SUM(op.payment_value)
FROM products p
JOIN order_items oi ON p.product_id = oi.product_id
JOIN orders o ON oi.order_id = o.order_id
JOIN order_payments op ON o.order_id = op.order_id
GROUP BY p.product_category_name
```

| Detector | flagged | Evidence |
|---|---|---|
| A (AST) | `true` | `order_payments` cardinality ratio ≈ 1.045 on `order_id`; `SUM(op.payment_value)` resolves to the many-side |
| B (reexec) | `true` | Original total $20,308,134.71 vs corrected baseline $16,008,872.12 — **+26.9% inflation** |

Detector B's +26.9% figure matches the manual verification (`SELECT SUM(payment_value) FROM order_payments` = $16,008,872) exactly.

**False positive checks — three correct queries, both detectors:**

| Query | A flagged | B flagged | Verdict |
|---|---|---|---|
| `SELECT SUM(payment_value) FROM order_payments` | `false` | `false` | OK — no joins; early exit at join-pair guard |
| `SELECT COUNT(DISTINCT o.order_id) FROM orders o JOIN order_payments op ON ...` | `false` | `false` | OK — DISTINCT excluded from `_agg_columns`; no SUM/AVG candidates |
| `SELECT AVG(order_total) FROM (SELECT order_id, SUM(...) ... GROUP BY order_id)` | `false` | `false` | OK — top-level FROM is a subquery; `_alias_map` returns empty; join-pair guard exits early |

**Result: 0 false positives across both detectors on all three clean queries.**

---

### Known limitations

**1. Top-level analysis only — subquery fan-out is a false-negative gap.**
Both detectors build the alias map and extract join pairs from the top-level SELECT only. Fan-out that is hidden inside a subquery or CTE is not analyzed. Example:

```sql
SELECT category, revenue
FROM (
    SELECT p.product_category_name AS category, SUM(op.payment_value) AS revenue
    FROM products p
    JOIN order_items oi ON p.product_id = oi.product_id
    JOIN order_payments op ON oi.order_id = op.order_id  -- fan-out here, inside subquery
    GROUP BY p.product_category_name
) sub
```

Both detectors see only the outer `SELECT ... FROM sub` and find no joins or aggregates at the top level, returning `flagged=False`. The magnitude of this gap will be measured in the eval harness.

**2. Unqualified column references in aggregates.**
Detector A resolves the table an aggregated column belongs to via its alias qualifier (`op.payment_value` → alias `op`). If the LLM generates `SUM(payment_value)` without a qualifier in a multi-table query, the alias resolves to an empty string and the column cannot be matched to a table. The cardinality probes will still correctly label many-side tables, but the check `t_alias in many_aliases` will fail silently. Detector B is unaffected by this since it does not rely on column-to-alias matching after the candidate-finding step.

**3. Fan-out via filtered joins.**
If the original query has WHERE conditions that reduce the many-side table's rows significantly (e.g., filtering to a single order), Detector B's `original_total` may be less than the corrected baseline and will not flag. Detector A is unaffected (it uses cardinality on the full table, not the filtered result).

**4. AVG fan-out is structurally detected but magnitude is not reported.**
Detector B computes a SUM baseline for the corrected comparison. For AVG fan-out, the inflated total and the corrected total are compared, but the percentage difference reflects a total-SUM discrepancy rather than an AVG discrepancy. The flag is still correct; the `pct_difference` field should be interpreted as "SUM inflation" not "AVG inflation" in the AVG case.

---

## F2-C / F2-D: Wrong-Grain AVG Detector (`trust/sanity.py`)

Wrong-grain AVG is the failure mode where `AVG` is computed over rows of a child-grain table when the question's entity is at a coarser parent grain. For example, averaging payment rows directly instead of first aggregating per order and then averaging the per-order totals.

### Shared foundation: `detect_child_grain`

Scans all columns of the target table (excluding the aggregated column) for FK-like relationships: a column is a FK candidate if it is non-unique in the child table (`ratio > 1.01`) and has a same-named counterpart in another table where it is unique (`ratio ≤ 1.001`, PK-like). Among all candidates, the one with the lowest fan-out ratio (most direct parent) is returned. All names are discovered dynamically from database metadata — no table or column names are hardcoded.

---

### Detector C — AST + child-grain metadata (`detect_wrong_grain_ast`)

**Approach:** Parses the SQL and checks whether `AVG` is applied directly to a column in a child-grain table (one from which `detect_child_grain` returns a parent) without a GROUP BY that pre-aggregates to the parent key. Exits early if the FROM clause is a subquery — grain handling inside a subquery is intentionally not analyzed.

**Tradeoffs:**
- Fast — no query execution; cost is `detect_child_grain` probes (O(columns × tables) COUNT queries)
- Catches the error structurally before execution, enabling pre-emptive FLAG
- Fires based on table-column structure alone; cannot distinguish "AVG of a semantically valid child column" from "AVG of a meaningless counter column" — both get flagged if the table is a child grain

---

### Detector D — Re-execution with grain-corrected AVG (`detect_wrong_grain_reexec`)

**Approach:** Identifies the child table and parent key via `detect_child_grain`, runs the original query, then constructs and runs a corrected variant:

```sql
SELECT AVG(_g."<agg_col>") FROM (
  SELECT "<parent_key>", SUM("<agg_col>") AS "<agg_col>"
  FROM "<child_table>" GROUP BY "<parent_key>"
) AS _g
```

If raw AVG and corrected AVG differ by more than 0.5% (relative), flags as wrong grain.

**Tradeoffs:**
- Produces a measured discrepancy — useful for severity scoring
- Wrapped in `try/except`; returns `flagged=False, reason="could not verify"` on failure

---

### Validation results

**True positives:**

| Query | C flagged | D: raw vs corrected | Assessment |
|---|---|---|---|
| `SELECT AVG(payment_value) FROM order_payments` | `true` | 154.10 vs 160.99 — **−4.3%** | Genuine success (see Q2 analysis below) |
| `SELECT AVG(order_item_id) FROM order_items` | `true` | 1.198 vs 1.368 — **−12.4%** | Fires for the wrong reason (see Q6 analysis below) |

**True negative:**

| Query | C flagged | D flagged | Verdict |
|---|---|---|---|
| `SELECT AVG(order_total) FROM (SELECT order_id, SUM(payment_value) AS order_total FROM order_payments GROUP BY order_id)` | `false` | `false` | OK — FROM is a subquery; early exit |

**0 false positives on the true negative.**

---

### Honest analysis of the two true positive cases

**Q2 — `AVG(payment_value) FROM order_payments`: genuine success.**

This is a clean wrong-grain error: `payment_value` is a meaningful numeric column, `order_payments` has multiple rows per order (installments, mixed payment methods), and averaging at row grain under-weights high-installment orders relative to single-payment orders. Detector C correctly identifies `order_payments.order_id` as a FK to `orders`. Detector D's corrected value (160.99) is the true per-order average and matches the manual ground truth exactly. The −4.3% discrepancy is real and structurally detectable. Both detectors work correctly here for the right reasons.

**Q6 — `AVG(order_item_id) FROM order_items`: fires, but for an unreliable reason.**

`order_item_id` is a sequential position counter (1 for the first item in an order, 2 for the second, etc.). Averaging it is meaningless regardless of grain: neither `AVG(order_item_id)` (raw) nor `AVG(SUM(order_item_id) per order)` (corrected) measures anything useful. The "corrected" value (1.368) is the average of triangular numbers (1, 3, 6, 10, ...) across order sizes — it is not a useful quantity.

Detector C fires correctly in the sense that `order_items` is genuinely a child-grain table and the query has no pre-aggregation. The structural flag is appropriate. Detector D fires because the two nonsensical values differ by 12.4%, which satisfies the tolerance check — but the 12.4% is not evidence of grain miscorrection in any meaningful sense.

**The real error in Q6 is semantic, not structural:** `order_item_id` should never be aggregated with AVG regardless of grain. A grain-corrected query is still wrong. This is exactly the class of error that the F3 LLM-as-judge check is designed to catch — it can reason about whether a column is a meaningful quantity to aggregate, which structural detection cannot.

**Implication:** Structural detection (Detectors C and D) handles true wrong-grain errors well when the aggregated column is a legitimate measure (revenue, price, weight). It will also fire on wrong-column errors like Q6, which is acceptable — flagging is correct even if the diagnosis is incomplete. But the eval should track true wrong-grain vs wrong-column cases separately, and the LLM-as-judge check should be held responsible for catching Q6 correctly.

---

### Implementation dependency note

**sqlglot 30.x: FROM clause key is `"from_"`, not `"from"`.**

In sqlglot 30.x, `Select.args` stores the FROM clause under the key `"from_"` (with a trailing underscore). All detectors in `trust/sanity.py` use `select.args.get("from_")`. If sqlglot changes this key in a future version, all FROM-clause-dependent logic will silently return `flagged=False` with reason "no FROM clause" rather than raising an exception — a silent regression. The `_alias_map` helper and the wrong-grain detector entry points are the three affected call sites. Pin the sqlglot version in `pyproject.toml` and add an AST sanity assertion to the test harness that verifies `select.args.get("from_")` returns a non-None value on a known simple query before running any eval.

---

## F3-E / F3-F: Semantic Checks (`trust/semantic.py`)

Semantic checks run last at runtime. They cost API calls and are reserved for errors with no structural signature — cases where the SQL is syntactically valid, references real columns, and produces a number, but the number is wrong because the query is semantically incoherent.

**Judge model:** `openai/gpt-oss-120b` (via Groq)
**Generator model:** `llama-3.3-70b-versatile` (via Groq)

The judge and generator are different model families from different labs (OpenAI OSS vs Meta), ensuring genuine independence of reasoning. Both run on the same Groq infrastructure — a minor caveat noted below.

The judge model is a module-level constant (`JUDGE_MODEL`) and can be swapped to `qwen/qwen3-32b` if rate limits become an issue.

---

### Detector E — LLM-as-judge alignment (`check_question_sql_alignment`)

**Approach:** The judge model is shown the question, the schema, and the generated SQL, and asked to assess whether the SQL correctly answers the question. The prompt probes four specific failure modes: wrong column, wrong grain, semantically meaningless aggregation (e.g. averaging an ID or position counter), and intent mismatch (e.g. question asked for average but query computed sum). The judge responds in structured JSON: `{"answers_question": bool, "concerns": [...], "severity": "none"|"soft"|"hard"}`. The response is parsed robustly (markdown fences stripped, fallback regex extraction). On parse failure, returns `flagged=False` rather than crashing.

**Flagging rule:** `flagged = not answers_question OR len(concerns) > 0`

**Validation result — Q6 (the structural-check blind spot):**

Query: `SELECT AVG(order_item_id) FROM order_items`
Question: "What is the average number of items per order?"

The judge returned `severity=hard` with four concerns:
- *"Aggregating surrogate key (order_item_id) instead of counting items"*
- *"Incorrect grain (no grouping by order_id)"*
- *"Semantic nonsense averaging IDs"*
- *"Does not compute average items per order"*

This is the error class that structural detection could only approximate. The F2 wrong-grain detector fired on Q6 but for an unreliable reason — it compared two nonsensical values and found them different. The LLM judge identified the real cause: `order_item_id` is a positional counter that should never be averaged, not a measurement. Structural checks cannot reason about column semantics; the judge can.

**Validation result — clean query:**

Query: `SELECT COUNT(*) FROM orders`
Question: "How many orders are there?"

Judge returned `flagged=False, severity=none`. No false positive.

---

### Detector F — Re-derivation consensus (`check_rederivation_consensus`)

**Approach:** The judge model is shown only the question and schema — never the generator's SQL. It writes its own independent SQL, which is executed against the live database. The result is compared to the original value. If the two differ by more than 0.5% (relative), the discrepancy flags the original as potentially wrong. If the judge's SQL fails to execute, the check returns `flagged=False` with reason "could not re-derive" — the original is never penalised for the judge's failure.

**Validation results:**

| Case | Original | Re-derived | Agreement | Flagged |
|---|---|---|---|---|
| "How many orders are there?" | 99441.0 | 99441.0 | 0.0% apart | `false` ✓ |
| "What is the average payment value per order?" | 154.10 | 160.99 | 4.3% apart | `true` ✓ |

For the wrong-grain case, the judge independently wrote the correct pre-aggregated query (`AVG(SUM(payment_value) per order)`) and obtained 160.99 — matching the manual ground truth exactly. The 4.3% discrepancy exposed the original's wrong-grain error without any structural analysis.

---

### Known limitations

**1. Consensus catches errors only when the judge succeeds where the generator failed.**

If both models make the same mistake, they will agree — and the agreement is a false negative. For instance, if both models generate `AVG(payment_value) FROM order_payments` when asked about average order value, the consensus check returns `agree=True, flagged=False` even though both are wrong. This is the fundamental limitation of self-consistency: it is a strong signal when models disagree, but silent when they share a systematic bias toward the same error. Consensus should be treated as a conditional signal, not a complete safety net.

**2. Provider independence is partial.**

The judge (`openai/gpt-oss-120b`) and generator (`llama-3.3-70b-versatile`) are architecturally independent — different labs, different training runs, different capability profiles. However, both are served by Groq. A Groq-wide infrastructure failure or a Groq-side prompt transformation (e.g. system prompt injection, safety filtering) would affect both models simultaneously, breaking the independence assumption at the provider level. True independence would require different inference providers for judge and generator.

---

## Integration note: wrong-column errors and the case for layered checks

When the generator produces a query that aggregates a semantically meaningless column — e.g. `AVG(order_item_id)` where `order_item_id` is a sequential position counter, not a count of items — the re-execution detector (F2-D / `detect_wrong_grain_reexec`) still fires and contributes a hard flag to the verdict. However, its reported magnitude is unreliable in this error class. Grain-correcting a meaningless column (`AVG(SUM(order_item_id) per order)`) produces a different meaningless number. The percentage difference between the two nonsensical values is not evidence of the error; it is coincidental noise. The correct ABSTAIN verdict does not rest on this signal.

The trustworthy signals for wrong-column errors are the semantic checks:

- **LLM-as-judge (F3-E):** Independently reasons about whether the aggregated column is a legitimate measure. For `AVG(order_item_id)`, it named the actual cause: surrogate key, positional counter, semantic nonsense. This is the check that caught the error correctly.
- **Consensus (F3-F):** The judge independently wrote the correct query and obtained 1.1417 (the true average items per order). The discrepancy between 1.0 and 1.1417 exposed the error — not because of grain correction, but because a competent independent model naturally avoided the wrong column.

**The correct verdict survives because the checks are independent.** The re-execution detector fired on noisy grounds; the judge and consensus checks fired on solid grounds. Aggregating their outputs with `aggregate_verdict` produces ABSTAIN regardless of which individual signal is reliable. This is the core argument for layering multiple independent checks across structural, sanity, and semantic families: no single check needs to be right for the right reason — the verdict only requires that at least one hard check fires, and two of the three did so here for sound reasons.

**On SQL non-determinism:** The generator is non-deterministic across runs. For the Q6 question ("What is the average number of items per order?"), the agent has been observed generating both `SELECT AVG(order_item_id) FROM order_items` (no GROUP BY) and `SELECT AVG(order_item_id) AS average_items_per_order FROM order_items GROUP BY order_id` (with GROUP BY). Both are wrong for the same underlying reason — wrong column — and both are caught by the semantic checks regardless of the GROUP BY variant. The trust layer is robust to this variation because it reasons about the column choice, not the query structure.

The same non-determinism applies to the join itself: for "total payment value per product category" the agent produces a fan-out inflated by 27% ($20.3M) on one run and 25% ($20.0M, routed through the category-translation table) on another — a different wrong query each time, which is itself an argument for runtime verification, since you cannot predict in advance which wrong query a given run will produce.

---

## Known limitation: semantic column-meaning errors

The evaluation harness surfaced a failure the trust layer does not catch. (This is eval case `q6` in `eval/questions.py` — "How many distinct customers are there?" — distinct from the "Q6" baseline question on average items per order referenced above; the shared number is coincidental.)

**The error.** Asked "how many distinct customers are there?", the agent generated:

```sql
SELECT COUNT(DISTINCT customer_id) FROM customers
```

This returns **99,441**. The correct answer is **96,096**, obtained via:

```sql
SELECT COUNT(DISTINCT customer_unique_id) FROM customers
```

The discrepancy is a property of the Olist schema: `customer_id` is assigned fresh for every order, so it is effectively a per-transaction identifier, while `customer_unique_id` is the stable per-person identifier. Counting distinct `customer_id` counts orders-with-a-customer, not distinct customers. The 3,345-customer gap is exactly the set of repeat buyers.

**Why no check caught it.** The query is structurally impeccable:

- **Structural (F1):** `customer_id` is a real column on a real table. No hallucination. Passes.
- **Sanity (F2):** No join, so no fan-out. No AVG/SUM at a child grain, so no wrong-grain flag. `COUNT(DISTINCT ...)` is the correct idiom for a distinctness question, so nothing structural is amiss. Passes.
- **Semantic (F3):** The independent LLM judge accepted `COUNT(DISTINCT customer_id)` as a plausible way to answer "how many distinct customers" — `customer_id` *looks* like the right column by name. Consensus did not fire either, because an independent model asked the same question tends to reach for the same plausible-looking column. Both passed.

The verdict was **ANSWER** with full confidence — a false negative.

**Why this is a boundary, not a bug.** Every check in this system reasons about *structure*: does the schema contain this name, does this join multiply rows, is this aggregate at the right grain, is this column a sensible thing to average. None of those questions can distinguish `customer_id` from `customer_unique_id`, because the distinction is not structural — it is a fact about what the data *means* in the real world. `customer_id` being per-order rather than per-person is domain knowledge encoded nowhere in the column types, cardinalities, or query shape that structure-based verification can inspect. Both columns are high-cardinality VARCHARs; both are valid arguments to `COUNT(DISTINCT)`.

This defines a boundary of the approach: **structure-based verification cannot catch errors that require real-world knowledge of what a column actually represents** — for example, an identifier that is per-transaction rather than per-entity. Catching this class would require an external signal the current system does not have: curated column-semantics metadata (a data dictionary noting that `customer_id` is per-order), entity-resolution heuristics, or a judge primed with dataset-specific documentation. Absent that, the honest behavior is to acknowledge the gap rather than to claim coverage the checks do not provide.

This is a documented limitation, not a defect in any individual detector. It marks where structural trust-checking ends and data-dictionary / domain-knowledge verification would have to begin.

---

## F2 filter checks: temporal & status (`trust/sanity.py`)

Soft advisory checks that flag a likely-missing predicate: `check_missing_temporal_filter` (question is time-scoped but the SQL has no DATE/TIMESTAMP predicate) and `check_unconstrained_status` (an aggregate over a low-cardinality status-like column with no WHERE filter). Both emit FLAG-level (soft) signals, not ABSTAIN.

Known soft false-positive (eval c2): the status check caveats a correct `SUM(payment_value)` for not constraining `payment_type`. Kept intentionally — it is a tolerable soft caveat (not a refusal) and the check catches genuine status-omission errors; tuning it to clear this case would over-fit to the eval set.
