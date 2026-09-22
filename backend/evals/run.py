"""Score judge datasets: precision/recall plus expensive-error counts."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

EXPENSIVE = {
    "qa_verifier": {"false_accept": {"predicted": "accept", "not_expected": "accept"}},
    "scope_judge": {"false_execute": {"predicted": "inside_spec", "not_expected": "inside_spec"}},
    "approval_detect": {"false_approval": {"predicted": "approved", "not_expected": "approved"}},
    "message_intent": {"dropped_work": {"predicted": "acknowledgement", "expected": "work_request"}},
    "cursor_completion": {"false_finished": {"predicted": "finished", "not_expected": "finished"}},
}

DATASETS = Path(__file__).resolve().parent / "datasets"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def score(kind: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = sorted({str(row.get("expected") or "") for row in rows} | {str(row.get("predicted") or "") for row in rows})
    matrix = {label: Counter() for label in labels}
    tp = fp = fn = 0
    expensive: Counter[str] = Counter()
    agreed = 0
    for row in rows:
        expected = str(row.get("expected") or "")
        predicted = str(row.get("predicted") or expected)
        matrix.setdefault(expected, Counter())[predicted] += 1
        if predicted == expected:
            tp += 1
            agreed += 1
        else:
            fp += 1
            fn += 1
        if kind == "qa_verifier" and predicted == "accept" and expected != "accept":
            expensive["false_done"] += 1
        if kind == "scope_judge" and predicted == "inside_spec" and expected != "inside_spec":
            expensive["false_cursor"] += 1
        if kind == "approval_detect" and predicted == "approved" and expected != "approved":
            expensive["false_approval"] += 1
        if kind == "message_intent" and expected in {"work_request", "change_request", "bug_report"} and predicted in {
            "acknowledgement",
            "small_talk",
        }:
            expensive["ignored_customer"] += 1
        if row.get("legacy") is not None and str(row.get("legacy")) != predicted:
            row["agreed"] = False
        else:
            row["agreed"] = predicted == expected
    total = max(1, len(rows))
    return {
        "kind": kind,
        "n": len(rows),
        "accuracy": agreed / total,
        "precision": tp / max(1, tp + fp) if fp else agreed / total,
        "recall": tp / max(1, tp + fn) if fn else agreed / total,
        "expensive": dict(expensive),
        "matrix": {key: dict(value) for key, value in matrix.items()},
        "shadow_disagreements": sum(1 for row in rows if row.get("legacy") not in (None, row.get("predicted"), row.get("expected"))),
    }


def score_dataset(path: Path) -> dict[str, Any]:
    rows = load_jsonl(path)
    kind = path.stem
    if rows and rows[0].get("kind"):
        kind = str(rows[0]["kind"])
    return score(kind, rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Score judge JSONL evals")
    parser.add_argument("paths", nargs="*", type=Path, default=sorted(DATASETS.glob("*.jsonl")))
    args = parser.parse_args()
    reports = [score_dataset(path) for path in args.paths]
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    expensive = sum(sum(report["expensive"].values()) for report in reports)
    return 1 if expensive else 0


if __name__ == "__main__":
    raise SystemExit(main())
