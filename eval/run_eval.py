"""Evaluation harness: scores the trust layer using agent_correct as the oracle for 'should fire'.

Scoring methodology
-------------------
The trust layer's job is to catch WRONG answers and stay quiet on CORRECT ones. So the ground
truth for "should the trust layer have fired?" is whether the agent's answer was actually wrong
on THIS run — NOT which bucket the question belongs to. The agent is non-deterministic: it
sometimes gets a trap right (nothing to catch) and sometimes gets a clean question wrong
(should be caught). Using the bucket as the oracle contaminates the metrics; we use
agent_correct instead.

  agent_correct False + verdict != ANSWER  -> TP  (caught a real error)
  agent_correct False + verdict == ANSWER  -> FN  (MISSED a real error — the dangerous case)
  agent_correct True  + verdict == ANSWER  -> TN  (correctly passed a correct query)
  agent_correct True  + verdict != ANSWER  -> FP  (false alarm on a correct query)

bucket / trap_type are kept purely as REPORTING categories (recall broken down by error type).
known_limitation questions are reported separately and excluded from headline metrics.

Manual confirmation
-------------------
Questions with descriptive / multi-row ground truth cannot be auto-scored. Each run is cached
to last_run.json; descriptive cases are marked PENDING until a human confirms agent_correct via
MANUAL_OVERRIDES, after which `--rescore` re-scores the SAME cached run (no agent re-invocation,
so the confirmed judgment stays valid despite agent non-determinism).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.graph import ANSWER, build_graph, load_schema  # noqa: E402
from eval.questions import QUESTIONS  # noqa: E402

REL_TOL = 0.02
CACHE = Path(__file__).parent / "last_run.json"

# Human-confirmed agent_correct for descriptive / multi-row questions, keyed by id.
# Filled in after reviewing the cached run's agent output (see the PENDING section).
MANUAL_OVERRIDES: dict[str, bool] = {
    "t3": False,  # agent total $20.3M is fan-out inflated (clean ~$16.0M)
    "t4": True,   # correct grain, no fan-out; price+freight is a valid reading of "revenue"
    "t7": False,  # COUNT(order_id)=11,115 overcounts vs correct COUNT(DISTINCT)=9,417
    "c2": True,   # payment-type breakdown matches ground truth exactly
}


# --------------------------------------------------------------------------- #
# Scoring helpers                                                             #
# --------------------------------------------------------------------------- #

def extract_scalar(result):
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


def summarize(result):
    """Compact, JSON-serializable summary used to judge multi-row results manually."""
    out = {"n_rows": 0, "grand_total": None, "top_row": None, "preview": []}
    if isinstance(result, list) and result:
        out["n_rows"] = len(result)
        out["preview"] = [list(r) for r in result[:8]]
        numeric_last = [r[-1] for r in result if isinstance(r[-1], (int, float))]
        if numeric_last:
            out["grand_total"] = float(sum(numeric_last))
            out["top_row"] = list(
                max(result, key=lambda r: r[-1] if isinstance(r[-1], (int, float)) else float("-inf"))
            )
    return out


def classify(agent_correct, trust_caught):
    """agent_correct is the oracle. None = manual judgment pending."""
    if agent_correct is None:
        return "PENDING"
    if not agent_correct and trust_caught:
        return "TP"
    if not agent_correct and not trust_caught:
        return "FN"
    if agent_correct and not trust_caught:
        return "TN"
    return "FP"


# --------------------------------------------------------------------------- #
# Run (invokes the agent) and cache                                          #
# --------------------------------------------------------------------------- #

def run_and_cache():
    schema_text = load_schema()
    app = build_graph()
    rows = []

    for q in QUESTIONS:
        state = app.invoke({
            "question": q["question"], "schema_text": schema_text, "sql": "",
            "result": [], "answer": "", "error": None, "trust_checks": [],
            "verdict": ANSWER, "confidence": 1.0, "reasoning": "",
        })
        result = state["result"]
        rows.append({
            "id": q["id"], "bucket": q["bucket"], "trap_type": q["trap_type"],
            "question": q["question"], "ground_truth": q["ground_truth"],
            "sql": state["sql"], "verdict": state["verdict"],
            "confidence": state["confidence"], "trust_caught": state["verdict"] != ANSWER,
            "fired": sorted({c["method"] for c in state.get("trust_checks", []) if c.get("flagged")}),
            "agent_val": extract_scalar(result),
            "summary": summarize(result),
            "error": state.get("error"),
        })

    CACHE.write_text(json.dumps(rows, indent=2, default=str))
    return rows


def load_cache():
    return json.loads(CACHE.read_text())


# --------------------------------------------------------------------------- #
# Scoring + reporting                                                        #
# --------------------------------------------------------------------------- #

def score(rows):
    for r in rows:
        gt = r["ground_truth"]
        if isinstance(gt, (int, float)):
            r["agent_correct"] = numeric_match(r["agent_val"], gt)
        else:
            r["agent_correct"] = MANUAL_OVERRIDES.get(r["id"], None)
        r["classification"] = classify(r["agent_correct"], r["trust_caught"])
    return rows


def _ac_disp(ac):
    return {True: "yes", False: "no", None: "PENDING"}[ac]


def report(rows):
    score(rows)

    # 1. Per-question table -------------------------------------------------- #
    print("\n" + "=" * 96)
    print("1. PER-QUESTION DETAIL  (agent_correct is the scoring oracle)")
    print("=" * 96)
    print(f"{'id':<4} {'bucket':<16} {'trap_type':<20} {'agent_correct':<13} {'verdict':<9} {'class':<8} fired")
    print("-" * 96)
    for r in rows:
        print(f"{r['id']:<4} {r['bucket']:<16} {str(r['trap_type'] or '-'):<20} "
              f"{_ac_disp(r['agent_correct']):<13} {r['verdict']:<9} {r['classification']:<8} "
              f"{','.join(r['fired'])}")

    # PENDING evidence ------------------------------------------------------- #
    pending = [r for r in rows if r["classification"] == "PENDING"]
    if pending:
        print("\n" + "-" * 96)
        print("PENDING MANUAL CONFIRMATION — review agent output vs ground truth, then fill MANUAL_OVERRIDES:")
        print("-" * 96)
        for r in pending:
            s = r["summary"]
            print(f"\n  [{r['id']}] {r['question']}")
            print(f"     ground_truth: {r['ground_truth']}")
            print(f"     agent SQL:    {' '.join(r['sql'].split())[:110]}")
            print(f"     n_rows={s['n_rows']}  grand_total={s['grand_total']}  top_row={s['top_row']}")
            print(f"     verdict={r['verdict']}  fired={r['fired'] or '(none)'}")

    # 2. Headline metrics ---------------------------------------------------- #
    headline = [r for r in rows
                if r["bucket"] != "known_limitation" and r["classification"] in ("TP", "FN", "TN", "FP")]
    tp = sum(r["classification"] == "TP" for r in headline)
    fn = sum(r["classification"] == "FN" for r in headline)
    tn = sum(r["classification"] == "TN" for r in headline)
    fp = sum(r["classification"] == "FP" for r in headline)
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    fpr = fp / (fp + tn) if (fp + tn) else float("nan")
    scored = tp + fn + tn + fp
    agent_correct_n = tn + fp
    n_pending = sum(r["classification"] == "PENDING" for r in rows if r["bucket"] != "known_limitation")

    print("\n" + "=" * 96)
    print("2. HEADLINE METRICS  (oracle = agent_correct; known_limitation excluded)")
    print("=" * 96)
    print(f"  Confusion counts:    TP={tp}  FN={fn}  TN={tn}  FP={fp}   (scored={scored}, pending={n_pending})")
    print(f"  Trust-layer recall:  {recall:.0%}   (TP/(TP+FN) = {tp}/{tp + fn})   "
          f"<- fraction of REAL agent errors caught")
    print(f"  False-positive rate: {fpr:.0%}   (FP/(FP+TN) = {fp}/{fp + tn})   "
          f"<- fraction of CORRECT answers wrongly flagged")
    print(f"  Agent accuracy:      {agent_correct_n}/{scored} correct on scored questions")

    # 3. Recall by error type ------------------------------------------------ #
    print("\n" + "=" * 96)
    print("3. BREAKDOWN BY BUCKET / ERROR TYPE  (reporting only — not the oracle)")
    print("=" * 96)
    print(f"  {'bucket':<16} {'trap_type':<20} {'TP':>3} {'FN':>3} {'TN':>3} {'FP':>3} {'PEND':>5}  recall")
    print("  " + "-" * 78)
    seen = []
    for r in rows:
        key = (r["bucket"], r["trap_type"])
        if key in seen:
            continue
        seen.append(key)
        grp = [x for x in rows if (x["bucket"], x["trap_type"]) == key]
        g_tp = sum(x["classification"] == "TP" for x in grp)
        g_fn = sum(x["classification"] == "FN" for x in grp)
        g_tn = sum(x["classification"] == "TN" for x in grp)
        g_fp = sum(x["classification"] == "FP" for x in grp)
        g_pd = sum(x["classification"] == "PENDING" for x in grp)
        rec = f"{g_tp / (g_tp + g_fn):.0%}" if (g_tp + g_fn) else "  -"
        print(f"  {r['bucket']:<16} {str(r['trap_type'] or '-'):<20} "
              f"{g_tp:>3} {g_fn:>3} {g_tn:>3} {g_fp:>3} {g_pd:>5}  {rec}")

    # 4. Known-limitation report -------------------------------------------- #
    known = [r for r in rows if r["bucket"] == "known_limitation"]
    print("\n" + "=" * 96)
    print("4. KNOWN-LIMITATION REPORT  (documented boundary cases — excluded from headline)")
    print("=" * 96)
    for r in known:
        ac = r["agent_correct"]
        print(f"\n  [{r['id']}] {r['trap_type']}")
        print(f"     agent wrong: {'yes' if ac is False else ('manual' if ac is None else 'no')}"
              f"   (agent_value={r['agent_val']}, ground_truth={r['ground_truth']})")
        print(f"     caught:      {'yes' if r['trust_caught'] else 'NO'}   "
              f"(verdict={r['verdict']}, fired={r['fired'] or '(none)'})")
        if ac is False and not r["trust_caught"]:
            print("     >> CONFIRMED BOUNDARY: agent wrong, trust layer did not catch — as documented.")

    # 5. Detector comparison ------------------------------------------------- #
    print("\n" + "=" * 96)
    print("5. DETECTOR FIRING ON CAUGHT ERRORS  (AST vs reexec vs semantic)")
    print("=" * 96)
    for r in rows:
        if r["classification"] == "TP":
            print(f"  {r['id']:<4} {str(r['trap_type'] or '-'):<20} "
                  f"ast={'Y' if 'ast' in r['fired'] else '-'}  "
                  f"reexec={'Y' if 'reexec' in r['fired'] else '-'}  "
                  f"judge={'Y' if 'llm_judge' in r['fired'] else '-'}  "
                  f"consensus={'Y' if 'consensus' in r['fired'] else '-'}  all={r['fired']}")


if __name__ == "__main__":
    if "--rescore" in sys.argv:
        if not CACHE.exists():
            sys.exit("No cached run found — run without --rescore first.")
        print(f"[rescore] loading cached run from {CACHE.name} (no agent re-invocation)")
        report(load_cache())
    else:
        report(run_and_cache())
