#!/bin/sh
# Recover WI #24 from zombie Cursor run (MCP: no active run, DB: run 14 running)
docker exec iceagent-postgres psql -U ice -d ice_agent -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;

UPDATE cursor_runs
SET status = 'cancelled',
    error = 'Zombie: workspace Cursor не был открыт; MCP: no active run',
    completed_at = NOW()
WHERE id = 14 AND work_item_id = 24 AND status = 'running';

UPDATE work_items
SET active_cursor_run_id = NULL,
    status = 'in_progress',
    wait_owner = 'self',
    next_action = 'Отправить задачу в Cursor заново (workspace d:/projects/uraltrade должен быть открыт)',
    pm_phase = 'READY_FOR_DEV',
    last_error = NULL,
    paused = false,
    wait_until = NULL,
    metadata_json = jsonb_set(
      jsonb_set(
        COALESCE(metadata_json, '{}'::jsonb),
        '{cursor_in_flight}',
        'false'::jsonb
      ),
      '{cursor_assignment_seq}',
      to_jsonb(COALESCE((metadata_json->>'cursor_assignment_seq')::int, 0) + 1)
    )
WHERE id = 24;

INSERT INTO work_item_events (work_item_id, kind, title, detail, payload, created_at)
SELECT 24, 'note', 'Разблокировка zombie Cursor run',
       'Run #14 отменён: окно Cursor на d:/projects/uraltrade не было открыто. Кейс готов к повторной отправке.',
       '{}'::jsonb, NOW()
WHERE EXISTS (SELECT 1 FROM work_items WHERE id = 24);

COMMIT;

SELECT id, status, pm_phase, wait_owner, next_action, active_cursor_run_id FROM work_items WHERE id = 24;
SELECT id, attempt, status, error FROM cursor_runs WHERE id = 14;
SQL
