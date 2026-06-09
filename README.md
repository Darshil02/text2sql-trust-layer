# SQL Trust Layer

**A verification layer for LLM-generated SQL that decides when to answer, flag, or abstain — catching silent errors (fan-out double-counts, wrong-grain aggregations, hallucinated columns) that run without error but return confidently wrong numbers.**

**🔗 [Live demo](https://text2sql-trust-layer.streamlit.app/)** — watch the trust layer catch a silently-wrong SQL answer in real time.

---

## The problem

Text-to-SQL agents are good enough to deploy and not good enough to trust blindly. Published evaluations of production systems put execution accuracy on realistic, enterprise-style queries well below headline benchmark numbers — commonly in the **50–80%** range once schemas get wide and questions get compositional.

The accuracy gap is not the dangerous part. The dangerous part is *how* these systems fail: **silently**. The generated SQL is valid, it executes without error, and it returns a number. The number is just wrong. There is no exception to catch, no stack trace, no NULL — only a plausible figure that a user has no runtime way to distrust. This is the **evaluation vacuum** of deployed text-to-SQL: offline benchmarks measure average accuracy, but a deployed system gets one question at a time and has no signal for *which* answers are the wrong ones.

A concrete example from this project. Asked "total payment value per product category," a naive agent generated a four-table join and returned **$20.3M**. The correct figure is **~$16.0M** — the answer was **27% too high**, because joining the payments table (many rows per order) multiplied the item rows before the SUM. The query ran cleanly. The number looked right. It was wrong, and nothing in the pipeline knew.

This project is an attempt to give the pipeline that missing signal.

## What this does

A [LangGraph](https://github.com/langchain-ai/langgraph) text-to-SQL agent generates SQL from a natural-language question. Before the answer reaches the user, a layered trust system evaluates the query and result, and a verdict aggregator returns one of **ANSWER**, **FLAG**, or **ABSTAIN** — each with a confidence score and a plain-English reason.

The checks run in three tiers, cheapest and most certain first:

- **Structural (F1) — certain, no LLM.** Schema existence: every table and column in the SQL must exist in the live schema. Catches hallucinated tables/columns. A miss here is a hard failure.
- **Sanity (F2) — empirical.** Fan-out detection via two independent methods — an **AST heuristic** (does a SUM/AVG aggregate a column on the many-side of a 1:N join?) and **empirical re-execution** (does the total diverge from a grain-corrected baseline?). Plus wrong-grain aggregation detection (AVG over child-table rows when an entity-level average was meant) and soft filter heuristics (missing temporal filter, unconstrained status column).
- **Semantic (F3) — independent model.** An LLM-judge running on a **different model family** from the generator checks whether the SQL actually answers the question — wrong column, meaningless aggregation, intent mismatch. A consensus check has the judge independently re-derive the answer and compares.

Signals are graded by certainty. **Hard** signals (schema miss, empirically confirmed fan-out, consensus disagreement, judge "hard" severity) drive **ABSTAIN**. **Soft** signals (AST fan-out suspicion, missing-filter heuristics) drive **FLAG** — answer, but with a stated caveat. Clean queries pass to **ANSWER**.

The generator is `llama-3.3-70b-versatile`; the judge is `openai/gpt-oss-120b`. Both run on Groq — architecturally independent (different labs, different training) but, honestly, sharing one inference provider (see Limitations).

## Results

Measured on a 15-question labeled eval set (`eval/questions.py`) over the [Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) e-commerce database, split into three buckets: **in-scope traps** (errors the layer should catch), **clean** (correct queries it should pass), and **known-limitation** (a documented boundary case, reported separately).

| Metric | Value |
|---|---|
| Recall on real agent errors | **100%** (4/4 caught) |
| False refusals (hard FPR) | **0%** (0/10) |
| Binary false-positive rate | **10%** (1/10 — one soft caveat) |
| Confusion matrix | TP=4, FN=0, TN=9, FP=1 |

**Methodology, stated honestly.** The scoring oracle is whether the agent's *actual generated SQL* was correct on that run — **not** whether the question belongs to a "trap" category. This matters because the agent is non-deterministic: it sometimes avoids a trap (nothing to catch) and sometimes gets a "clean" question wrong (should be caught). Scoring against the question category instead of the realized output inflates or deflates the numbers; scoring against the realized output is the truthful measure. Descriptive/multi-row answers were confirmed by hand.

**Sample size, stated honestly.** This is a **15-question set**. The results are **directional**, not a benchmark. They demonstrate that the approach catches the specific silent-error classes it targets without refusing correct answers — on this set. They do not establish a generalization claim. A larger, multi-schema eval is the obvious next step.

## Honest limitations

This section is load-bearing. The approach has real boundaries, and pretending otherwise would defeat the point of a *trust* layer.

- **Semantic column-meaning errors are not caught.** Asked "how many distinct customers," the agent answered `COUNT(DISTINCT customer_id)` = 99,441. The correct answer is 96,096 via `customer_unique_id` — because in Olist, `customer_id` is assigned *per order*, not per person. The query is structurally flawless (real column, no join, no fan-out, correct `COUNT(DISTINCT)` idiom), and the independent judge accepted it as plausible. **Structure-based verification cannot catch errors that require real-world knowledge of what a column represents.** Crossing this boundary would need a data dictionary or entity-resolution metadata the system does not have. (Documented as eval case `k1`.)
- **COUNT fan-out (missing `DISTINCT`) rests on the semantic judge alone.** When an item-level join overcounts orders because the query used `COUNT(order_id)` instead of `COUNT(DISTINCT order_id)`, no structural detector fires — only the LLM-judge catches it. There is no redundant empirical check for this class, so it inherits the judge's reliability.
- **Soft heuristics over-fire occasionally.** The status check, for instance, caveats a correct `SUM(payment_value)` for not filtering `payment_type`. This is kept intentionally: it is a tolerable soft *caveat* (not a refusal), the check catches genuine status-omission errors, and tuning it away would over-fit to this eval set.
- **The eval is small.** 15 questions, one dataset. Directional evidence, not a generalization claim.
- **Provider independence is partial.** Generator and judge are different model families but both served by Groq; a provider-level failure would not be independent.

## Architecture

```
agent/
  graph.py            LangGraph state machine: generate -> execute -> trust layer -> explain
  nodes.py            generate SQL, execute against DuckDB, explain result
  schema_retrieval.py (placeholder for future RAG schema retrieval)
trust/
  structural.py       F1: schema-existence check
  sanity.py           F2: fan-out (AST + reexec), wrong-grain, temporal/status heuristics
  semantic.py         F3: LLM-judge alignment + consensus re-derivation (independent model)
  verdict.py          aggregates checks -> ANSWER / FLAG / ABSTAIN + confidence + reasoning
eval/
  questions.py        15-question labeled set (in_scope_trap / clean / known_limitation)
  run_eval.py         harness: runs pipeline, scores by agent_correct oracle, reports metrics
data/
  load_olist.py       loads Olist CSVs into a DuckDB database
tests/                pytest suite (structural derived-column regression tests)
docs/                 design notes: traps.md, baseline_failures.md, detectors.md
```

The trust layer's orchestration lives in `agent/graph.py` (`node_run_trust_layer`), which runs the checks in cost order, short-circuits after a structural hard failure, and applies one cross-check rule: the AST fan-out *advisory* defers to the empirical re-execution result when re-execution actually measured the query and found no inflation.

## Setup

**1. Data** — the Olist dataset is not included in the repo. Download [Brazilian E-Commerce (Olist)](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) from Kaggle and place the nine CSVs in `data/olist/`, then build the DuckDB database:

```bash
uv run python data/load_olist.py
```

**2. Dependencies** — this project uses [uv](https://github.com/astral-sh/uv):

```bash
uv sync
```

**3. API keys** — copy `.env.example` to `.env` and set `GROQ_API_KEY`. Groq powers both the SQL generator (Llama 3.3) and the independent judge (gpt-oss); no other key is required by the default pipeline.

```bash
cp .env.example .env   # then edit GROQ_API_KEY
```

**4. Run the agent** on a single question:

```bash
uv run python agent/graph.py "What is the total revenue from delivered orders?"
```

**5. Run the eval** and the tests:

```bash
uv run python eval/run_eval.py
uv run pytest
```

## About

This was built as a focused project exploring **runtime reliability for text-to-SQL** — not benchmark accuracy, but the narrower question of whether a system can know, at answer time, which of its own outputs to trust. The design decisions, the trap taxonomy, the detector validation, and the documented failure boundaries live in [`docs/`](docs/): [`traps.md`](docs/traps.md) (the silent-error specification), [`baseline_failures.md`](docs/baseline_failures.md) (measured naive-agent failures), and [`detectors.md`](docs/detectors.md) (per-detector validation and limitations).
