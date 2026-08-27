SELECT max_tool_rounds FROM runtime_settings LIMIT 1;
SELECT id, kind, title, left(detail, 120) FROM work_item_events WHERE work_item_id=24 ORDER BY id DESC LIMIT 5;
SELECT id, status, pm_phase, next_action, active_cursor_run_id FROM work_items WHERE id IN (24,25);
