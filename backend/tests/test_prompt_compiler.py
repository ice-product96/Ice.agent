from app.prompt_compiler import (
    clip_to_tokens,
    compile_playbook,
    compile_prompt,
    format_verdicts_block,
    normalize_phase,
)


def test_phase_playbooks_are_distinct() -> None:
    discussion = compile_playbook("DISCUSSION")
    qa = compile_playbook("QA")
    assert "Phase DISCUSSION" in discussion
    assert "Phase QA" in qa
    assert "pm_accept_task" in qa
    assert "never send raw customer text" in discussion.lower()


def test_compile_prompt_budgets_and_verdicts() -> None:
    huge = "x" * 20_000
    prompt, sizes = compile_prompt(
        phase="QA",
        identity_sections=["## Личность\nМакс"],
        dossier=huge,
        memories="- decision: export already agreed",
        verdicts=[
            {
                "kind": "qa_verifier",
                "verdict": "insufficient_evidence",
                "confidence": 0.91,
                "reasoning": "no proof",
                "evidence": [{"source": "cursor_summary", "text": "already implemented"}],
            }
        ],
        known={"verdict": "known", "answer": "Export was agreed last week"},
    )
    assert sizes["total"] > 0
    assert sizes["dossier"] <= 800
    assert "qa_verifier" in prompt
    assert "Already known" in prompt
    assert "Judge verdicts" in prompt
    assert clip_to_tokens(huge, 10).endswith("…")


def test_normalize_phase_aliases() -> None:
    assert normalize_phase("CLIENT_REVIEW") == "QA"
    assert normalize_phase("READY_FOR_DEV") == "READY_FOR_DEV"
    assert "Judge verdicts" in format_verdicts_block(
        [{"kind": "scope_judge", "verdict": "inside_spec", "confidence": 0.8}]
    )
