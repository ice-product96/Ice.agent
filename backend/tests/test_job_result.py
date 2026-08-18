import pytest

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


def test_humanize_hides_duplicate_cron_sql() -> None:
    outcome = humanize_job_outcome(
        None,
        error=RuntimeError(
            'PendingRollbackError: rolled back due to UniqueViolationError: '
            'duplicate key value violates unique constraint "cron_jobs_name_key"'
        ),
    )
    assert outcome["status"] == "error"
    assert "PendingRollback" not in outcome["summary"]
    assert "asyncpg" not in outcome["summary"]
    assert "Сбой записи" in outcome["summary"]


def test_humanize_error() -> None:
    outcome = humanize_job_outcome(None, error=RuntimeError("MCP session terminated"))
    assert outcome["ok"] is False
    assert outcome["status"] == "error"
    assert outcome["title"] == "Ошибка"
    assert "MCP" in outcome["summary"]


def test_humanize_does_not_claim_sent_from_intent_flag() -> None:
    outcome = humanize_job_outcome(
        {"ok": True, "result": "Карусель обновлена."},
        payload={"_deliver_origin_reply": True, "reply_chat_id": None},
    )
    joined = " ".join(outcome["details"])
    assert "отправлен" not in joined
    assert outcome["summary"] == "Карусель обновлена."


def test_humanize_reports_failed_customer_delivery() -> None:
    outcome = humanize_job_outcome(
        {
            "ok": True,
            "result": "Карусель обновлена.",
            "delivery": {"sent": False, "reason": "не сохранён исходный чат заказчика."},
        }
    )
    assert any("Заказчику не отправлено" in item for item in outcome["details"])
    assert not any("Результат отправлен" in item for item in outcome["details"])


def test_followup_payload_keeps_origin_chat() -> None:
    from app.job_result import build_followup_payload, collect_origin_from_jobs, origin_chat_id

    payload = build_followup_payload(
        message="cursorremote_check",
        run_at_iso="2026-08-17T15:00:00+00:00",
        timezone="Asia/Yekaterinburg",
        context={"chat_id": 123456, "phone": "+79001112233", "sender_id": 99},
        account_phone="+79001112233",
    )
    assert payload["reply_chat_id"] == 123456
    assert payload["chat_id"] == 123456
    assert payload["reply_phone"] == "+79001112233"
    recovered = collect_origin_from_jobs([type("Job", (), {"payload": payload})()])
    assert origin_chat_id(recovered) == 123456


def test_telegram_already_sent_ignores_manager_redirect() -> None:
    from app.job_result import telegram_already_sent

    assert not telegram_already_sent(
        [
            {
                "tool": "telegram_send_message",
                "status": "success",
                "result": {"ok": True, "redirected_to_manager": True, "customer_notified": False},
            }
        ]
    )
    assert telegram_already_sent(
        [{"tool": "telegram_send_message", "status": "success", "result": {"ok": True}}]
    )


@pytest.mark.asyncio
async def test_send_origin_reply_only_marks_sent_after_success() -> None:
    from app.job_result import send_origin_reply

    class FakeTelegram:
        def __init__(self) -> None:
            self.sent: list[tuple] = []

        async def send_message(self, phone: str, entity: object, text: str) -> dict:
            self.sent.append((phone, entity, text))
            return {"ok": True}

    telegram = FakeTelegram()
    skipped = await send_origin_reply(telegram, "+7900", None, "готово")
    assert skipped["sent"] is False
    assert "чат" in skipped["reason"]
    assert telegram.sent == []

    delivered = await send_origin_reply(telegram, "+7900", "123", "готово")
    assert delivered["sent"] is True
    assert telegram.sent == [("+7900", 123, "готово")]


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
    assert data["kind"] == "cron"


def test_cron_json_hides_heartbeat_last_result() -> None:
    job = type(
        "Job",
        (),
        {
            "id": 3,
            "name": "employee-heartbeat-1",
            "agent_id": 1,
            "cron": "*/15 * * * *",
            "enabled": True,
            "last_run_at": None,
            "created_at": None,
            "updated_at": None,
            "payload": {
                "source": "employee_heartbeat",
                "kind": "employee_tick",
                "last_result": {
                    "ok": True,
                    "status": "completed",
                    "title": "Сторож",
                    "summary": "Проверено открытых кейсов: 0.",
                },
            },
        },
    )()
    data = cron_json(job)
    assert data["kind"] == "heartbeat"
    assert data["last_result"] is None


def test_humanize_heartbeat_is_watchdog_not_customer_result() -> None:
    outcome = humanize_job_outcome(
        {"ok": True, "result": "журнал тика", "watchdog": {"count": 2}},
        payload={"source": "employee_heartbeat"},
    )
    assert outcome["title"] == "Сторож"
    assert "2" in outcome["summary"]
    assert "журнал тика" not in outcome["summary"]
