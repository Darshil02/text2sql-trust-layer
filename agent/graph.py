"""LangGraph state machine: generate -> execute -> trust_layer -> explain."""

import sys
from pathlib import Path
from typing import Optional

import duckdb
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.nodes import execute_sql, explain_answer, generate_sql
from trust.sanity import (
    check_missing_temporal_filter,
    check_unconstrained_status,
    detect_fanout_ast,
    detect_fanout_reexec,
    detect_wrong_grain_ast,
    detect_wrong_grain_reexec,
)
from trust.semantic import check_question_sql_alignment, check_rederivation_consensus
from trust.structural import check_schema_exists
from trust.verdict import ABSTAIN, ANSWER, FLAG, aggregate_verdict

DB_PATH = str(Path(__file__).parent.parent / "data" / "olist.duckdb")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    question:     str
    schema_text:  str
    sql:          str
    result:       list
    answer:       str
    error:        Optional[str]
    trust_checks: list
    verdict:      str
    confidence:   float
    reasoning:    str


# ---------------------------------------------------------------------------
# Schema loader
# ---------------------------------------------------------------------------

def load_schema(db_path: str = DB_PATH) -> str:
    con = duckdb.connect(db_path, read_only=True)
    tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
    parts = []
    for table in tables:
        cols = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        col_defs = ", ".join(f"{row[1]} {row[2]}" for row in cols)
        parts.append(f"{table}({col_defs})")
    con.close()
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def node_generate_sql(state: AgentState) -> dict:
    sql = generate_sql(state["question"], state["schema_text"])
    return {"sql": sql}


def node_execute_sql(state: AgentState) -> dict:
    rows, error = execute_sql(state["sql"], DB_PATH)
    return {"result": rows, "error": error}


def node_run_trust_layer(state: AgentState) -> dict:
    """
    Run all trust checks in cost order (cheap/certain first, expensive last),
    short-circuiting after a structural hard failure.

    Order:
      1. F1 structural — schema existence (hard; skip rest if flagged)
      2. F2 sanity     — fan-out A+B, wrong-grain A+B, temporal, status
      3. F3 semantic   — alignment (llm_judge), consensus
    """
    sql          = state["sql"]
    question     = state["question"]
    schema_text  = state["schema_text"]
    result       = state.get("result") or []

    con = duckdb.connect(DB_PATH, read_only=True)
    checks: list[dict] = []

    try:
        # ── F1: structural ──────────────────────────────────────────────────
        s_check = check_schema_exists(sql, con)
        checks.append(s_check)

        structural_hard = s_check.get("flagged") and s_check.get("method") == "structural"

        if not structural_hard:
            # ── F2: sanity ─────────────────────────────────────────────────
            checks.append(detect_fanout_ast(sql, con))
            checks.append(detect_fanout_reexec(sql, con))
            checks.append(detect_wrong_grain_ast(sql, con))
            checks.append(detect_wrong_grain_reexec(sql, con))
            checks.append(check_missing_temporal_filter(question, sql, con))
            checks.append(check_unconstrained_status(question, sql, con))

            # ── F3: semantic ───────────────────────────────────────────────
            checks.append(check_question_sql_alignment(question, sql, schema_text))
            if result:
                checks.append(
                    check_rederivation_consensus(question, schema_text, result, con)
                )
    finally:
        con.close()

    v = aggregate_verdict(checks)
    return {
        "trust_checks": checks,
        "verdict":      v["verdict"],
        "confidence":   v["confidence"],
        "reasoning":    v["reasoning"],
    }


def node_explain_answer(state: AgentState) -> dict:
    """
    Shape the user-facing answer based on the trust verdict:
      ABSTAIN → refuse to present the number; surface the reasoning.
      FLAG    → answer with stated caveats.
      ANSWER  → answer normally.
    """
    verdict  = state.get("verdict", ANSWER)
    reasoning = state.get("reasoning", "")

    if verdict == ABSTAIN:
        answer = (
            f"This question cannot be answered with confidence.\n\n"
            f"{reasoning}"
        )
        return {"answer": answer}

    base = explain_answer(state["question"], state["sql"], state["result"])

    if verdict == FLAG:
        answer = f"{base}\n\nCaveat: {reasoning}"
        return {"answer": answer}

    return {"answer": base}


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------

def build_graph():
    g = StateGraph(AgentState)
    g.add_node("generate_sql",    node_generate_sql)
    g.add_node("execute_sql",     node_execute_sql)
    g.add_node("run_trust_layer", node_run_trust_layer)
    g.add_node("explain_answer",  node_explain_answer)
    g.set_entry_point("generate_sql")
    g.add_edge("generate_sql",    "execute_sql")
    g.add_edge("execute_sql",     "run_trust_layer")
    g.add_edge("run_trust_layer", "explain_answer")
    g.add_edge("explain_answer",  END)
    return g.compile()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    question    = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "how many orders are there?"
    schema_text = load_schema()
    app         = build_graph()

    state = app.invoke({
        "question":     question,
        "schema_text":  schema_text,
        "sql":          "",
        "result":       [],
        "answer":       "",
        "error":        None,
        "trust_checks": [],
        "verdict":      ANSWER,
        "confidence":   1.0,
        "reasoning":    "",
    })

    rows    = state["result"]
    preview = rows[:10]
    suffix  = f"  ... ({len(rows)} rows total)" if len(rows) > 10 else ""

    print(f"\nSQL:\n{state['sql']}")
    print(f"\nResult:\n{preview}{suffix}")
    print(f"\nVERDICT:    {state['verdict']}")
    print(f"Confidence: {state['confidence']}")
    print(f"Reasoning:  {state['reasoning']}")
    if state.get("error"):
        print(f"\nSQL Error:\n{state['error']}")
    print(f"\nAnswer:\n{state['answer']}")
