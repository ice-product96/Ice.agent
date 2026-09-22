from pathlib import Path

from evals.run import load_jsonl, score, score_dataset

DATASETS = Path(__file__).resolve().parents[1] / "evals" / "datasets"


def test_eval_datasets_exist_and_score() -> None:
    files = sorted(DATASETS.glob("*.jsonl"))
    assert files, "expected judge eval datasets"
    for path in files:
        rows = load_jsonl(path)
        assert rows
        report = score_dataset(path)
        assert report["n"] == len(rows)
        assert 0 <= report["accuracy"] <= 1


def test_expensive_errors_are_counted() -> None:
    rows = [
        {"expected": "fix_required", "predicted": "accept"},
        {"expected": "accept", "predicted": "accept"},
    ]
    report = score("qa_verifier", rows)
    assert report["expensive"]["false_done"] == 1
    assert report["accuracy"] == 0.5
