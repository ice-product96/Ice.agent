\d agent_tasks
SELECT id, status, left(coalesce(error,''),300) err, created_at, updated_at FROM agent_tasks ORDER BY id DESC LIMIT 8;
SELECT id, status, pm_phase, project_id, customer_id, next_action, left(goal,200), metadata_json->>'cursor_in_flight' FROM work_items WHERE id=26;
SELECT id, kind, title, left(detail,160), created_at FROM work_item_events WHERE work_item_id=26 ORDER BY id DESC LIMIT 20;
SELECT id, direction, left(text,200), created_at FROM message_logs WHERE chat_id='183432854' AND created_at > '2026-08-27 05:50:00+00' ORDER BY id;
