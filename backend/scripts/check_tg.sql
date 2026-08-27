SELECT id, status, pm_phase, next_action, title FROM work_items WHERE agent_id=1 ORDER BY id DESC LIMIT 10;
SELECT id, status, left(error,200) err, created_at FROM agent_tasks WHERE agent_id=1 ORDER BY id DESC LIMIT 10;
SELECT id, direction, chat_id, left(text,120) txt, created_at FROM message_logs WHERE agent_id=1 AND chat_id='183432854' ORDER BY id DESC LIMIT 15;
SELECT key, value FROM runtime_settings WHERE false;
SELECT * FROM runtime_settings;
