SELECT id, direction, left(text,180) txt, created_at FROM message_logs WHERE chat_id='183432854' AND id > 4251 ORDER BY id;
SELECT id, kind, title, left(detail,200), created_at FROM work_item_events WHERE work_item_id=26 ORDER BY id;
SELECT id, status, pm_phase, wait_owner, next_action, last_error, active_cursor_run_id FROM work_items WHERE id IN (24,25,26);
