\d cursor_runs
SELECT id, attempt, status, error, started_at, completed_at,
  left(coalesce(result_json::text, result::text, ''), 800) AS res
FROM cursor_runs WHERE work_item_id=30 ORDER BY id;
