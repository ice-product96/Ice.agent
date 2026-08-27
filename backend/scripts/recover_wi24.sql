UPDATE cursor_runs SET status='cancelled', error='Zombie: workspace not open', completed_at=NOW() WHERE id=14 AND status='running';
UPDATE work_items SET active_cursor_run_id=NULL, status='in_progress', wait_owner='self', pm_phase='READY_FOR_DEV', next_action='Resend to Cursor', last_error=NULL, paused=false, wait_until=NULL WHERE id=24;
SELECT id, status, pm_phase, active_cursor_run_id FROM work_items WHERE id=24;
SELECT id, status, error FROM cursor_runs WHERE id=14;
