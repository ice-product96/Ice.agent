export type ID = string
export type Status = 'online' | 'offline' | 'error' | 'pending' | 'active' | 'paused' | 'completed'
export type AgentLinkRef = ID | {
  id?: ID
  agent_id?: ID
  target_agent_id: ID
  can_delegate?: boolean
  can_message?: boolean
  permissions?: string[]
}

export interface Agent {
  id: ID
  name: string
  description?: string
  prompt: string
  model: string
  provider?: string
  llm_profile_id?: ID
  telegram_account_id?: ID
  sip_account_id?: ID
  tools: string[]
  tool_permissions?: string[]
  realtime_voice?: string
  realtime_model?: string
  inbound_greeting?: string
  links: AgentLinkRef[]
  typing_enabled: boolean
  enabled: boolean
  status?: Status
  created_at?: string
  updated_at?: string
}

export interface SipAccount {
  id: ID
  name: string
  sip_server: string
  domain: string
  login: string
  auth_username?: string | null
  has_password: boolean
  transport: 'udp' | 'tcp' | string
  sip_proxy?: string | null
  display_name?: string
  caller_id?: string | null
  stun_server?: string | null
  public_ip?: string | null
  enabled: boolean
  register_on_startup: boolean
  max_concurrent_calls: number
  ring_delay_seconds?: number
  registered?: boolean
  registration_status?: string
  last_error?: string | null
  status?: Status
  created_at?: string
  updated_at?: string
}

export interface SipCall {
  id: ID
  agent_id?: ID | null
  sip_account_id?: ID | null
  direction: 'inbound' | 'outbound' | string
  remote_number: string
  status: string
  started_at?: string | null
  answered_at?: string | null
  ended_at?: string | null
  hangup_cause?: string | null
  transcript?: string
  sip_call_id?: string
  created_at?: string
  updated_at?: string
}

export interface TelegramAccount {
  id: ID
  session_id?: ID
  name: string
  phone: string
  api_id: number
  has_api_hash: boolean
  http_proxy?: string | null
  mtproto_host?: string | null
  mtproto_port?: number | null
  mtproto_dc_id?: number | null
  proxy_enabled?: boolean
  readiness?: string
  username?: string
  status: Status
  agent_id?: ID
  authorized?: boolean
  enabled?: boolean
  created_at?: string
}

export interface MemoryItem {
  id: ID
  agent_id?: ID
  scope: string
  key: string
  content: string
  project_id?: string | null
  customer_id?: string | null
  category?: string | null
  metadata?: Record<string, unknown>
  created_at: string
}

export interface McpServer {
  id: ID
  name: string
  url: string
  transport: 'sse' | 'streamable-http' | 'stdio'
  command?: string
  args?: string[]
  env?: Record<string, string>
  enabled: boolean
  status?: Status
  connection_status?: 'connected' | 'disconnected' | 'error'
  connection_error?: string
  tools?: string[]
}

export interface CronJobResult {
  ok?: boolean
  status?: 'completed' | 'skipped' | 'error' | string
  title?: string
  summary?: string
  details?: string[]
  ran_at?: string
}

export interface CronJob {
  id: ID
  name: string
  agent_id: ID
  schedule: string
  run_once_at?: string
  prompt: string
  timezone: string
  enabled: boolean
  last_run_at?: string
  next_run_at?: string
  status?: Status
  last_result?: CronJobResult | null
  kind?: 'cron' | 'heartbeat' | 'followup' | string
  work_item_id?: ID | number | null
}

export interface LogEntry {
  id: ID
  timestamp: string
  level: 'debug' | 'info' | 'warning' | 'error'
  source: string
  message: string
  context?: Record<string, unknown>
}

export interface AgentTask {
  id: ID
  title: string
  from_agent_id?: ID
  to_agent_id: ID
  status: 'queued' | 'running' | 'completed' | 'failed'
  payload?: string
  result?: string
  created_at: string
  updated_at?: string
}

export interface AdminSettings {
  admin_ids: string[]
  escalation_agent_id?: ID
  escalation_chat_id?: string
  notify_on_error: boolean
  notify_on_escalation: boolean
}

export type LlmProvider = 'openai' | 'deepseek' | 'custom-openai-compatible'

export interface LlmProfile {
  id: ID
  name: string
  provider: LlmProvider
  base_url: string
  http_proxy?: string | null
  default_model: string
  enabled: boolean
  has_api_key: boolean
}

export type LlmProfileWrite = Omit<LlmProfile, 'id' | 'has_api_key'> & { api_key?: string }

export interface RuntimeSettings {
  search_provider: string
  searxng_url: string | null
  tavily_api_key?: string
  has_tavily_api_key?: boolean
  tavily_http_proxy?: string | null
  timezone: string
  telegram_history_limit: number
  recent_context_messages: number
  context_max_chars: number
  summarization_enabled: boolean
  summarize_after_messages: number
  memory_enabled: boolean
  memory_backend: string
  mem0_api_key?: string
  has_mem0_api_key: boolean
  memory_status?: string
  memory_error?: string | null
  qdrant_url: string | null
  memory_llm_profile_id?: ID | null
  typing_min_seconds: number
  typing_max_seconds: number
  typing_jitter_seconds: number
  typing_chunk_size: number
  typing_presence: boolean
  task_workers: number
  max_tool_rounds: number
}

export interface ConnectionHealth {
  name: string
  status?: Status | string
  ready?: boolean
  detail?: string
}

export interface Dashboard {
  agents?: { total: number; online: number; errors: number }
  telegram_accounts?: { total: number; connected: number }
  sip_accounts?: { total: number; registered: number; active_calls?: number }
  tasks?: { running: number; queued: number; completed_today: number }
  memory_items?: number
  mcp_servers?: { total: number; online: number }
  uptime_seconds?: number
  connections?: ConnectionHealth[] | Record<string, ConnectionHealth | boolean | string>
  readiness?: ConnectionHealth[] | Record<string, ConnectionHealth | boolean | string>
  conversations_count?: number
  active_conversations_count?: number
  agents_count?: number
  telegram_accounts_count?: number
  sip_accounts_count?: number
  mcp_servers_count?: number
  open_consultations_count?: number
  autonomous_agents_count?: number
  counts?: {
    conversations?: number
    active_conversations?: number
    agents?: number
    telegram_accounts?: number
    sip_accounts?: number
    mcp_servers?: number
    open_consultations?: number
    autonomous_agents?: number
    failed_work_items?: number
    waiting_manager?: number
    [key: string]: number | undefined
  }
}

export interface Conversation {
  id: ID
  agent_id: ID
  account_id: ID
  chat_id: string
  user_id: string
  thread_id?: string
  project_id?: string | null
  customer_id?: string | null
  message_count: number
  last_message_at?: string | null
  last_user_message_at?: string | null
  last_agent_message_at?: string | null
  rolling_summary?: string | null
  updated_at: string
}

export interface ConversationMessage {
  id: ID
  direction: string
  text: string
  sender_id?: string | null
  message_id?: string | null
  message_at?: string | null
  created_at: string
}

export interface ConversationDetail extends Conversation {
  messages: ConversationMessage[]
}

export interface EmployeePolicy {
  approval_required_tools: string[]
  manager_orders_without_approval: boolean
  customer_requests_without_approval: boolean
  consult_manager_on_idle_tick: boolean
  tick_instruction_extra: string
  intake_debounce_minutes: number
  pm_mode: boolean
}

export interface EmployeePolicyCatalog {
  approval_tool_options: Array<{ id: string; label: string }>
  defaults: EmployeePolicy
}

export interface EmployeeProfile {
  id: ID | null
  agent_id: ID
  autonomy_enabled: boolean
  paused: boolean
  heartbeat_minutes: number
  workday_start: string
  workday_end: string
  timezone: string
  budget_ticks_per_day: number
  ticks_used_today: number
  ticks_day?: string | null
  last_tick_at?: string | null
  last_digest_at?: string | null
  role_title: string
  mission: string
  policy?: EmployeePolicy
}

export interface EmployeeRosterEntry extends EmployeeProfile {
  agent_name: string
  agent_enabled: boolean
  telegram_account_id?: ID | null
  has_profile?: boolean
  open_consultations?: number
  work_item_counts?: Record<string, number>
  failed_work_items?: number
  waiting_manager?: number
  actionable_work_items?: number
}

export interface EmployeePlan {
  id: ID
  agent_id: ID
  horizon: 'hour' | 'day' | 'week' | 'month' | string
  period_start?: string | null
  period_end?: string | null
  title: string
  body: { steps?: Array<{ id?: string; title?: string; status?: string; result?: string }> }
  status: string
}

export interface EmployeeNeed {
  id: ID
  agent_id: ID
  kind: string
  title: string
  detail: string
  priority: number
  status: string
  consultation_id?: ID | null
}

export interface Consultation {
  id: ID
  agent_id: ID
  question: string
  context: string
  status: string
  requires_approval: boolean
  action_name?: string | null
  work_item_id?: ID | number | null
  answer_text?: string | null
  answered_by?: string | null
  answered_at?: string | null
}

export interface WorkItemEvent {
  id: ID
  created_at?: string | null
  kind: string
  title: string
  detail?: string
  payload?: Record<string, unknown>
}

export interface DecisionRecord {
  id: ID
  project_id: string
  work_item_id?: ID | null
  topic?: string
  decision: string
  rationale?: string
  confirmed_by?: string
  source_message_id?: string | null
  context?: Record<string, unknown>
  created_at?: string | null
}

export interface CursorRun {
  id: ID
  work_item_id: ID
  project_id: string
  attempt: number
  status: string
  request?: Record<string, unknown>
  result?: Record<string, unknown> | null
  error?: string | null
  started_at?: string | null
  completed_at?: string | null
  created_at?: string | null
}

export interface ProjectState {
  project_id: string
  autonomy_level: 'LEVEL_0' | 'LEVEL_1' | 'LEVEL_2' | 'LEVEL_3'
  config?: Record<string, unknown>
  created_at?: string | null
  updated_at?: string | null
}

export interface PmProjectDetail extends ProjectState {
  decisions: DecisionRecord[]
  tasks: WorkItem[]
}

export interface WorkItem {
  id: ID
  agent_id: ID
  title: string
  goal?: string
  status: string
  status_label?: string
  aborted?: boolean
  next_action?: string
  wait_owner?: string
  wait_until?: string | null
  source?: string
  chat_id?: string | null
  reply_phone?: string | null
  sender_id?: string | null
  sender_username?: string | null
  paused?: boolean
  retry_count?: number
  last_error?: string | null
  consultation_id?: ID | number | null
  cron_job_id?: ID | number | null
  task_type?: 'feature' | 'bug' | 'change' | 'technical' | string
  context?: Record<string, unknown>
  requirements?: string[]
  acceptance_criteria?: string[]
  constraints?: string[]
  edge_cases?: string[]
  priority?: 'critical' | 'high' | 'normal' | 'low' | string
  pm_phase?: string
  project_id?: string | null
  customer_id?: string | null
  source_message_id?: string | null
  active_cursor_run_id?: ID | null
  project?: ProjectState | null
  decisions?: DecisionRecord[]
  cursor_runs?: CursorRun[]
  created_at?: string | null
  updated_at?: string | null
  events?: WorkItemEvent[]
  intake?: {
    armed?: boolean
    count?: number
    debounce_minutes?: number
    flush_job_id?: number | null
    messages?: Array<{ at?: string | null; text?: string; attachments?: Array<{ kind?: string; filename?: string }> }>
  }
}

export interface WorkItemCounts {
  open?: number
  in_progress?: number
  waiting_external?: number
  waiting_customer?: number
  waiting_manager?: number
  collecting?: number
  failed?: number
  paused?: number
  done?: number
  actionable?: number
}

export interface PromptSectionRevision {
  id: ID
  agent_id: ID
  key: string
  source: string
  note?: string
  chars: number
  preview: string
  content: string
  created_at?: string | null
}

export interface EmployeeState {
  agent_id: ID
  agent_name: string
  profile: EmployeeProfile
  prompt_sections: Record<string, string>
  plans?: EmployeePlan[]
  jobs?: CronJob[]
  needs: EmployeeNeed[]
  consultations: Consultation[]
  work_items?: WorkItem[]
  work_item_counts?: WorkItemCounts
  customers?: Customer[]
}

export interface Customer {
  id: ID
  name: string
  notes?: string
  agent_id?: ID | null
  project_id?: string
  cursor_workspace?: string
  cursor_window_id?: string | null
  is_default?: boolean
  prompt_block?: string
  created_at?: string | null
  updated_at?: string | null
}

export interface CursorProjectOption {
  id: string
  label: string
  workspace?: string
  window_id?: string | null
  project_id?: string
  source?: 'live' | 'allowlist' | 'saved' | string
}

export interface Paginated<T> {
  items: T[]
  total: number
  page?: number
  size?: number
}
