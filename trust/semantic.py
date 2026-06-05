"""F3 semantic checks: re-derivation consensus and LLM-as-judge alignment."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_groq import ChatGroq

load_dotenv(Path(__file__).parent.parent / ".env")

# Generator: llama-3.3-70b-versatile (defined in agent/nodes.py)
# Judge must differ — swap to "qwen/qwen3-32b" if rate limits become an issue
JUDGE_MODEL = "openai/gpt-oss-120b"

_CONSENSUS_TOLERANCE = 0.005  # 0.5% relative difference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _judge_llm() -> ChatGroq:
    return ChatGroq(model=JUDGE_MODEL, api_key=os.getenv("GROQ_API_KEY"))


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```(?:json|sql)?\s*\n?", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text.strip())
    return text.strip()


def _parse_judge_json(text: str) -> dict | None:
    """Parse judge JSON, handling markdown fences and leading prose."""
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def _extract_scalar(result: Any) -> float | None:
    """Pull a single numeric value from a DB result row-list or raw scalar."""
    if isinstance(result, (int, float)):
        return float(result)
    if isinstance(result, (list, tuple)) and result:
        first = result[0]
        if isinstance(first, (int, float)):
            return float(first)
        if isinstance(first, (list, tuple)) and first:
            val = first[0]
            if isinstance(val, (int, float)):
                return float(val)
    return None


def _numerics_agree(a: float, b: float, tol: float = _CONSENSUS_TOLERANCE) -> bool:
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom <= tol


def load_schema_text(con) -> str:
    """Build a compact schema description from a live DuckDB connection."""
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    parts = []
    for table in tables:
        cols = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        col_defs = ", ".join(f"{r[1]} {r[2]}" for r in cols)
        parts.append(f"{table}({col_defs})")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# F3-A: LLM-as-judge alignment check
# ---------------------------------------------------------------------------

_ALIGNMENT_PROMPT = """\
You are a SQL correctness auditor. Given a user question, a database schema, \
and a generated SQL query, assess whether the SQL actually answers the question.

Probe specifically:
1. Does the SQL aggregate the RIGHT column? Or is it using a surrogate key, \
position counter, or ID that should never be averaged or summed?
2. Is the aggregate at the RIGHT grain (e.g. per-order average vs per-row average)?
3. Does aggregating this specific column make semantic sense for the question \
(e.g. averaging an item-position counter is meaningless)?
4. Does the SQL match the question's INTENT (e.g. question asks for average \
but query computes sum, or vice versa)?

You MUST respond with ONLY valid JSON — no preamble, no markdown fences:
{{"answers_question": <true|false>, "concerns": [<short strings>], \
"severity": "<none|soft|hard>"}}

Schema:
{schema_text}

Question: {question}

SQL:
{sql}"""


def check_question_sql_alignment(
    question: str,
    sql: str,
    schema_text: str,
) -> dict[str, Any]:
    """
    Use the independent judge model to assess whether the SQL correctly answers
    the question. Specifically probes for wrong column, wrong grain, meaningless
    aggregation, and intent mismatch.

    Returns: {flagged, reason, concerns, severity, method="llm_judge"}
    flagged = True when answers_question=False OR concerns is non-empty.
    """
    prompt = _ALIGNMENT_PROMPT.format(
        schema_text=schema_text,
        question=question,
        sql=sql,
    )

    try:
        response = _judge_llm().invoke(prompt).content
    except Exception as exc:
        return {
            "flagged": False,
            "reason": f"judge unavailable: {exc}",
            "concerns": [],
            "severity": "none",
            "method": "llm_judge",
        }

    parsed = _parse_judge_json(response)
    if parsed is None:
        return {
            "flagged": False,
            "reason": f"judge response unparseable — raw: {response[:200]}",
            "concerns": [],
            "severity": "none",
            "method": "llm_judge",
        }

    answers = bool(parsed.get("answers_question", True))
    concerns: list[str] = parsed.get("concerns", [])
    severity: str = parsed.get("severity", "none")

    flagged = not answers or len(concerns) > 0

    if not flagged:
        reason = "SQL correctly answers the question"
    elif not answers:
        reason = (
            f"SQL does not answer the question: {'; '.join(concerns)}"
            if concerns else "SQL does not answer the question"
        )
    else:
        reason = f"concerns raised: {'; '.join(concerns)}"

    return {
        "flagged": flagged,
        "reason": reason,
        "concerns": concerns,
        "severity": severity,
        "method": "llm_judge",
    }


# ---------------------------------------------------------------------------
# F3-B: Re-derivation consensus check
# ---------------------------------------------------------------------------

_REDERIVE_PROMPT = """\
You are a DuckDB SQL expert. Write a single SQL query to answer the question \
below using the provided schema. Return ONLY valid DuckDB SQL — no markdown, \
no explanation, no code fences.

Schema:
{schema_text}

Question: {question}

SQL:"""


def check_rederivation_consensus(
    question: str,
    schema_text: str,
    original_value: Any,
    con,
) -> dict[str, Any]:
    """
    Ask the judge model to independently write its own SQL for the question
    (without seeing the generator's SQL). Execute it and compare to original_value.

    Agreement within _CONSENSUS_TOLERANCE → consensus high, no flag.
    Disagreement → flagged, indicating the original may be wrong.

    On any judge execution failure: flagged=False, reason="could not re-derive" —
    never penalises the original for the judge's failure.

    Returns: {flagged, reason, original_value, rederived_value, agree, method="consensus"}
    """
    prompt = _REDERIVE_PROMPT.format(schema_text=schema_text, question=question)

    try:
        raw_sql = _strip_fences(_judge_llm().invoke(prompt).content)
    except Exception as exc:
        return {
            "flagged": False,
            "reason": f"could not re-derive: LLM error — {exc}",
            "original_value": None,
            "rederived_value": None,
            "agree": False,
            "method": "consensus",
        }

    try:
        rows = con.execute(raw_sql).fetchall()
    except Exception as exc:
        return {
            "flagged": False,
            "reason": f"could not re-derive: SQL error — {exc}",
            "original_value": None,
            "rederived_value": None,
            "agree": False,
            "method": "consensus",
        }

    orig_scalar = _extract_scalar(original_value)
    redv_scalar = _extract_scalar(rows)

    # Non-scalar fallback: compare row counts
    if orig_scalar is None or redv_scalar is None:
        orig_len = len(original_value) if isinstance(original_value, list) else 1
        redv_len = len(rows)
        agree = orig_len == redv_len
        return {
            "flagged": not agree,
            "reason": (
                f"row counts differ ({orig_len} vs {redv_len})"
                if not agree else
                "result shapes agree (non-scalar)"
            ),
            "original_value": orig_len,
            "rederived_value": redv_len,
            "agree": agree,
            "method": "consensus",
        }

    agree = _numerics_agree(orig_scalar, redv_scalar)
    pct = abs(orig_scalar - redv_scalar) / max(abs(orig_scalar), abs(redv_scalar), 1e-9)

    return {
        "flagged": not agree,
        "reason": (
            f"values differ: original {orig_scalar:.4f} vs re-derived {redv_scalar:.4f} "
            f"({pct:.1%} apart) — consensus low"
            if not agree else
            f"values agree: original {orig_scalar:.4f} vs re-derived {redv_scalar:.4f} "
            f"({pct:.1%} apart)"
        ),
        "original_value": round(orig_scalar, 4),
        "rederived_value": round(redv_scalar, 4),
        "agree": agree,
        "method": "consensus",
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import duckdb

    DB_PATH = str(Path(__file__).parent.parent / "data" / "olist.duckdb")
    con = duckdb.connect(DB_PATH, read_only=True)
    schema = load_schema_text(con)

    print(f"Judge model: {JUDGE_MODEL}\n")

    ALIGNMENT_CASES = [
        (
            "SHOULD FLAG — wrong column (semantic killer Q6)",
            "What is the average number of items per order?",
            "SELECT AVG(order_item_id) FROM order_items",
            True,
        ),
        (
            "SHOULD PASS — correct query",
            "How many orders are there?",
            "SELECT COUNT(*) FROM orders",
            False,
        ),
    ]

    print("=" * 60)
    print("F3-A: LLM-AS-JUDGE ALIGNMENT")
    print("=" * 60)
    for label, question, sql, expect_flag in ALIGNMENT_CASES:
        result = check_question_sql_alignment(question, sql, schema)
        ok = result["flagged"] == expect_flag
        verdict = "OK" if ok else ("FALSE POSITIVE" if result["flagged"] else "FALSE NEGATIVE")
        print(f"\n  [{verdict}] {label}")
        print(f"    flagged={result['flagged']}  severity={result['severity']}")
        print(f"    reason={result['reason']}")
        for c in result["concerns"]:
            print(f"      - {c}")

    CONSENSUS_CASES = [
        (
            "SHOULD AGREE — correct count",
            "How many orders are there?",
            99441,
            False,
        ),
        (
            "SHOULD DISAGREE — original is wrong-grain (agent 154.10, correct ~160.99)",
            "What is the average payment value per order?",
            154.10,
            True,
        ),
    ]

    print()
    print("=" * 60)
    print("F3-B: RE-DERIVATION CONSENSUS")
    print("=" * 60)
    for label, question, original_value, expect_flag in CONSENSUS_CASES:
        result = check_rederivation_consensus(question, schema, original_value, con)
        ok = result["flagged"] == expect_flag
        verdict = "OK" if ok else ("FALSE POSITIVE" if result["flagged"] else "MISSED — consensus failed to catch")
        print(f"\n  [{verdict}] {label}")
        print(f"    flagged={result['flagged']}  agree={result['agree']}")
        print(f"    original={result['original_value']}  rederived={result['rederived_value']}")
        print(f"    reason={result['reason']}")

    con.close()
