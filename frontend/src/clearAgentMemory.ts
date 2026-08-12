import { api } from './api'

const API_BASE = import.meta.env.VITE_API_URL || '/api/v1'

function token() {
  return localStorage.getItem('ice_token')
}

/** Clear all semantic memory for one agent (optional: include conversation transcripts). */
export async function clearAgentMemory(
  agentId: string,
  options?: { include_conversations?: boolean },
): Promise<{
  ok: boolean
  memory_deleted: number
  memory_remaining: number
  conversations_cleared: number
}> {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  const auth = token()
  if (auth) headers.set('Authorization', `Bearer ${auth}`)
  const response = await fetch(`${API_BASE}/agents/${agentId}/memory/clear`, {
    method: 'POST',
    headers,
    body: JSON.stringify(options || {}),
  })
  const payload = await response.json().catch(() => undefined)
  if (!response.ok) {
    const detail = typeof payload?.detail === 'string' ? payload.detail : `Ошибка запроса (${response.status})`
    throw new Error(detail)
  }
  return payload as {
    ok: boolean
    memory_deleted: number
    memory_remaining: number
    conversations_cleared: number
  }
}
