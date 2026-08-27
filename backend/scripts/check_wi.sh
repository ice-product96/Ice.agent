#!/bin/sh
docker exec iceagent-postgres psql -U ice -d ice_agent -c "SELECT id, status, pm_phase, wait_owner, next_action, last_error, active_cursor_run_id, project_id FROM work_items WHERE id IN (24,25);"
docker exec iceagent-postgres psql -U ice -d ice_agent -c "SELECT work_item_id, attempt, status, error FROM cursor_runs WHERE work_item_id IN (24,25) ORDER BY work_item_id, attempt;"
docker exec iceagent-postgres psql -U ice -d ice_agent -c "SELECT id, name, cursor_workspace, project_id FROM customers WHERE id='uraltrade' OR project_id='uraltrade';"
