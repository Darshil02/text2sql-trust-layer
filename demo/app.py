"""Streamlit demo UI for TrustSQL: a split-screen 'with vs without trust layer' comparison.

Reads demo_cache.json (real, cached pipeline outputs — no live API calls) and renders
each curated question as naive-agent answer vs trust-layer verdict. Self-contained for
deployment: see demo/requirements.txt for the minimal dependency set.
"""

import json
from pathlib import Path

import pandas as pd
import sqlglot
import streamlit as st

CACHE = Path(__file__).parent / "demo_cache.json"
REPO_URL = "https://github.com/Darshil02/text2sql-trust-layer"

# Verdict palette: (text color, background color)
VERDICT_COLORS = {
    "ANSWER":  ("#0f5132", "#d1e7dd"),
    "FLAG":    ("#664d03", "#fff3cd"),
    "ABSTAIN": ("#842029", "#f8d7da"),
}

# Short selector labels: id -> (question, what it demonstrates)
DEMO_LABELS = {
    "fanout":           ("Total payment value per category", "fan-out double-count"),
    "wrong_column":     ("Average items per order", "wrong column"),
    "clean_count":      ("How many orders", "clean — passes"),
    "clean_paytype":    ("Revenue by payment type", "clean — soft caveat"),
    "known_limitation": ("How many distinct customers", "known limitation — the honest miss"),
}


@st.cache_data
def load_cache():
    entries = json.loads(CACHE.read_text())
    return {e["id"]: e for e in entries}


def verdict_badge(verdict: str) -> str:
    fg, bg = VERDICT_COLORS.get(verdict, ("#333", "#eee"))
    return (
        f'<span style="background:{bg};color:{fg};padding:8px 22px;border-radius:10px;'
        f'font-weight:800;font-size:1.6rem;letter-spacing:0.5px;">{verdict}</span>'
    )


def pretty_sql(sql: str) -> str:
    """Format SQL multi-line so the full query is readable and clearly complete."""
    try:
        return sqlglot.parse_one(sql, dialect="duckdb").sql(dialect="duckdb", pretty=True)
    except Exception:
        return sql


def output_columns(sql: str, ncols: int) -> list[str]:
    """Recover SELECT output column names from the SQL; fall back to generic labels."""
    try:
        sel = sqlglot.parse_one(sql, dialect="duckdb")
        names = [e.alias_or_name or f"col_{i+1}" for i, e in enumerate(sel.selects)]
        if len(names) == ncols and all(names):
            return names
    except Exception:
        pass
    return [f"col_{i+1}" for i in range(ncols)]


def confidence_label(conf: float) -> str:
    q = "high" if conf >= 0.8 else "medium" if conf >= 0.4 else "low"
    return f"{conf} ({q})"


def check_tags(checks: list[str]) -> str:
    if not checks:
        return '<span style="color:#6a737d;font-style:italic;">no checks fired</span>'
    pills = "".join(
        f'<span style="background:#eef1f4;color:#24292f;padding:3px 10px;border-radius:12px;'
        f'font-size:0.8rem;margin-right:6px;font-family:monospace;">{c}</span>'
        for c in checks
    )
    return pills


def main():
    st.set_page_config(page_title="text2sql-trust-layer — interactive demo", layout="wide")
    data = load_cache()

    # ---- Header ----------------------------------------------------------- #
    st.title("text2sql-trust-layer — interactive demo")
    st.markdown(
        "##### Watch an LLM SQL agent get caught producing confident, silently-wrong answers."
    )
    st.markdown(
        '<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;'
        'padding:10px 14px;font-size:0.82rem;color:#57606a;line-height:1.45;">'
        "<b>About these examples:</b> real outputs from the pipeline on representative runs. "
        "The SQL agent is non-deterministic — it produces a different (often differently-wrong) "
        "query each run — so the examples shown are characteristic real runs, not live generation. "
        "This non-determinism is itself why a runtime trust layer is needed: you can't predict "
        "which wrong query you'll get."
        "</div>",
        unsafe_allow_html=True,
    )
    st.write("")

    # ---- Question selector ----------------------------------------------- #
    ids = list(DEMO_LABELS.keys())

    def fmt(i):
        q, demo = DEMO_LABELS[i]
        return f"{q}  →  ({demo})"

    selected = st.selectbox("Choose a question", ids, index=0, format_func=fmt)
    entry = data[selected]
    is_silent_error = "correct_answer" in entry
    caught = entry["verdict"] != "ANSWER"

    st.markdown(f"**Question:** *{entry['question']}*")
    st.write("")

    # ---- Split screen ----------------------------------------------------- #
    left, right = st.columns(2, gap="large")

    with left:
        st.markdown("#### 🤖 Without trust layer (naive agent)")
        st.caption("Generated SQL")
        st.code(pretty_sql(entry["sql"]), language="sql")
        st.caption("Answer presented to the user")
        st.markdown(
            f'<div style="background:#ffffff;border:1px solid #d0d7de;border-radius:8px;'
            f'padding:16px 18px;font-size:1.05rem;color:#1f2328;">{entry["naive_answer"]}</div>',
            unsafe_allow_html=True,
        )
        with st.expander(f"View raw result ({entry['result_rows']} row(s))", expanded=False):
            preview = entry["result_preview"]
            if preview and len(preview[0]) == 1:
                # Single scalar — show it plainly.
                val = preview[0][0]
                disp = f"{val:,}" if isinstance(val, int) else (
                    f"{val:,.4f}" if isinstance(val, float) else str(val))
                st.markdown(f"Result: **{disp}**")
            elif preview:
                cols = output_columns(entry["sql"], len(preview[0]))
                st.dataframe(pd.DataFrame(preview, columns=cols),
                             hide_index=True, width="stretch")
                if entry["result_rows"] > len(preview):
                    st.caption(f"showing first {len(preview)} of {entry['result_rows']} rows")
            else:
                st.caption("query returned no rows")

    with right:
        st.markdown("#### 🛡️ With trust layer")
        st.markdown(verdict_badge(entry["verdict"]), unsafe_allow_html=True)
        st.write("")
        st.markdown(f"**Confidence in answer:** `{confidence_label(entry['confidence'])}`")
        st.markdown("**Checks fired:** " + check_tags(entry["checks_fired"]), unsafe_allow_html=True)
        st.caption("Reasoning")
        st.markdown(
            f'<div style="background:#f6f8fa;border-left:4px solid #57606a;border-radius:4px;'
            f'padding:12px 14px;font-size:0.9rem;color:#24292f;line-height:1.5;">'
            f'{entry["reasoning"]}</div>',
            unsafe_allow_html=True,
        )

        # ---- Silent-error reveal — the payoff ---------------------------- #
        if is_silent_error and caught:
            st.write("")
            st.markdown(
                f'<div style="background:#fff1f0;border:2px solid #cf222e;border-radius:10px;'
                f'padding:16px 18px;margin-top:6px;">'
                f'<div style="color:#cf222e;font-weight:800;font-size:1.05rem;margin-bottom:8px;">'
                f'⚠ SILENT ERROR — caught before reaching the user</div>'
                f'<div style="font-size:1.0rem;color:#1f2328;line-height:1.55;">'
                f'{entry["error_magnitude"]}</div>'
                f'<div style="margin-top:10px;font-size:0.95rem;color:#1f2328;">'
                f'<b>Correct answer:</b> {entry["correct_answer"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Known limitation — the honest miss -------------------------- #
        if is_silent_error and not caught:
            st.write("")
            st.markdown(
                f'<div style="background:#fff8e6;border:2px dashed #9a6700;border-radius:10px;'
                f'padding:16px 18px;margin-top:6px;">'
                f'<div style="color:#9a6700;font-weight:800;font-size:1.05rem;margin-bottom:8px;">'
                f'🔍 KNOWN LIMITATION — the trust layer did NOT catch this</div>'
                f'<div style="font-size:1.0rem;color:#1f2328;line-height:1.55;">'
                f'{entry["error_magnitude"]}</div>'
                f'<div style="margin-top:10px;font-size:0.95rem;color:#1f2328;">'
                f'<b>Correct answer:</b> {entry["correct_answer"]}</div>'
                f'<div style="margin-top:10px;font-size:0.86rem;color:#57606a;line-height:1.5;">'
                f'This is a semantic column-meaning error: <code>customer_id</code> is a real column '
                f'and the query is structurally valid, so structure-based checks (schema, fan-out, '
                f'grain) all correctly pass. Catching it would require knowing what the column '
                f'<i>means</i> in the real world — a documented boundary of the approach, shown here '
                f'on purpose.</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ---- Footer ----------------------------------------------------------- #
    st.write("")
    st.divider()
    st.markdown(
        f'<div style="font-size:0.85rem;color:#57606a;">'
        f'<a href="{REPO_URL}" target="_blank">{REPO_URL}</a><br>'
        f'Built with LangGraph + DuckDB + Groq. Trust layer: structural, sanity '
        f'(fan-out / wrong-grain), and semantic (independent LLM-judge) checks. '
        f'Full methodology and limitations in the repo.'
        f'</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
