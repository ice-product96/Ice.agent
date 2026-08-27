SELECT id, status, pm_phase, wait_owner, next_action, active_cursor_run_id,
  metadata_json->>'cursor_in_flight' in_flight,
  left(coalesce(last_error,''),200) err, updated_at
FROM work_items WHERE id=30;

SELECT id, attempt, status, left(coalesce(error,''),160) err,
  left(result_json::text, 400) res, started_at, completed_at
FROM cursor_runs WHERE work_item_id=30 ORDER BY id;

SELECT id, kind, title, left(detail,180) d, created_at
FROM work_item_events WHERE work_item_id=30 ORDER BY id DESC LIMIT 25;
