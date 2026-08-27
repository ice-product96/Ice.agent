SELECT id, attempt, status, error,
  left(coalesce(request::text,''),300) req,
  left(coalesce(result::text,''),500) res
FROM cursor_runs WHERE work_item_id=30 ORDER BY id;

SELECT id, kind, title, left(detail,200), created_at
FROM work_item_events
WHERE work_item_id=30 AND (
  kind IN ('created','estimate','blocked','decision')
  OR title ILIKE '%submit%'
  OR title ILIKE '%Cursor%'
  OR title ILIKE '%задубл%'
  OR title ILIKE '%prompt%'
  OR detail ILIKE '%prompt_sent%'
  OR detail ILIKE '%already working%'
  OR detail ILIKE '%mismatch%'
  OR detail ILIKE '%workspace%'
)
ORDER BY id;

SELECT id, status, pm_phase, metadata_json->>'cursor_in_flight' in_flight,
  metadata_json->>'cursor_assignment_seq' seq,
  active_cursor_run_id
FROM work_items WHERE id=30;
