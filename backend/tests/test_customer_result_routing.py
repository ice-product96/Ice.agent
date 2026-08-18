from app.action_reports import (
    cursor_result_ready_for_customer,
    format_manager_status,
    is_internal_execution,
    should_redirect_customer_outbound,
)


def test_leftover_idle_summary_is_not_customer_result() -> None:
    audit = [
        {
            "tool": "cursorremote_check",
            "status": "success",
            "result": {"done": True, "summary": "сводка по предыдущей задаче"},
        }
    ]
    assert not cursor_result_ready_for_customer(audit)
    assert not cursor_result_ready_for_customer(
        [
            {
                "tool": "cursorremote_do",
                "status": "success",
                "result": {
                    "done": True,
                    "skipped_prompt": True,
                    "prompt_sent": False,
                    "summary": "idle leftover",
                },
            }
        ]
    )


def test_this_assignment_done_is_customer_result() -> None:
    assert cursor_result_ready_for_customer(
        [
            {
                "tool": "cursorremote_do",
                "status": "success",
                "result": {"done": True, "prompt_sent": True, "summary": "скругления готовы"},
            }
        ]
    )
    assert cursor_result_ready_for_customer(
        [
            {
                "tool": "cursorremote_check",
                "status": "success",
                "result": {"done": True, "summary": "готово"},
            }
        ],
        cursor_was_in_flight=True,
    )
    assert not cursor_result_ready_for_customer(
        [
            {
                "tool": "cursorremote_do",
                "status": "success",
                "result": {"done": False, "prompt_sent": True},
            }
        ]
    )


def test_progress_to_customer_is_redirected_on_internal_runs() -> None:
    context = {
        "source": "intake_flush",
        "chat_id": 777,
        "_cursor_was_in_flight": False,
    }
    audit = [
        {
            "tool": "cursorremote_do",
            "status": "success",
            "result": {"done": False, "prompt_sent": True},
        }
    ]
    assert is_internal_execution(context)
    assert should_redirect_customer_outbound(context, audit, 777, admin_ids={1})
    assert not should_redirect_customer_outbound(context, audit, 1, admin_ids={1})
    assert not should_redirect_customer_outbound(
        {"source": "telegram", "chat_id": 777}, audit, 777, admin_ids={1}
    )


def test_manager_status_is_labeled_for_flush() -> None:
    text = format_manager_status(
        agent_name="Макс",
        text="Перевёл задание в Cursor, жду итог.",
        work_item_id=12,
        source="intake_flush",
    )
    assert "Запуск накопленного задания" in text
    assert "Кейс #12" in text
    assert "Перевёл задание" in text
