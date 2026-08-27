SELECT id, attempt, status, left(coalesce(error,''),160) err,
  left(result_json::text, 700) res,
  left(request_json::text, 200) req
FROM cursor_runs WHERE work_item_id=30 ORDER BY id;
