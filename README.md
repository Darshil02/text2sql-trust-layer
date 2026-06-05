# SQL Trust Layer

A verification layer for LLM-generated SQL that decides when to answer, flag, or abstain.

## Problem

Production text-to-SQL agents generate plausible-looking SQL that executes without error and returns a confident answer — yet the answer is silently wrong. Common failure modes include fan-out double-counts (a JOIN multiplies rows before a SUM, inflating the result by 27% or more), wrong-grain aggregations (AVG computed over payment rows instead of per-order totals), and hallucinated columns or tables that don't exist in the schema. There is no runtime signal that any of these have occurred. This project adds a verification layer that runs after SQL generation and decides — with reasoning — whether to **ANSWER**, **FLAG** with caveats, or **ABSTAIN** entirely.

## How it works

A [LangGraph](https://github.com/langchain-ai/langgraph) text-to-SQL agent generates SQL from a natural-language question using a Groq-hosted Llama model. Before the answer is returned, a layered trust system runs three families of checks:

1. **Structural (F1):** Validates that every table and column in the generated SQL exists in the live schema. Hallucinated references are a hard failure.
2. **Sanity (F2):** Detects fan-out inflation (via sqlglot AST analysis of join cardinality and empirical re-execution against a grain-corrected baseline), wrong-grain aggregations (AVG over child-table rows when an entity-level average was intended), missing temporal filters, and unconstrained status columns.
3. **Semantic (F3):** An independent judge model (`openai/gpt-oss-120b`, different lab from the Llama generator) assesses whether the SQL actually answers the question — probing for wrong columns, meaningless aggregations, and intent mismatches. A consensus check asks the judge to independently re-derive the answer and compares results.

A verdict aggregator combines all check outputs into a single decision: **ANSWER** (all clear), **FLAG** (soft concerns, answer with caveats), or **ABSTAIN** (hard failure, refuse to present the number). Each verdict includes a confidence score and a plain-English reasoning string.

## Status

Under active development. Eval harness and quantitative results coming.

## Setup

**Data:** Download the [Brazilian E-Commerce (Olist) dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) from Kaggle and place the CSV files in `data/olist/`. Then run:

```bash
uv run python data/load_olist.py
```

**API keys:** Copy `.env.example` to `.env` and fill in:
- `GROQ_API_KEY` — for both the SQL generator (Llama) and the semantic judge
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — optional, not used by default

**Dependencies:**

```bash
uv sync
```

**Run the agent:**

```bash
uv run python agent/graph.py "What is the total revenue from delivered orders?"
```
