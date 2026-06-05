"""Individual agent nodes: generate SQL, execute against DuckDB, and explain results."""

import os
from pathlib import Path

import duckdb
from dotenv import load_dotenv
from langchain_groq import ChatGroq

load_dotenv(Path(__file__).parent.parent / ".env")

_MODEL = "llama-3.3-70b-versatile"


def _llm() -> ChatGroq:
    return ChatGroq(model=_MODEL, api_key=os.getenv("GROQ_API_KEY"))


def generate_sql(question: str, schema_text: str) -> str:
    prompt = (
        "You are a DuckDB SQL expert. Given the schema below and the user question, "
        "return ONLY a valid DuckDB SQL query. No markdown, no explanation, no code fences.\n\n"
        f"Schema:\n{schema_text}\n\n"
        f"Question: {question}\n\n"
        "SQL:"
    )
    return _llm().invoke(prompt).content.strip()


def execute_sql(sql: str, db_path: str) -> tuple[list, str | None]:
    try:
        con = duckdb.connect(db_path, read_only=True)
        rows = con.execute(sql).fetchall()
        con.close()
        return rows, None
    except Exception as exc:
        return [], str(exc)


_MAX_RESULT_ROWS = 50
_MAX_RESULT_CHARS = 4000


def _truncate_result(result: list) -> str:
    total = len(result)
    preview = result[:_MAX_RESULT_ROWS]
    label = f"showing {len(preview)} of {total} rows" if total > _MAX_RESULT_ROWS else f"{total} rows"
    text = f"[{label}]\n{preview}"
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + f"\n... (truncated at {_MAX_RESULT_CHARS} chars)"
    return text


def explain_answer(question: str, sql: str, result: list) -> str:
    result_text = _truncate_result(result)
    prompt = (
        "You are a helpful data analyst. Given the question, the SQL that was run, "
        "and the raw query result, write a clear, concise plain-English answer. "
        "State the number directly. No markdown.\n\n"
        f"Question: {question}\n"
        f"SQL: {sql}\n"
        f"Result: {result_text}\n\n"
        "Answer:"
    )
    return _llm().invoke(prompt).content.strip()
