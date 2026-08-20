#!/usr/bin/env python3
"""Does an LLM in the filtering loop earn its cost?

Ground truth: the 50 threads already ingested. Extraction is the same judgement
we want the filter to make, so a thread that produced facts is a true KEEP and
one that produced none is a true DROP.

Compares: rules only | LLM only | rules then LLM on 'unsure'.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lib import CONFIG_PATH, abs_path, llm_api_key, llm_base_url, llm_model, load_config  # noqa: E402
from triage_threads import api_key, human_segments, llm_verdict, rule_verdict  # noqa: E402


def score(name: str, pairs: list[tuple[str, str]], cost: str = "") -> dict:
    tp = sum(1 for t, p in pairs if t == "keep" and p == "keep")
    tn = sum(1 for t, p in pairs if t == "drop" and p == "drop")
    fp = sum(1 for t, p in pairs if t == "drop" and p == "keep")
    fn = sum(1 for t, p in pairs if t == "keep" and p == "drop")
    n = len(pairs)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    print(
        f"{name:<22} acc {100*(tp+tn)/n:5.1f}%  precision {100*prec:5.1f}%  "
        f"recall {100*rec:5.1f}%  F1 {100*f1:5.1f}   "
        f"[kept {tp+fp}/{n}, missed {fn} good, let in {fp} junk] {cost}"
    )
    return {"acc": (tp + tn) / n, "prec": prec, "rec": rec, "f1": f1, "fp": fp, "fn": fn}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--model", default=None, help="Override config llm.model")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    model = llm_model(cfg, args.model)
    key = api_key(cfg)
    conn = sqlite3.connect(abs_path(cfg, cfg["outputs"]["sqlite"]))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM threads WHERE in_bank=1 AND built=1 ORDER BY content_chars DESC"
    ).fetchall()
    conn.close()
    print(f"Evaluation set: {len(rows)} ingested threads "
          f"({sum(1 for r in rows if r['memory_units'])} produced facts)\n")

    key = api_key()
    rules_pairs, llm_pairs, hybrid_pairs = [], [], []
    llm_time = 0.0
    detail = []

    for r in rows:
        truth = "keep" if r["memory_units"] else "drop"
        content = ""
        p = Path(r["thread_json_path"])
        if p.exists():
            content = json.loads(p.read_text(encoding="utf-8")).get("content") or ""

        rv, reason = rule_verdict(dict(r), content, cfg)
        # rules alone must commit: treat 'unsure' as keep (conservative)
        rules_only = "keep" if rv in ("keep", "unsure") else "drop"

        human, _, _ = human_segments(content, cfg)
        try:
            lv, el = llm_verdict(
                human or content, r["subject"] or "", model, key, llm_base_url(cfg)
            )
            llm_time += el
        except Exception as exc:
            print(f"  llm error: {exc}", file=sys.stderr)
            lv = "keep"

        hybrid = lv if rv == "unsure" else rv

        rules_pairs.append((truth, rules_only))
        llm_pairs.append((truth, lv))
        hybrid_pairs.append((truth, hybrid))
        detail.append(
            {
                "document_id": r["document_id"],
                "subject": (r["subject"] or "")[:60],
                "truth": truth,
                "rules": rules_only,
                "rule_stage": rv,
                "reason": reason,
                "llm": lv,
                "hybrid": hybrid,
            }
        )

    n = len(rows)
    print("=== RESULTS ===")
    score("rules only", rules_pairs, "(free)")
    score("LLM only", llm_pairs, f"({llm_time/n:.2f}s/thread)")
    unsure = sum(1 for d in detail if d["rule_stage"] == "unsure")
    score("rules + LLM on unsure", hybrid_pairs, f"({unsure}/{n} needed the model)")

    print("\n=== disagreements with ground truth (hybrid) ===")
    for d in detail:
        if d["hybrid"] != d["truth"]:
            print(f"  truth={d['truth']:<5} got={d['hybrid']:<5} [{d['reason'][:28]:<28}] {d['subject']}")

    out = abs_path(cfg, "data/review/triage_eval.json")
    out.write_text(json.dumps(detail, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
