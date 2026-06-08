"""Aggregates F1/F2/F3 check results into a confidence score and ANSWER/FLAG/ABSTAIN verdict."""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Verdict constants
# ---------------------------------------------------------------------------

ANSWER  = "ANSWER"   # high confidence — respond normally
FLAG    = "FLAG"     # soft concerns — answer with stated caveats
ABSTAIN = "ABSTAIN"  # hard failure — refuse to answer

# ---------------------------------------------------------------------------
# Penalties per flag severity
# ---------------------------------------------------------------------------

_HARD_PENALTY = 0.4
_SOFT_PENALTY = 0.1

# HARD flags (empirically CONFIRMED problems) -> ABSTAIN.
#   structural: a referenced table/column does not exist in the schema.
#   reexec:     re-execution measured actual numeric divergence from a grain-corrected
#               baseline — fan-out inflation is observed, not merely possible.
#   consensus:  an independent model re-derived a materially different answer.
_HARD_METHODS = {"structural", "reexec", "consensus"}

# SOFT flags (ADVISORY — possible but unconfirmed) -> FLAG (answer with caveat).
#   ast:      structural fan-out *suspicion* only. AST cannot distinguish a legitimate
#             1:N aggregation (e.g. revenue per seller over order_items) from real
#             fan-out without re-execution; reexec is the empirical confirmer. An AST
#             flag alone therefore warrants a caveat, not a refusal.
#   temporal: question looks time-scoped but no DATE/TIMESTAMP predicate was found.
#   status:   aggregate over a low-cardinality status column with no filter.
_SOFT_METHODS = {"ast", "temporal", "status"}

# "llm_judge" severity is carried in the check's own "severity" field


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _flag_level(check: dict) -> str | None:
    """
    Return "hard", "soft", or None (not flagged) for a single check result.

    Classification rules:
      - Not flagged               → None
      - method in _HARD_METHODS   → hard
      - method in _SOFT_METHODS   → soft
      - method == "llm_judge"     → hard if severity=="hard", else soft
      - unknown method            → soft (conservative default)
    """
    if not check.get("flagged"):
        return None

    method   = check.get("method", "")
    severity = check.get("severity", "")

    if method == "llm_judge":
        return "hard" if severity == "hard" else "soft"
    if method in _HARD_METHODS:
        return "hard"
    if method in _SOFT_METHODS:
        return "soft"
    return "soft"   # safe default for unrecognised methods


def _build_reasoning(
    verdict: str,
    hard_flags: list[dict],
    soft_flags: list[dict],
) -> str:
    def _fmt(checks: list[dict]) -> str:
        return "; ".join(f"[{c['method']}] {c['reason']}" for c in checks)

    if verdict == ANSWER:
        return "All checks passed."

    if verdict == ABSTAIN:
        parts = [f"Not answering because: {_fmt(hard_flags)}."]
        if soft_flags:
            parts.append(f"Also flagged (soft): {_fmt(soft_flags)}.")
        return " ".join(parts)

    # FLAG
    return f"Answer provided with caveats: {_fmt(soft_flags)}."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def aggregate_verdict(check_results: list[dict]) -> dict[str, Any]:
    """
    Combine all trust-layer check outputs into a single verdict.

    Each element of check_results must have at least:
      {flagged: bool, method: str, reason: str}
    and may include: severity ("hard"/"soft"), pct_difference, concerns, etc.

    Decision logic:
      - Any HARD flag  → ABSTAIN
      - Any SOFT flag  → FLAG
      - No flags       → ANSWER

    Confidence (0.0–1.0):
      Starts at 1.0; deducts _HARD_PENALTY per hard flag and _SOFT_PENALTY
      per soft flag; floors at 0.0.

    Returns:
      {verdict, confidence, reasoning, hard_flags, soft_flags}
    """
    hard_flags: list[dict] = []
    soft_flags: list[dict] = []

    for check in check_results:
        level = _flag_level(check)
        if level == "hard":
            hard_flags.append(check)
        elif level == "soft":
            soft_flags.append(check)

    # Decision
    if hard_flags:
        verdict = ABSTAIN
    elif soft_flags:
        verdict = FLAG
    else:
        verdict = ANSWER

    # Confidence
    confidence = 1.0
    confidence -= len(hard_flags) * _HARD_PENALTY
    confidence -= len(soft_flags) * _SOFT_PENALTY
    confidence = round(max(0.0, confidence), 2)

    reasoning = _build_reasoning(verdict, hard_flags, soft_flags)

    return {
        "verdict":    verdict,
        "confidence": confidence,
        "reasoning":  reasoning,
        "hard_flags": hard_flags,
        "soft_flags": soft_flags,
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    CASES = [
        # 1. All checks pass
        (
            "All checks pass",
            [
                {"flagged": False, "method": "structural", "reason": "all tables and columns exist in schema"},
                {"flagged": False, "method": "ast",        "reason": "no fan-out detected via AST + cardinality"},
                {"flagged": False, "method": "temporal",   "reason": "no temporal language detected in question"},
                {"flagged": False, "method": "status",     "reason": "no status-like columns found"},
                {"flagged": False, "method": "llm_judge",  "reason": "SQL correctly answers the question", "severity": "none"},
            ],
        ),
        # 2. One soft flag (temporal)
        (
            "One soft flag — temporal filter missing",
            [
                {"flagged": False, "method": "structural", "reason": "schema OK"},
                {"flagged": False, "method": "ast",        "reason": "no fan-out detected"},
                {
                    "flagged": True, "method": "temporal", "severity": "soft",
                    "reason": "question contains temporal language but SQL has no predicate on any DATE or TIMESTAMP column",
                },
            ],
        ),
        # 3. One hard flag (structural — hallucinated column)
        (
            "One hard flag — structural schema miss",
            [
                {
                    "flagged": True, "method": "structural",
                    "reason": "unknown column(s): ['customer_name']",
                    "missing_tables": [], "missing_columns": ["customer_name"],
                },
                {"flagged": False, "method": "ast",      "reason": "no fan-out detected"},
                {"flagged": False, "method": "temporal", "reason": "no temporal language"},
            ],
        ),
        # 4. Hard fan-out (reexec) + soft status
        (
            "Hard reexec fan-out + soft unconstrained status",
            [
                {
                    "flagged": True, "method": "reexec",
                    "reason": "original total 20,308,134.71 exceeds grain-corrected baseline 16,008,872.12 by 26.9% — fan-out inflation confirmed",
                    "original_value": 20308134.71, "corrected_value": 16008872.12, "pct_difference": 0.2686,
                },
                {
                    "flagged": True, "method": "status", "severity": "soft",
                    "reason": "aggregate on 'orders' has unconstrained status-like column(s): ['orders.order_status (8 distinct values)']",
                },
                {"flagged": False, "method": "structural", "reason": "schema OK"},
            ],
        ),
        # 5. Semantic judge severity=hard
        (
            "Semantic judge severity=hard — wrong column",
            [
                {"flagged": False, "method": "structural", "reason": "schema OK"},
                {"flagged": False, "method": "ast",        "reason": "no fan-out detected"},
                {
                    "flagged": True, "method": "llm_judge", "severity": "hard",
                    "reason": "SQL does not answer the question: Aggregating surrogate key (order_item_id) instead of counting items; Semantic nonsense averaging IDs",
                    "concerns": [
                        "Aggregating surrogate key (order_item_id) instead of counting items",
                        "Incorrect grain (no grouping by order_id)",
                        "Semantic nonsense averaging IDs",
                        "Does not compute average items per order",
                    ],
                },
            ],
        ),
    ]

    for i, (label, checks) in enumerate(CASES, 1):
        result = aggregate_verdict(checks)
        print(f"Case {i}: {label}")
        print(f"  verdict={result['verdict']}  confidence={result['confidence']}")
        print(f"  hard_flags={len(result['hard_flags'])}  soft_flags={len(result['soft_flags'])}")
        print(f"  reasoning: {result['reasoning']}")
        print()
