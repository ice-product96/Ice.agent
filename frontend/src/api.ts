import type {
  AdminSettings, Agent, AgentTask, CronJob, Dashboard, LogEntry, McpServer,
  Conversation, ConversationDetail, LlmProfile, LlmProfileWrite, MemoryItem, Paginated,
  RuntimeSettings, SipAccount, SipCall, TelegramAccount,
} from './types'

const API_BASE = import.meta.env.VITE_API_URL || '/api/v1'

export class ApiError extends Error {
  constructor(public status: number, message: string, public details?: unknown) {
    super(message)
  }
}

function token() {
  return localStorage.getItem('ice_token')
}

async function readBody(response: Response): Promise<unknown> {
  const raw = await response.text()
  if (!raw) return undefined
  try {
    return JSON.parse(raw) as unknown
  } catch {
    return raw
  }
}

function errorMessage(status: number, details: unknown): string {
  if (typeof details === 'string' && details.trim()) return details.trim().slice(0, 400)
  if (typeof details === 'object' && details && 'detail' in details) {
    const detail = (details as { detail: unknown }).detail
    if (typeof detail === 'string') {
      const text = detail.replace(/:\s*$/, '').trim()
      if (text) return text.slice(0, 400)
    }
    if (Array.isArray(detail)) return detail.map(item => typeof item === 'object' && item && 'msg' in item ? String((item as { msg: unknown }).msg) : String(item)).join('; ')
    if (detail != null) return String(detail)
  }
  return `Ошибка запроса (${status})`
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const auth = token()
  if (auth) headers.set('Authorization', `Bearer ${auth}`)
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  // Wrong password on login is also 401 — only kick session for authenticated routes
  if (response.status === 401 && path !== '/auth/login') {
    window.dispatchEvent(new Event('ice:unauthorized'))
  }
  const payload = await readBody(response)
  if (!response.ok) {
    throw new ApiError(response.status, errorMessage(response.status, payload), payload)
  }
  if (response.status === 204) return undefined as T
  return payload as T
}

const body = (data: unknown): RequestInit => ({ body: JSON.stringify(data) })
const qs = (params: Record<string, string | number | undefined>) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => value !== undefined && search.set(key, String(value)))
  return search.size ? `?${search}` : ''
}

export const api = {
  login: (username: string, password: string) =>
    request<{ access_token: string; token_type: string }>('/auth/login', {
      method: 'POST', ...body({ username, password }),
    }),
  me: () => request<{ id: string; username: string }>('/auth/me'),
  dashboard: () => request<Dashboard>('/dashboard'),

  agents: {
    list: () => request<Agent[]>('/agents'),
    create: (data: Omit<Agent, 'id'>) => request<Agent>('/agents', { method: 'POST', ...body(data) }),
    update: (id: string, data: Partial<Agent>) => request<Agent>(`/agents/${id}`, { method: 'PATCH', ...body(data) }),
    remove: (id: string) => request<void>(`/agents/${id}`, { method: 'DELETE' }),
  },
  llmProfiles: {
    list: () => request<LlmProfile[]>('/llm-profiles'),
    create: (data: LlmProfileWrite) => request<LlmProfile>('/llm-profiles', { method: 'POST', ...body(data) }),
    update: (id: string, data: Partial<LlmProfileWrite>) => request<LlmProfile>(`/llm-profiles/${id}`, { method: 'PATCH', ...body(data) }),
    remove: (id: string) => request<void>(`/llm-profiles/${id}`, { method: 'DELETE' }),
    test: (id: string) => request<{ ok?: boolean; message?: string; detail?: string }>(`/llm-profiles/${id}/test`, { method: 'POST' }),
  },
  telegram: {
    list: () => request<TelegramAccount[]>('/telegram/accounts'),
    startLogin: (data: {
      name: string; phone: string; api_id: number; api_hash: string
      http_proxy?: string; mtproto_host?: string; mtproto_port?: number; mtproto_dc_id?: number
    }) =>
      request<{ session_id: string; phone_code_hash?: string }>('/telegram/accounts/login', { method: 'POST', ...body(data) }),
    verifyCode: (data: { session_id: string; code: string; password?: string }) =>
      request<TelegramAccount>('/telegram/accounts/verify', { method: 'POST', ...body(data) }),
    updateProxy: (id: string, data: {
      http_proxy?: string | null
      mtproto_host?: string | null
      mtproto_port?: number | null
      mtproto_dc_id?: number | null
      clear_proxy?: boolean
    }) =>
      request<TelegramAccount>(`/telegram/accounts/${id}/proxy`, { method: 'PATCH', ...body(data) }),
    remove: (id: string) => request<void>(`/telegram/accounts/${id}`, { method: 'DELETE' }),
  },
  sip: {
    list: () => request<SipAccount[]>('/sip/accounts'),
    create: (data: Partial<SipAccount> & { name: string; login: string; password?: string }) =>
      request<SipAccount>('/sip/accounts', { method: 'POST', ...body(data) }),
    update: (id: string, data: Partial<SipAccount> & { password?: string; clear_password?: boolean }) =>
      request<SipAccount>(`/sip/accounts/${id}`, { method: 'PATCH', ...body(data) }),
    register: (id: string) => request<SipAccount>(`/sip/accounts/${id}/register`, { method: 'POST' }),
    remove: (id: string) => request<void>(`/sip/accounts/${id}`, { method: 'DELETE' }),
    calls: (activeOnly = false) =>
      request<{ items: SipCall[]; active: Array<Record<string, unknown>>; total: number }>(
        `/sip/calls${qs({ active_only: activeOnly ? 1 : undefined, limit: 100 })}`,
      ),
    dial: (data: { agent_id: string; number: string; sip_account_id?: string }) =>
      request<Record<string, unknown>>('/sip/calls', { method: 'POST', ...body(data) }),
    hangup: (id: string) => request<{ ok: boolean }>(`/sip/calls/${id}/hangup`, { method: 'POST' }),
    status: () => request<Record<string, unknown>>('/sip/status'),
  },
  journals: {
    clear: (agentId?: string) =>
      request<{
        ok: boolean
        memory_deleted: number
        memory_remaining: number
        calls_deleted: number
        conversations_cleared: number
        messages_deleted: number
      }>(agentId ? `/agents/${agentId}/journals/clear` : '/journals/clear', {
        method: 'POST',
        ...body({ memory: true, calls: true, conversations: true }),
      }),
  },
  memory: {
    list: (search?: string, agentId?: string) =>
      request<Paginated<MemoryItem> | MemoryItem[]>(`/memory${qs({ search, agent_id: agentId })}`),
    migrate: () => request<{ ok: boolean; migrated: number; failed: number; remaining: number; pending_before: number }>('/memory/migrate', { method: 'POST' }),
    remove: (id: string) => request<void>(`/memory/${id}`, { method: 'DELETE' }),
  },
  conversations: {
    list: (agentId?: string, search?: string, limit = 100, offset = 0) =>
      request<Paginated<Conversation> | Conversation[]>(`/conversations${qs({
        agent_id: agentId, search, limit, offset,
      })}`),
    get: (id: string) => request<ConversationDetail>(`/conversations/${id}`),
    clear: (id: string) => request<void>(`/conversations/${id}`, { method: 'DELETE' }),
  },
  mcp: {
    list: () => request<McpServer[]>('/mcp/servers'),
    create: (data: Omit<McpServer, 'id'>) => request<McpServer>('/mcp/servers', { method: 'POST', ...body(data) }),
    update: (id: string, data: Partial<McpServer>) => request<McpServer>(`/mcp/servers/${id}`, { method: 'PATCH', ...body(data) }),
    remove: (id: string) => request<void>(`/mcp/servers/${id}`, { method: 'DELETE' }),
  },
  cron: {
    list: () => request<CronJob[]>('/cron'),
    create: (data: Omit<CronJob, 'id'>) => request<CronJob>('/cron', { method: 'POST', ...body(data) }),
    update: (id: string, data: Partial<CronJob>) => request<CronJob>(`/cron/${id}`, { method: 'PATCH', ...body(data) }),
    remove: (id: string) => request<void>(`/cron/${id}`, { method: 'DELETE' }),
  },
  settings: {
    get: () => request<AdminSettings>('/settings/admin'),
    update: (data: AdminSettings) => request<AdminSettings>('/settings/admin', { method: 'PUT', ...body(data) }),
    runtime: () => request<RuntimeSettings>('/settings/runtime'),
    updateRuntime: (data: RuntimeSettings) => request<RuntimeSettings>('/settings/runtime', { method: 'PUT', ...body(data) }),
    testSearch: () => request<{ ok?: boolean; message?: string; detail?: string }>('/settings/runtime/test-search', { method: 'POST' }),
  },
  logs: (level?: string, search?: string) =>
    request<Paginated<LogEntry> | LogEntry[]>(`/logs${qs({ level, search, limit: 200 })}`),
  tasks: () => request<Paginated<AgentTask> | AgentTask[]>('/tasks?limit=200'),
}

export function openLiveSocket(onMessage: (payload: unknown) => void) {
  const configured = import.meta.env.VITE_WS_URL
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = configured || `${protocol}//${location.host}/ws/events`
  const socket = new WebSocket(`${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token() || '')}`)
  socket.onmessage = (event) => {
    try { onMessage(JSON.parse(event.data)) } catch { onMessage(event.data) }
  }
  return socket
}
