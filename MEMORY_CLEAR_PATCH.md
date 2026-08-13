# Очистка памяти агента

Добавлена возможность полной очистки семантической памяти (Mem0/Qdrant) для одного агента, чтобы перенастроить его на новые задачи.

## Новые файлы

| Файл | Назначение |
|------|------------|
| `backend/app/memory_clear.py` | Логика `clear_agent_memory()` |
| `backend/app/memory_routes.py` | `POST /api/v1/agents/{id}/memory/clear` |
| `backend/app/main_memory_clear.py` | Entrypoint с зарегистрированным роутом |

## API

```http
POST /api/v1/agents/{agent_id}/memory/clear
Content-Type: application/json

{
  "include_conversations": false
}
```

Ответ:

```json
{
  "ok": true,
  "memory_deleted": 12,
  "memory_remaining": 0,
  "conversations_cleared": 0
}
```

- `memory_deleted` — сколько записей памяти удалено для агента (все пользователи).
- `include_conversations: true` — дополнительно удаляет все диалоги и транскрипты агента из БД.

## Подключение backend

**Вариант A (рекомендуется)** — одна строка в `backend/app/main.py` после `app.include_router(contract_router)`:

```python
from .memory_routes import router as memory_router
app.include_router(memory_router)
```

**Вариант B** — без правки `main.py`, сменить entrypoint:

```bash
uvicorn app.main_memory_clear:app --host 0.0.0.0 --port 8000
```

В `backend/Dockerfile` заменить `app.main:app` на `app.main_memory_clear:app`.

## Подключение frontend

В `frontend/src/api.ts`, блок `agents`:

```typescript
clearMemory: (id: string, data?: { include_conversations?: boolean }) =>
  request<{ ok: boolean; memory_deleted: number; memory_remaining: number; conversations_cleared: number }>(
    `/agents/${id}/memory/clear`,
    { method: 'POST', ...body(data || {}) },
  ),
```

В `frontend/src/App.tsx` — модальное окно в форме агента (пример):

```tsx
function ClearAgentMemoryModal({ agent, onClose, onCleared }: {
  agent: Agent; onClose: () => void; onCleared: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [includeConversations, setIncludeConversations] = useState(false)
  const [error, setError] = useState('')
  return <Modal title="Очистить память агента?" subtitle="Удаляет все сохранённые факты и контекст памяти для этого агента." onClose={onClose}>
    {error && <Alert message={error}/>}
    <div className="clear-context"><BrainCircuit size={22}/><div><strong>{agent.name}</strong><small>Агент {agent.id}</small></div></div>
    <label className="check"><input type="checkbox" checked={includeConversations} onChange={e => setIncludeConversations(e.target.checked)}/><span>Также очистить диалоги и транскрипты</span></label>
    <div className="modal-actions">
      <button className="secondary" onClick={onClose}>Отмена</button>
      <button className="danger" disabled={busy} onClick={async () => {
        setBusy(true); setError('')
        try { await api.agents.clearMemory(agent.id, { include_conversations: includeConversations }); onCleared() }
        catch (err) { setError(err instanceof Error ? err.message : 'Не удалось очистить память'); setBusy(false) }
      }}>{busy ? <LoaderCircle className="spin" size={16}/> : <Trash2 size={16}/>}Очистить память</button>
    </div>
  </Modal>
}
```

В `AgentForm`, если `form.id` задан, добавить кнопку «Очистить память» и состояние `clearingMemory`.

## Тест

```bash
cd backend && pytest tests/test_memory_clear.py -q
```

## Деплой

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.memory-clear.yml up -d --build
```

Проверка:

```bash
curl -X POST "http://localhost:8040/api/v1/agents/1/memory/clear" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"include_conversations": true}'
```
