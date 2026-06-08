"""Runs the evaluation harness over the question set and computes accuracy and trust metrics."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.graph import ANSWER, build_graph, load_schema  # noqa: E402
from eval.questions import QUESTIONS  # noqa: E402

# Relative tolerance for numeric ground-truth comparison.
# Kept tight enough that the q3 wrong-grain error (154.10 vs 160.99 = 4.3%) is NOT a match.
REL_TOL = 0.02


def extract_scalar(result):
    """Pull a single numeric value from a DB result row-list, or None."""
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, (list, tuple)) and first:
            v = first[0]
            if isinstance(v, (int, float)):
                return float(v)
        elif isinstance(first, (int, float)):
            return float(first)
    return None


def numeric_match(agent_val, gt, rel_tol=REL_TOL):
    if agent_val is None:
        return False
    denom = max(abs(float(gt)), 1e-9)
    return abs(agent_val - float(gt)) / denom <= rel_tol


def classify(agent_correct, trust_caught):
    """Confusion matrix. agent_correct=None means manual review needed."""
    if agent_correct is None:
        return "MANUAL"
    if not agent_correct and trust_caught:
        return "TP"   # wrong + caught
    if not agent_correct and not trust_caught:
        return "FN"   # wrong + missed
    if agent_correct and not trust_caught:
        return "TN"   # right + passed
    return "FP"       # right + flagged


def run():
    schema_text = load_schema()
    app = build_graph()
    rows_out = []

    for q in QUESTIONS:
        state = app.invoke({
            "question":     q["question"],
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

        sql        = state["sql"]
        result     = state["result"]
        verdict    = state["verdict"]
        confidence = state["confidence"]
        checks     = state.get("trust_checks", [])
        error      = state.get("error")

        gt = q["ground_truth"]
        agent_val = extract_scalar(result)

        if isinstance(gt, (int, float)):
            agent_correct = numeric_match(agent_val, gt)
            agent_disp = "yes" if agent_correct else "no"
        else:
            agent_correct = None          # descriptive → manual
            agent_disp = "manual"

        trust_caught = verdict != ANSWER
        cls = classify(agent_correct, trust_caught)
        fired = sorted({c["method"] for c in checks if c.get("flagged")})

        rows_out.append({
            "id": q["id"],
            "is_trap": q["is_trap"],
            "trap_type": q["trap_type"],
            "ground_truth": gt,
            "agent_val": agent_val,
            "agent_correct": agent_disp,
            "verdict": verdict,
            "confidence": confidence,
            "classification": cls,
            "fired": fired,
            "sql": sql,
            "n_rows": len(result) if isinstance(result, list) else 1,
            "error": error,
        })

    return rows_out


def report(rows_out):
    # ---- Per-question detail ----
    print("\n" + "=" * 78)
    print("PER-QUESTION DETAIL")
    print("=" * 78)
    for r in rows_out:
        print(f"\n[{r['id']}] trap={r['is_trap']} ({r['trap_type'] or 'none'})")
        print(f"  SQL: {' '.join(r['sql'].split())[:100]}")
        print(f"  ground_truth: {r['ground_truth']}")
        print(f"  agent_value:  {r['agent_val']}  ({r['n_rows']} row(s))")
        if r["error"]:
            print(f"  SQL ERROR: {r['error']}")
        print(f"  verdict: {r['verdict']} (conf {r['confidence']})  |  agent_correct: {r['agent_correct']}")
        print(f"  checks fired: {r['fired'] or '(none)'}")
        print(f"  >> classification: {r['classification']}")

    # ---- Summary table ----
    print("\n" + "=" * 78)
    print("SUMMARY TABLE")
    print("=" * 78)
    hdr = f"{'id':<4} {'is_trap':<8} {'agent_correct':<14} {'verdict':<9} {'class':<7} fired"
    print(hdr)
    print("-" * 78)
    for r in rows_out:
        print(
            f"{r['id']:<4} {str(r['is_trap']):<8} {r['agent_correct']:<14} "
            f"{r['verdict']:<9} {r['classification']:<7} {','.join(r['fired'])}"
        )

    # ---- Metrics ----
    counts = {"TP": 0, "FN": 0, "TN": 0, "FP": 0, "MANUAL": 0}
    for r in rows_out:
        counts[r["classification"]] += 1

    tp, fn, tn, fp = counts["TP"], counts["FN"], counts["TN"], counts["FP"]
    auto_total = tp + fn + tn + fp
    agent_correct_n = tn + fp                       # agent was right
    agent_acc = agent_correct_n / auto_total if auto_total else 0.0
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    fpr = fp / (fp + tn) if (fp + tn) else float("nan")

    print("\n" + "=" * 78)
    print("SUMMARY METRICS")
    print("=" * 78)
    print(f"  Class counts: TP={tp}  FN={fn}  TN={tn}  FP={fp}  MANUAL={counts['MANUAL']}")
    print(f"  Auto-verifiable questions: {auto_total}  (manual review: {counts['MANUAL']})")
    print(f"  Agent accuracy (auto):     {agent_acc:.0%}  ({agent_correct_n}/{auto_total})")
    print(f"  Trust-layer recall:        {recall:.0%}  (TP/(TP+FN) = {tp}/{tp + fn})"
          if (tp + fn) else "  Trust-layer recall:        n/a (no positive cases)")
    print(f"  False-positive rate:       {fpr:.0%}  (FP/(FP+TN) = {fp}/{fp + tn})"
          if (fp + tn) else "  False-positive rate:       n/a (no negative cases)")

    if counts["MANUAL"]:
        print("\n  MANUAL REVIEW NEEDED (descriptive ground truth):")
        for r in rows_out:
            if r["classification"] == "MANUAL":
                print(f"    [{r['id']}] verdict={r['verdict']}  trap={r['is_trap']}  "
                      f"gt={r['ground_truth']}")


if __name__ == "__main__":
    report(run())
