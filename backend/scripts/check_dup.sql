SELECT id, status, pm_phase, wait_owner, next_action, active_cursor_run_id, left(title,80) title, updated_at
FROM work_items WHERE agent_id=1 AND status NOT IN ('done') ORDER BY id DESC LIMIT 8;

SELECT id, work_item_id, attempt, status, left(coalesce(error,''),120) err, started_at, completed_at
FROM cursor_runs WHERE work_item_id IN (
  SELECT id FROM work_items WHERE agent_id=1 ORDER BY id DESC LIMIT 5
) ORDER BY id DESC LIMIT 20;

SELECT work_item_id, kind, title, left(detail,140) detail, created_at
FROM work_item_events
WHERE work_item_id IN (SELECT id FROM work_items WHERE agent_id=1 ORDER BY id DESC LIMIT 3)
ORDER BY id DESC LIMIT 40;
