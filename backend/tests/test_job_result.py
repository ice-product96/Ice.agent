from app.contract import cron_json
from app.job_result import (
    describe_value,
    humanize_job_outcome,
    notes_from_audit,
    public_job_result,
)


def test_describe_value_turns_cursor_json_into_russian() -> None:
    assert describe_value('{"done": true, "summary": "ветка собрана"}') == "Cursor закончил работу. ветка собрана"
    assert describe_value({"done": False, "status": "waiting_approval"}).startswith("Cursor ещё работает")
    assert "{" not in describe_value({"done": True, "summary": "готово"})


def test_describe_value_maps_skip_reasons() -> None:
    assert describe_value({"skipped": True, "reason": "off_hours"}) == "Вне рабочих часов — тик пропущен."
    assert describe_value({"skipped": True, "reason": "paused"}) == "Сотрудник на паузе — тик пропущен."


def test_humanize_skipped_tick() -> None:
    outcome = humanize_job_outcome({"ok": True, "skipped": True, "reason": "off_hours"})
    assert outcome["status"] == "skipped"
    assert outcome["title"] == "Пропущено"
    assert "рабочих часов" in outcome["summary"]


def test_humanize_error() -> None:
    outcome = humanize_job_outcome(None, error=RuntimeError("MCP session terminated"))
    assert outcome["ok"] is False
    assert outcome["status"] == "error"
    assert outcome["title"] == "Ошибка"
    assert "MCP" in outcome["summary"]


def test_humanize_agent_reply_and_notes() -> None:
    outcome = humanize_job_outcome(
        {"ok": True, "result": "Сводка отправлена.", "notified": True},
        payload={"_job_notes": ["Сообщение отправлено в Telegram."]},
    )
    assert outcome["status"] == "completed"
    assert outcome["title"] == "Выполнено"
    assert outcome["summary"] == "Сводка отправлена."
    assert "Telegram" in " ".join(outcome["details"])


def test_public_job_result_strips_internal_keys() -> None:
    public = public_job_result(
        {
            "ok": True,
            "status": "completed",
            "title": "Выполнено",
            "summary": "Cursor закончил работу.",
            "details": ["Сообщение отправлено в Telegram."],
            "ran_at": "2026-08-17T12:00:00+00:00",
            "_job_notes": ["secret"],
            "raw": {"done": True},
        }
    )
    assert public is not None
    assert public["title"] == "Выполнено"
    assert "_job_notes" not in public
    assert "raw" not in public
    assert public_job_result({"ok": True}) is None


def test_notes_from_audit_cursor_and_schedule() -> None:
    notes = notes_from_audit(
        [
            {
                "tool": "cursorremote_check",
                "status": "success",
                "result": {"done": True, "summary": "PR готов"},
            },
            {"tool": "schedule_self", "status": "success", "result": {"skipped": True}},
            {"tool": "telegram_send_message", "status": "success", "result": {"ok": True}},
        ]
    )
    assert any("Cursor закончил" in note for note in notes)
    assert any("не ставилась" in note for note in notes)
    assert any("Telegram" in note for note in notes)


def test_cron_json_exposes_readable_last_result() -> None:
    job = type(
        "Job",
        (),
        {
            "id": 7,
            "name": "check",
            "agent_id": 1,
            "cron": "@once",
            "enabled": False,
            "last_run_at": None,
            "created_at": None,
            "updated_at": None,
            "payload": {
                "prompt": "проверь Cursor",
                "timezone": "UTC",
                "run_once_at": "2026-08-17T14:00:00+00:00",
                "last_result": {
                    "ok": True,
                    "status": "completed",
                    "title": "Выполнено",
                    "summary": "Cursor закончил работу.",
                    "details": ["Результат отправлен в исходный чат Telegram."],
                    "raw_json": {"done": True},
                },
            },
        },
    )()
    data = cron_json(job)
    assert data["last_result"]["title"] == "Выполнено"
    assert data["last_result"]["summary"] == "Cursor закончил работу."
    assert "raw_json" not in data["last_result"]
