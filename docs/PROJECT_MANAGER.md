# Project-manager workflow

This document describes the project-manager implementation currently present in **ice.agent**. The application is the existing FastAPI/SQLAlchemy service; it is not Hermes and does not introduce a second runtime. Existing LLM model and provider configuration is deliberately unchanged.

## Activation and existing context

PM behavior is opt-in per Employee profile. The default Employee policy contains `"pm_mode": false`; enable it in the Employee UI or by PATCHing `/api/v1/agents/{agent_id}/employee` while preserving the rest of the policy:

```json
{
  "policy": {
    "pm_mode": true
  }
}
```

The PM instruction and tools are layered into the existing runtime only when this flag is true. Existing `PromptSection` records (`identity`, `role`, `rules`, `skills`, `tone`, and `self_notes`), scoped Mem0 retrieval, and rolling conversation summaries continue to supply context. The implementation does not replace those mechanisms.

Inbound Telegram messages are converted to the channel-neutral `NormalizedInboundEvent` envelope before entering the runtime. It carries `channel`, `conversation_id`, `message_id`, `client_id`, optional `project_id`, text, attachments, timestamp, thread, and channel metadata. This preserves source identity while allowing PM logic to consume a normalized context.

## Persistent PM state

- `WorkItem` is the authoritative task record. It now stores task type, business context, requirements, acceptance criteria, constraints, edge cases, priority, PM phase, source message ID, and the active Cursor run.
- `WorkItemEvent` is the append-only task timeline/audit stream. Phase transitions, scope changes, tool activity, fixes, and QA acceptance are recorded here.
- `ProjectState` stores per-project configuration and autonomy. Its levels are `LEVEL_0` through `LEVEL_3`; a newly created project defaults to `LEVEL_1`.
- `DecisionRecord` stores idempotent project or task decisions, including topic, rationale, confirmer, source message, and context.
- `CursorRun` stores each development attempt, its idempotency key, request, status, structured result, error, and timestamps. A task cannot have two active runs.

The canonical PM phases are:

```text
DISCUSSION → CLARIFICATION → REQUIREMENTS_READY → CLIENT_CONFIRMED
→ READY_FOR_DEV → IN_DEVELOPMENT → DEV_COMPLETE → QA
→ CLIENT_REVIEW → DONE
```

The state machine also supports `BLOCKED`, `CHANGES_REQUESTED`, and `CANCELLED`, with explicit allowed transitions. Not every task visits every phase: for example, a ready task may move from `REQUIREMENTS_READY` directly to `READY_FOR_DEV` when its autonomy gate permits it. Invalid transitions are rejected and readiness gates require a goal, task type, requirements, and acceptance criteria.

## Semantic tool surface

With `pm_mode` enabled, the runtime exposes:

- `pm_structure_task` — create/update a structured, deduplicated `WorkItem`; `create_new_task=true` starts a separate requirement in the same conversation instead of overwriting the current task; raw customer text is not sent directly to development.
- `pm_get_task` — read authoritative task, decisions, Cursor runs, and audit events.
- `pm_record_decision` — persist a confirmed decision idempotently.
- `pm_transition_task` — apply a valid PM phase transition and append an event.
- `submit_development_task` — enforce readiness and autonomy, render a task brief, create a `CursorRun`, and submit it.
- `get_development_status` — poll the active run and persist progress or completion.
- `get_development_result` — read the persisted structured result without changing state.
- `request_development_fix` — record failed QA/change state and start a distinct follow-up attempt.
- `pm_accept_task` — mark `DONE` only after tests and lint pass and the latest run supplies passing evidence for every stored acceptance criterion.

The existing `CursorRemote` MCP transport is reused. PM mode wraps it with the semantic development tools above and removes the generic CursorRemote run/tool entry points from the agent's tool registry, preventing arbitrary raw prompts from bypassing task structure. The CursorRemote server still must be named `cursorremote`, use a bearer token, be restricted to an allowlisted workspace, and be explicitly attached to the agent.

## Autonomy and least privilege

Project autonomy controls development submission:

- `LEVEL_0`: observe/plan; development requires confirmation.
- `LEVEL_1` (default): only a small bug fix inside agreed scope may proceed without further confirmation.
- `LEVEL_2`: work inside agreed scope may proceed.
- `LEVEL_3`: ordinary development may proceed, but it does not bypass high-risk or out-of-scope confirmation.

High-risk work and work outside agreed scope remain gated. Commercial terms, serious deadline commitments, scope conflicts, destructive production actions, security incidents, production-data deletion, and billing changes must be escalated.
High-risk `owner_approved` state is accepted only from a persisted consultation whose status is `approved` and which records the approver; an ordinary admin message or rejected consultation is not approval. `CLIENT_CONFIRMED` records the current identifiable client message as provenance.

Apply least privilege:

1. Keep `pm_mode` disabled for employees that do not manage development.
2. Attach only the MCP servers an agent needs. CursorRemote is not inherited merely because MCP is enabled.
3. Restrict CursorRemote to the exact workspace and keep its bearer token secret.
4. Grant the `cursorremote` mutating permission only to the PM agent that submits work.
5. Start projects at `LEVEL_1`; raise autonomy only for a documented reason and lower it when the broader permission is no longer needed.
6. Treat `WorkItemEvent`, `DecisionRecord`, and `CursorRun` as the audit trail; do not infer completion from conversational text.

## Inspecting and operating PM state

All `/api/v1` endpoints below require the admin bearer token returned by `POST /api/v1/auth/login`.

| Operation | Endpoint |
| --- | --- |
| List project states | `GET /api/v1/pm/projects` |
| Project state, tasks, decisions | `GET /api/v1/pm/projects/{project_id}` |
| Change autonomy/config | `PATCH /api/v1/pm/projects/{project_id}` |
| List an agent's tasks | `GET /api/v1/agents/{agent_id}/work-items?status=all` |
| Task details, decisions, runs, audit | `GET /api/v1/agents/{agent_id}/work-items/{work_item_id}` |
| List attached MCP servers | `GET /api/v1/agents/{agent_id}/mcp-servers` |
| Attach an MCP server | `PUT /api/v1/agents/{agent_id}/mcp-servers/{server_id}` |
| Detach an MCP server | `DELETE /api/v1/agents/{agent_id}/mcp-servers/{server_id}` |

Example autonomy change:

```bash
curl -X PATCH "http://127.0.0.1:8040/api/v1/pm/projects/acme" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"autonomy_level":"LEVEL_1"}'
```

The task-detail response is the most complete inspection surface: `events` is the audit timeline, `decisions` contains task decisions, `cursor_runs` contains all development attempts, and `project` shows the effective autonomy. The Employee UI displays the same PM phase, project autonomy, decisions, and Cursor runs.

## Migration

Revision `e1f2a3b4c5d6` (down revision `c9d0e1f2a3b4`) creates `project_states`, `decision_records`, and `cursor_runs`, and extends `work_items` with PM fields and indexes.

The API container entrypoint runs `python -m app.migrate` before Uvicorn. On an Alembic-managed database this upgrades to head. A legacy schema with an existing `work_items` table is stamped at the pre-PM revision `c9d0e1f2a3b4` and then upgraded normally, so SQLite and PostgreSQL both execute the real PM migration. A much older schema without `work_items` creates the entirely missing current tables before stamping head.

Use the repository's Compose stack:

```bash
cp .env.example .env
# Set ICE_SECRET_KEY and ICE_ADMIN_PASSWORD.
docker compose up -d --build

docker compose ps
curl http://127.0.0.1:8040/health
docker compose logs api --tail 100
docker compose exec api alembic current
```

To run migration explicitly in a one-off API container:

```bash
docker compose run --rm --entrypoint python api -m app.migrate
docker compose run --rm --entrypoint alembic api current
```

To test in a Docker-capable environment (the production image does not include test extras by default):

```bash
docker compose run --rm api sh -lc 'pip install -e ".[test,mem0]" && python -m pytest -q -p pytest_asyncio.plugin'
docker compose run --rm api sh -lc 'pip install -e ".[test,mem0]" && ruff check .'
```

Frontend verification can run directly:

```bash
cd frontend
npm ci
npm run build
```

## Rollback

Use the pre-change repository bundle at:

```text
data/backups/pm-20260824-103901/ice-agent-head.bundle
```

Before changing a deployed database, take a fresh PostgreSQL dump and preserve the `iceagent_agent_data` volume. Then:

1. Stop writers: `docker compose stop api ui`.
2. Verify the repository backup: `git bundle verify data/backups/pm-20260824-103901/ice-agent-head.bundle`.
3. Restore the pre-PM application revision from that bundle in a separate checkout/worktree; do not overwrite an unreviewed working tree.
4. With the database backup confirmed, run the schema rollback against the Compose database:

   ```bash
   docker compose run --rm --entrypoint alembic api downgrade c9d0e1f2a3b4
   ```

5. Rebuild/start the restored revision: `docker compose up -d --build`.
6. Check `docker compose ps`, `/health`, API logs, and the UI. If downgrade or validation fails, stop and restore the PostgreSQL dump rather than attempting ad-hoc schema edits.

The downgrade removes the PM tables and PM columns from `work_items`; preserve any required PM records before running it.

## Current local-only blockers

This workstation cannot perform deployment validation:

- there is no SSH access to `192.168.10.64`;
- no production backup or restart can be performed here;
- no live production Cursor MCP connection can be exercised here;
- this machine lacks Python and Docker, so backend pytest and Alembic migration smoke tests require a Docker-capable environment.

The frontend build can run on this machine. These constraints are environment blockers, not evidence that production migration or CursorRemote execution succeeded.
