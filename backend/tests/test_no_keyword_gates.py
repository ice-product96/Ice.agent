"""Guardian: decision modules must not grow new keyword / substring gates."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"

# Snapshot of existing uppercase string collections. New ones in decision
# modules fail the test so keyword gates cannot silently return.
ALLOWLISTED_CONSTANTS = {
    "work_items.py": {"_ACK_WORDS", "_WIPE_VERBS", "OPEN_STATUSES", "TERMINAL_STATUSES", "_WIPE_SCOPE"},
    "tracker_poll.py": {"TRACKER_LANE_ALIASES", "OPEN_TRACKER_STATUSES", "DONE_TRACKER_STATUSES"},
    "cursor_callback.py": {"_FINISHED", "_IN_FLIGHT", "CALLBACK_ARG_KEYS", "_FAILED"},
    "cursorremote_drive.py": {
        "_IN_FLIGHT_ASSISTANT",
        "APPROVE_LABELS",
        "BUSY_STATUSES",
        "IDLE_STATUSES",
        "STOPPED_STATUSES",
        "WORKER_TOOLS",
    },
    "action_reports.py": {"INTERNAL_EXECUTION_SOURCES"},
    "routing.py": {"_REJECT_PREFIXES"},
    "pm_state.py": {
        "AUTONOMY_LEVELS",
        "CURSOR_RUN_TERMINAL_STATUSES",
        "EXECUTION_VERDICTS",
        "MANAGER_CONFIRMERS",
        "PM_PHASES",
        "SPEC_SCOPE_FIELDS",
        "SPEC_STATUSES",
        "_CURSOR_BRIEF_DROP_MARKERS",
        "_DONE_SUMMARY_MARKERS",
    },
    "approval_gate.py": {"INTERNAL_SOURCES"},
    "intake_gate.py": {"WORK_INTENTS"},
}

DECISION_FILES = (
    "work_items.py",
    "customers.py",
    "routing.py",
    "pm_state.py",
    "project_schedule.py",
    "tracker_poll.py",
    "cursor_callback.py",
    "cursorremote_drive.py",
    "action_reports.py",
    "qa_gate.py",
    "approval_gate.py",
    "intake_gate.py",
    "cursor_gate.py",
    "delivery_gate.py",
)


def _string_frozenset_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "frozenset":
            pass
        elif isinstance(value, ast.Tuple) and all(
            isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in value.elts
        ):
            pass
        else:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                names.add(target.id)
    return names


def test_no_new_keyword_gate_constants() -> None:
    extras: list[str] = []
    for name in DECISION_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = _string_frozenset_names(tree)
        allowed = ALLOWLISTED_CONSTANTS.get(name, set())
        unexpected = sorted(found - allowed)
        for item in unexpected:
            extras.append(f"{name}:{item}")
    assert extras == [], f"new keyword-gate constants: {extras}"
