import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity, Bot, BrainCircuit, Briefcase, CalendarClock, CheckCircle2, ChevronRight, CircleAlert,
  Clock3, Database, FileText, Globe2, KeyRound, LayoutDashboard, Link2, LoaderCircle, LogOut, Menu,
  MessageCircle, MessagesSquare, Moon, Phone, PhoneCall, PhoneOff, Play, Plus, RefreshCw, Search, ServerCog, Settings, ShieldCheck,
  Sparkles, Trash2, Users, Wifi, WifiOff, X, Zap,
} from 'lucide-react'
import { api, openLiveSocket } from './api'
import { agentModelPresets, profileModelPresets } from './llmModels'
import type {
  AdminSettings, Agent, AgentTask, Consultation, CronJob, Dashboard, EmployeeState, LogEntry, McpServer,
  Conversation, ConversationDetail, LlmProfile, LlmProfileWrite, MemoryItem, RuntimeSettings,
  SipAccount, SipCall, Status, TelegramAccount,
} from './types'

type Page = 'dashboard' | 'agents' | 'connections' | 'runtime' | 'telegram' | 'sip' | 'calls' | 'conversations' | 'employee' | 'memory' | 'mcp' | 'cron' | 'settings' | 'logs' | 'tasks'
type Icon = typeof LayoutDashboard

const nav: { id: Page; label: string; icon: Icon; group?: string }[] = [
  { id: 'dashboard', label: 'Обзор', icon: LayoutDashboard, group: 'Рабочая область' },
  { id: 'agents', label: 'Агенты', icon: Bot },
  { id: 'employee', label: 'Сотрудник', icon: Briefcase },
  { id: 'connections', label: 'Подключения', icon: KeyRound },
  { id: 'telegram', label: 'Telegram', icon: MessageCircle },
  { id: 'sip', label: 'SIP', icon: Phone },
  { id: 'calls', label: 'Звонки', icon: PhoneCall },
  { id: 'conversations', label: 'Диалоги', icon: MessagesSquare },
  { id: 'memory', label: 'Память', icon: BrainCircuit },
  { id: 'mcp', label: 'MCP-серверы', icon: ServerCog, group: 'Автоматизация' },
  { id: 'cron', label: 'Расписания', icon: CalendarClock },
  { id: 'tasks', label: 'Задачи агентов', icon: Zap },
  { id: 'logs', label: 'Системные логи', icon: FileText, group: 'Система' },
  { id: 'runtime', label: 'Настройки runtime', icon: Globe2 },
  { id: 'settings', label: 'Настройки администратора', icon: Settings },
]

const title: Record<Page, [string, string]> = {
  dashboard: ['Центр управления', 'Статус рабочей области Ice.agent в реальном времени'],
  agents: ['Агенты', 'Настройка интеллекта, подключений и возможностей'],
  employee: ['Сотрудник', 'Автономия, планы, потребности и консультации с руководителем'],
  connections: ['Подключения и провайдеры', 'Управление учётными данными LLM и эндпоинтами моделей'],
  telegram: ['Аккаунты Telegram', 'Управление подключёнными пользовательскими и бот-сессиями'],
  sip: ['SIP-аккаунты', 'Регистрация в АТС и привязка к агентам для голосовых звонков'],
  calls: ['Звонки', 'Активные и завершённые SIP-звонки через OpenAI Realtime'],
  conversations: ['Диалоги', 'Просмотр контекста агентов и транскриптов'],
  memory: ['Память', 'Просмотр и управление сохранённым контекстом агентов'],
  mcp: ['MCP-серверы', 'Подключение агентов к внешним инструментам и ресурсам'],
  cron: ['Расписания', 'Запуск промптов агентов по расписанию'],
  settings: ['Настройки администратора', 'Контроль доступа и маршрутизация эскалации'],
  runtime: ['Настройки runtime', 'Поиск, память, набор текста и поведение воркеров'],
  logs: ['Системные логи', 'Трассировка событий runtime между сервисами'],
  tasks: ['Межагентные задачи', 'Координация и делегирование работ в реальном времени'],
}

const statusLabel: Record<string, string> = {
  online: 'онлайн', offline: 'офлайн', pending: 'ожидание', paused: 'пауза',
  error: 'ошибка', active: 'активен',
}

const taskStatusLabel: Record<string, string> = {
  queued: 'в очереди', running: 'выполняется', completed: 'завершено', failed: 'ошибка',
}

function serviceDisplayName(name: string) {
  const key = name.toLowerCase().replaceAll('_', ' ')
  const map: Record<string, string> = {
    llm: 'LLM', search: 'Поиск', memory: 'Память', telegram: 'Telegram', sip: 'SIP', mcp: 'MCP',
  }
  return map[key] || name.replaceAll('_', ' ')
}

function toolDisplayName(tool: string) {
  const map: Record<string, string> = {
    web_search: 'веб-поиск', memory: 'память', code_execution: 'выполнение кода',
    telegram: 'Telegram', sip: 'SIP', filesystem: 'файловая система', mcp: 'MCP', employee: 'сотрудник',
  }
  return map[tool] || tool.replace('_', ' ')
}

function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const refresh = useCallback(async () => {
    setLoading(true); setError('')
    try { setData(await loader()) } catch (e) { setError(e instanceof Error ? e.message : 'Что-то пошло не так') }
    finally { setLoading(false) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => { void refresh() }, [refresh])
  return { data, setData, loading, error, refresh }
}

function Shell({ page, setPage, logout, children }: {
  page: Page; setPage: (page: Page) => void; logout: () => void; children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  return <div className="app-shell">
    <aside className={`sidebar ${open ? 'open' : ''}`}>
      <div className="brand"><span className="brand-mark"><Sparkles size={19}/></span><span>Ice<span>.agent</span></span></div>
      <button className="close-menu" onClick={() => setOpen(false)} aria-label="Закрыть меню"><X/></button>
      <nav>
        {nav.map((item, i) => {
          const Icon = item.icon
          return <div key={item.id}>
            {item.group && <div className={`nav-group ${i ? 'spaced' : ''}`}>{item.group}</div>}
            <button className={`nav-item ${page === item.id ? 'active' : ''}`} onClick={() => { setPage(item.id); setOpen(false) }}>
              <Icon size={18}/><span>{item.label}</span>{page === item.id && <span className="nav-glow"/>}
            </button>
          </div>
        })}
      </nav>
      <div className="sidebar-foot">
        <div className="system-mini"><span className="pulse"/><div><strong>Система работает</strong><small>Все сервисы подключены</small></div></div>
        <button className="nav-item" onClick={logout}><LogOut size={18}/>Выйти</button>
      </div>
    </aside>
    {open && <button className="scrim" onClick={() => setOpen(false)} aria-label="Закрыть меню"/>}
    <main>
      <header className="topbar">
        <button className="menu-button" onClick={() => setOpen(true)}><Menu/></button>
        <div><h1>{(title[page] || title.dashboard)[0]}</h1><p>{(title[page] || title.dashboard)[1]}</p></div>
        <div className="top-actions"><span className="live-pill"><span className="pulse"/>В эфире</span><button className="avatar">IA</button></div>
      </header>
      <div className="page">{children}</div>
    </main>
  </div>
}

function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('')
    try {
      const result = await api.login(username, password)
      localStorage.setItem('ice_token', result.access_token)
      onLogin()
    } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось войти') }
    finally { setBusy(false) }
  }
  return <div className="login-page">
    <div className="login-orb one"/><div className="login-orb two"/>
    <form className="login-card" onSubmit={submit}>
      <div className="brand login-brand"><span className="brand-mark"><Sparkles size={22}/></span><span>Ice<span>.agent</span></span></div>
      <div className="login-copy"><h1>С возвращением</h1><p>Войдите в центр управления агентами.</p></div>
      {error && <Alert message={error}/>}
      <Field label="Имя пользователя"><input autoFocus required value={username} onChange={e => setUsername(e.target.value)} placeholder="admin"/></Field>
      <Field label="Пароль"><input required type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••"/></Field>
      <button className="primary login-submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={18}/> : <ShieldCheck size={18}/>} Войти</button>
      <small className="secure-note"><ShieldCheck size={13}/> Защищённая зона администрирования</small>
    </form>
  </div>
}

function Loading() { return <div className="state"><LoaderCircle className="spin"/><span>Загрузка данных…</span></div> }
function Alert({ message }: { message: string }) { return <div className="alert"><CircleAlert size={17}/><span>{message}</span></div> }
function Empty({ icon: Icon = Database, title: heading, text }: { icon?: Icon; title: string; text: string }) {
  return <div className="empty"><span><Icon size={26}/></span><h3>{heading}</h3><p>{text}</p></div>
}
function Field({ label, hint, children, wide }: { label: string; hint?: string; children: ReactNode; wide?: boolean }) {
  return <label className={`field ${wide ? 'wide' : ''}`}><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>
}
function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return <label className="toggle-row">{label && <span>{label}</span>}<button type="button" className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><i/></button></label>
}
function StatusDot({ status = 'offline' }: { status?: Status }) {
  return <span className={`status ${status}`}><i/>{statusLabel[status] || status}</span>
}
function Modal({ title: heading, subtitle, onClose, children }: { title: string; subtitle?: string; onClose: () => void; children: ReactNode }) {
  return <div className="modal-wrap" role="dialog" aria-modal="true"><button className="modal-backdrop" onClick={onClose}/><div className="modal">
    <div className="modal-head"><div><h2>{heading}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" onClick={onClose}><X/></button></div>{children}
  </div></div>
}
function SectionHead({ title: heading, text, action }: { title: string; text?: string; action?: ReactNode }) {
  return <div className="section-head"><div><h2>{heading}</h2>{text && <p>{text}</p>}</div>{action}</div>
}
function ConfirmDelete({ name, onClose, onDelete }: { name: string; onClose: () => void; onDelete: () => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  return <Modal title="Удалить элемент?" subtitle={`«${name}» будет удалён без возможности восстановления.`} onClose={onClose}>
    <div className="modal-actions"><button className="secondary" onClick={onClose}>Отмена</button><button className="danger" disabled={busy} onClick={async () => { setBusy(true); await onDelete(); onClose() }}><Trash2 size={16}/>Удалить</button></div>
  </Modal>
}

function ConfirmClearJournals({
  agentId,
  agentName,
  onClose,
  onCleared,
}: {
  agentId?: string
  agentName?: string
  onClose: () => void
  onCleared: () => void | Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const title = agentName ? `Очистить журналы «${agentName}»?` : 'Удалить все журналы?'
  const subtitle = agentName
    ? 'Память, история звонков и диалоги этого агента будут удалены. Активные звонки не трогаем.'
    : 'Память, история звонков и все диалоги будут удалены. Активные звонки не трогаем. Это необратимо.'
  return <Modal title={title} subtitle={subtitle} onClose={onClose}>
    {error && <Alert message={error}/>}
    <div className="modal-actions">
      <button className="secondary" onClick={onClose}>Отмена</button>
      <button className="danger" disabled={busy} onClick={async () => {
        setBusy(true); setError('')
        try {
          await api.journals.clear(agentId)
          await onCleared()
          onClose()
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Не удалось очистить журналы')
          setBusy(false)
        }
      }}>{busy ? <LoaderCircle className="spin" size={16}/> : <Trash2 size={16}/>}Удалить всё</button>
    </div>
  </Modal>
}

function healthItems(data: Dashboard) {
  const source = data.connections || data.readiness
  if (!source) return []
  if (Array.isArray(source)) return source
  return Object.entries(source).map(([name, value]) => {
    if (typeof value === 'boolean') return { name, ready: value }
    if (typeof value === 'string') return { name, status: value }
    return { ...value, name }
  })
}

function DashboardScreen({ go }: { go: (p: Page) => void }) {
  const { data, loading, error, refresh } = useLoad(api.dashboard, [])
  if (loading) return <Loading/>
  if (error) return <><Alert message={error}/><button className="secondary" onClick={refresh}>Повторить</button></>
  if (!data) return <Empty title="Нет данных панели" text="API вернул пустой обзор."/>
  const d = data
  const agents = d.agents ?? { total: d.counts?.agents ?? d.agents_count ?? 0, online: 0, errors: 0 }
  const telegram = d.telegram_accounts ?? { total: d.counts?.telegram_accounts ?? d.telegram_accounts_count ?? 0, connected: 0 }
  const sip = d.sip_accounts ?? { total: d.counts?.sip_accounts ?? d.sip_accounts_count ?? 0, registered: 0, active_calls: 0 }
  const tasks = d.tasks ?? { running: 0, queued: 0, completed_today: 0 }
  const mcp = d.mcp_servers ?? { total: d.counts?.mcp_servers ?? d.mcp_servers_count ?? 0, online: 0 }
  const memoryItems = d.memory_items ?? 0
  const configuration = healthItems(d)
  const conversationCount = d.counts?.conversations ?? d.counts?.active_conversations ?? d.conversations_count ?? d.active_conversations_count
  const openConsults = d.counts?.open_consultations ?? d.open_consultations_count ?? 0
  const autonomous = d.counts?.autonomous_agents ?? d.autonomous_agents_count ?? 0
  const stats = [
    ['Активные агенты', agents.online, `${agents.total} настроено`, Bot, 'violet'],
    ['Сотрудники', autonomous, `${openConsults} консультаций`, Briefcase, 'amber'],
    ['Telegram', telegram.connected, `${telegram.total} аккаунтов`, MessageCircle, 'blue'],
    ['SIP', sip.registered, `${sip.total} аккаунтов · ${sip.active_calls || 0} звонков`, Phone, 'cyan'],
    ['Выполняемые задачи', tasks.running, `${tasks.queued} в очереди`, Zap, 'amber'],
    ['Записи памяти', memoryItems, 'Сохранённый контекст', BrainCircuit, 'cyan'],
    ...(conversationCount === undefined
      ? []
      : [['Активные диалоги', conversationCount, 'Контексты диалогов', MessagesSquare, 'violet'] as const]),
  ] as const
  return <>
    <div className="hero-card">
      <div><span className="eyebrow"><Activity size={14}/> Состояние системы</span><h2>Всё работает штатно.</h2><p>{agents.online} агентов активны и готовы обрабатывать запросы.</p></div>
      <div className="health-ring"><strong>{agents.errors ? '!' : '99.9%'}</strong><span>{agents.errors ? 'внимание' : 'аптайм'}</span></div>
    </div>
    <div className="stat-grid">{stats.map(([label, value, sub, Icon, color]) =>
      <div className="stat-card" key={label}><span className={`stat-icon ${color}`}><Icon/></span><div className="stat-value">{value}</div><strong>{label}</strong><small>{sub}</small></div>
    )}</div>
    {configuration.length > 0 && <section className="panel health-panel">
      <SectionHead title="Готовность конфигурации" text="Проверки провайдеров и подключений от API" action={<button className="secondary compact" onClick={() => go('connections')}>Управление</button>}/>
      <div className="health-grid">{configuration.map((item, index) => {
        const ready = item.ready ?? ['ready', 'online', 'active', 'ok', 'connected', 'configured'].includes(String(item.status).toLowerCase())
        const detail = item.detail || (
          String(item.status).toLowerCase() === 'degraded' ? 'Ограниченный режим' :
          String(item.status).toLowerCase() === 'disabled' ? 'Отключено' :
          item.status || (ready ? 'Готов' : 'Требует настройки')
        )
        return <div className={`health-item ${ready ? 'ready' : 'attention'}`} key={`${item.name}-${index}`}>
          {ready ? <CheckCircle2 size={18}/> : <CircleAlert size={18}/>}<div><strong>{serviceDisplayName(String(item.name))}</strong><small>{detail}</small></div>
        </div>
      })}</div>
    </section>}
    <div className="dashboard-grid">
      <section className="panel"><SectionHead title="Статус сервисов" text="Инфраструктурные подключения"/>
        <div className="service-list">
          {[['Runtime агентов', `${agents.online}/${agents.total}`, agents.errors ? 'error' : 'online'],
            ['Шлюз MCP', `${mcp.online}/${mcp.total}`, mcp.online ? 'online' : 'offline'],
            ['Мост Telegram', `${telegram.connected} подключено`, telegram.connected ? 'online' : 'offline'],
            ['SIP UA', `${sip.registered} зарегистрировано`, sip.registered ? 'online' : 'offline'],
            ['Воркер задач', `${tasks.running} выполняется`, 'online']].map(([name, val, status]) =>
            <div className="service-row" key={name}><span className={`service-icon ${status}`}><Wifi size={17}/></span><div><strong>{name}</strong><small>{val}</small></div><StatusDot status={status as Status}/></div>)}
        </div>
      </section>
      <section className="panel"><SectionHead title="Быстрые действия" text="Частые задачи рабочей области"/>
        <div className="quick-grid">
          {[['Создать агента', 'Настроить нового AI-агента', Bot, 'agents'], ['Подключить Telegram', 'Добавить аккаунт мессенджера', MessageCircle, 'telegram'],
            ['Добавить SIP', 'Аккаунт АТС для голосовых звонков', Phone, 'sip'], ['Запланировать задачу', 'Автоматизировать повторяющуюся задачу', Clock3, 'cron']].map(([label, sub, Icon, page]) =>
            <button className="quick-action" key={label as string} onClick={() => go(page as Page)}><span><Icon size={19}/></span><div><strong>{label as string}</strong><small>{sub as string}</small></div><ChevronRight size={16}/></button>)}
        </div>
      </section>
    </div>
  </>
}

const emptyAgent: Omit<Agent, 'id'> = {
  name: '', description: '', prompt: '', model: 'gpt-5.6-terra', provider: 'openai',
  tools: [], tool_permissions: [], links: [], typing_enabled: true, enabled: true,
}
function AgentsScreen() {
  const { data = [], setData, loading, error, refresh } = useLoad(api.agents.list, [])
  const profiles = useLoad(api.llmProfiles.list, [])
  const telegram = useLoad(api.telegram.list, [])
  const sip = useLoad(api.sip.list, [])
  const [editing, setEditing] = useState<Partial<Agent> | null>(null)
  const [deleting, setDeleting] = useState<Agent | null>(null)
  const [clearing, setClearing] = useState<Agent | null>(null)
  if (loading || profiles.loading || telegram.loading || sip.loading) return <Loading/>
  const profileName = (id?: string) => profiles.data?.find(p => String(p.id) === String(id))?.name || (id ? 'Неизвестный профиль LLM' : 'Без профиля LLM')
  const telegramName = (id?: string) => telegram.data?.find(a => String(a.id) === String(id))?.name || (id ? 'Неизвестный аккаунт Telegram' : 'Без аккаунта Telegram')
  const sipName = (id?: string) => sip.data?.find(a => String(a.id) === String(id))?.name || (id ? 'Неизвестный SIP' : 'Без SIP')
  return <>
    {error && <Alert message={error}/>}
    <SectionHead title={`${data.length} настроенных агентов`} text="У каждого агента изолированное поведение и подключения" action={<button className="primary" onClick={() => setEditing(emptyAgent)}><Plus size={17}/>Новый агент</button>}/>
    {data.length === 0 ? <Empty icon={Bot} title="Агентов пока нет" text="Создайте первого автономного агента."/> :
      <div className="card-grid">{data.map(agent => <article className="entity-card" key={agent.id}>
        <div className="entity-top"><span className="entity-avatar"><Bot/></span><StatusDot status={agent.status || (agent.enabled && agent.llm_profile_id ? 'online' : agent.enabled ? 'pending' : 'paused')}/></div>
        <h3>{agent.name}</h3><p>{agent.description || 'Описание не указано.'}</p>
        <div className="binding-list"><span><KeyRound size={13}/>{profileName(agent.llm_profile_id)}</span><span><MessageCircle size={13}/>{telegramName(agent.telegram_account_id)}</span><span><Phone size={13}/>{sipName(agent.sip_account_id)}</span></div>
        <div className="chip-row"><span className="chip">{agent.provider || 'openai'}</span><span className="chip">{agent.model || 'Без модели'}</span>{agent.tools.slice(0, 2).map(t => <span className="chip" key={t}>{toolDisplayName(t)}</span>)}</div>
        <div className="entity-meta"><span><Link2 size={14}/>{agent.links.length} связей</span><span>{agent.typing_enabled ? 'Индикатор набора вкл.' : 'Индикатор набора выкл.'}</span></div>
        <div className="card-actions"><button className="secondary" onClick={() => setEditing(agent)}>Настроить</button><button className="secondary" onClick={() => setClearing(agent)}>Удалить всё</button><button className="icon-button danger-ghost" onClick={() => setDeleting(agent)}><Trash2 size={17}/></button></div>
      </article>)}</div>}
    {editing && <AgentForm value={editing} agents={data} profiles={profiles.data || []} telegram={telegram.data || []} sip={sip.data || []} onClose={() => setEditing(null)} onSave={async value => {
      if (value.id) { const saved = await api.agents.update(value.id, value); setData(data.map(a => a.id === saved.id ? saved : a)) }
      else { const saved = await api.agents.create(value as Omit<Agent, 'id'>); setData([...data, saved]) }
      setEditing(null)
    }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.agents.remove(deleting.id); setData(data.filter(a => a.id !== deleting.id)) }}/>}
    {clearing && <ConfirmClearJournals agentId={clearing.id} agentName={clearing.name} onClose={() => setClearing(null)} onCleared={() => undefined}/>}
    {error && <button className="secondary" onClick={refresh}><RefreshCw size={16}/>Повторить</button>}
  </>
}

function AgentForm({ value, agents, profiles, telegram, sip, onClose, onSave }: {
  value: Partial<Agent>; agents: Agent[]; profiles: LlmProfile[]; telegram: TelegramAccount[]; sip: SipAccount[];
  onClose: () => void; onSave: (v: Partial<Agent>) => Promise<void>
}) {
  const [form, setForm] = useState(value)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [clearing, setClearing] = useState(false)
  const toolOptions = ['web_search', 'memory', 'code_execution', 'telegram', 'sip', 'filesystem', 'mcp', 'employee']
  const permissionOptions = [
    ['telegram_delete_dialog', 'Удалять диалоги Telegram'],
    ['telegram_delete_messages', 'Удалять сообщения Telegram'],
    ['telegram_leave_channel', 'Покидать каналы и группы'],
    ['schedule_self', 'Создавать отложенные задачи'],
  ] as const
  const linkTarget = (link: Agent['links'][number]) =>
    typeof link === 'string' ? link : String(link.target_agent_id ?? link.agent_id ?? link.id)
  const linked = (id: string) => (form.links || []).some(link => linkTarget(link) === String(id))
  const patch = (v: Partial<Agent>) => setForm(f => ({ ...f, ...v }))
  const provider = form.provider || 'openai'
  const modelPresets = agentModelPresets(provider)
  const modelPresetIds = modelPresets.map(item => item.id)
  const modelSelectValue = modelPresetIds.includes(form.model || '') ? form.model! : '__custom__'
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('')
    try { await onSave(form) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось сохранить агента'); setBusy(false) }
  }
  return <Modal title={form.id ? 'Настройка агента' : 'Создание агента'} subtitle="Задайте личность, runtime и взаимодействие." onClose={onClose}>
    <form onSubmit={submit}>
      {error && <Alert message={error}/>}
      <div className="form-grid">
        <Field label="Имя"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Исследовательский ассистент"/></Field>
        <Field label="Описание"><input value={form.description || ''} onChange={e => patch({ description: e.target.value })} placeholder="Чем занимается агент"/></Field>
        <Field label="Профиль LLM" hint="Только включённые профили могут выполнять работу"><select value={form.llm_profile_id || ''} onChange={e => {
          const profile = profiles.find(p => String(p.id) === e.target.value)
          const nextProvider = profile?.provider === 'deepseek' ? 'deepseek' : profile ? 'openai' : provider
          const presets = agentModelPresets(nextProvider)
          const nextModel = profile?.default_model || form.model || presets[0]?.id || ''
          patch({
            llm_profile_id: e.target.value || undefined,
            provider: nextProvider,
            model: presets.some(item => item.id === nextModel) ? nextModel : nextModel,
          })
        }}><option value="">Выберите профиль LLM</option>{profiles.map(p => <option value={p.id} key={p.id}>{p.name} · {p.provider}{p.enabled ? '' : ' (отключён)'}</option>)}</select></Field>
        <Field label="Провайдер LLM" hint="Определяет эндпоинт и список моделей"><select value={provider} onChange={e => {
          const nextProvider = e.target.value
          const presets = agentModelPresets(nextProvider)
          const nextModel = presets.some(item => item.id === form.model) ? form.model : presets[0]?.id || ''
          patch({ provider: nextProvider, model: nextModel })
        }}><option value="openai">OpenAI</option><option value="deepseek">DeepSeek</option></select></Field>
        <Field label="Модель" hint="Переопределение модели агента; профиль LLM задаёт ключ и base URL">
          <select value={modelSelectValue} onChange={e => {
            const value = e.target.value
            patch({ model: value === '__custom__' ? '' : value })
          }}>
            {modelPresets.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
            <option value="__custom__">Другая модель…</option>
          </select>
          {modelSelectValue === '__custom__' && <input required value={form.model || ''} onChange={e => patch({ model: e.target.value })} placeholder="gpt-5.6-terra" style={{ marginTop: '0.5rem' }}/>}
        </Field>
        <Field label="Аккаунт Telegram" hint="Необязательная мессенджер-идентичность"><select value={form.telegram_account_id || ''} onChange={e => patch({ telegram_account_id: e.target.value || undefined })}><option value="">Без аккаунта Telegram</option>{telegram.map(a => <option value={a.id} key={a.id}>{a.name} · {a.phone}{a.readiness && a.readiness !== 'ready' ? ` (${a.readiness})` : ''}</option>)}</select></Field>
        <Field label="SIP-аккаунт" hint="Для входящих/исходящих голосовых звонков через OpenAI Realtime"><select value={form.sip_account_id || ''} onChange={e => patch({ sip_account_id: e.target.value || undefined })}><option value="">Без SIP</option>{sip.map(a => <option value={a.id} key={a.id}>{a.name} · {a.login}{a.registered ? ' (reg)' : ''}</option>)}</select></Field>
        <Field label="Realtime voice" hint="Голос OpenAI Realtime на звонках"><input value={form.realtime_voice || 'marin'} onChange={e => patch({ realtime_voice: e.target.value })} placeholder="marin"/></Field>
        <Field label="Realtime model" hint="Точный id: gpt-realtime-2 (строчные буквы). HTTP-прокси — из профиля LLM агента."><input value={form.realtime_model || 'gpt-realtime-2'} onChange={e => patch({ realtime_model: e.target.value })} placeholder="gpt-realtime-2"/></Field>
        <Field label="Приветствие на входящем" hint="Агент говорит первым после ответа. Пусто — «Ало! Чем могу помочь?»." wide><textarea rows={3} value={form.inbound_greeting || ''} onChange={e => patch({ inbound_greeting: e.target.value })} placeholder="Ало! Меня зовут … Чем могу помочь?"/></Field>
        <Field label="Системный промпт" wide><textarea required rows={7} value={form.prompt || ''} onChange={e => patch({ prompt: e.target.value })} placeholder="Вы полезный агент…"/></Field>
        <Field label="Инструменты" wide><div className="check-grid">{toolOptions.map(tool => <label className="check" key={tool}><input type="checkbox" checked={(form.tools || []).includes(tool)} onChange={() => patch({ tools: (form.tools || []).includes(tool) ? form.tools!.filter(t => t !== tool) : [...(form.tools || []), tool] })}/><span>{toolDisplayName(tool)}</span></label>)}</div></Field>
        <Field label="Опасные действия" hint="Отправка сообщений и вступление в каналы уже входят в инструмент Telegram. Здесь — только необратимые операции." wide><div className="check-grid">{permissionOptions.map(([permission, label]) => <label className="check" key={permission}><input type="checkbox" checked={(form.tool_permissions || []).includes(permission)} onChange={() => patch({ tool_permissions: (form.tool_permissions || []).includes(permission) ? form.tool_permissions!.filter(item => item !== permission) : [...(form.tool_permissions || []), permission] })}/><span>{label}</span></label>)}</div></Field>
        <Field label="Связи с агентами" hint="Разрешить делегирование работы" wide><div className="check-grid">{agents.filter(a => String(a.id) !== String(form.id)).map(a => <label className="check" key={a.id}><input type="checkbox" checked={linked(a.id)} onChange={() => patch({ links: linked(a.id) ? (form.links || []).filter(link => linkTarget(link) !== String(a.id)) : [...(form.links || []), a.id] })}/><span>{a.name}</span></label>)}</div></Field>
        <div className="toggle-box"><Toggle label="Показывать индикатор набора" checked={form.typing_enabled ?? true} onChange={v => patch({ typing_enabled: v })}/></div>
        <div className="toggle-box"><Toggle label="Агент включён" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div>
      </div>
      <div className="modal-actions">{form.id && <button type="button" className="danger" onClick={() => setClearing(true)}><Trash2 size={16}/>Удалить всё</button>}<button type="button" className="secondary" onClick={onClose}>Отмена</button><button className="primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16}/>}Сохранить агента</button></div>
    </form>
    {clearing && form.id && <ConfirmClearJournals agentId={String(form.id)} agentName={form.name} onClose={() => setClearing(false)} onCleared={() => setClearing(false)}/>}
  </Modal>
}

const emptyProfile: LlmProfileWrite = { name: '', provider: 'openai', base_url: 'https://api.openai.com/v1', default_model: 'gpt-5.6-terra', enabled: true, api_key: '', http_proxy: '' }
function ConnectionsScreen() {
  const loaded = useLoad(api.llmProfiles.list, []); const profiles = loaded.data || []
  const [editing, setEditing] = useState<Partial<LlmProfile> | LlmProfileWrite | null>(null)
  const [deleting, setDeleting] = useState<LlmProfile | null>(null)
  const [testing, setTesting] = useState<string>(); const [testResult, setTestResult] = useState<Record<string, string>>({})
  if (loaded.loading) return <Loading/>
  async function test(profile: LlmProfile) {
    setTesting(profile.id); setTestResult(r => ({ ...r, [profile.id]: '' }))
    try { const result = await api.llmProfiles.test(profile.id); setTestResult(r => ({ ...r, [profile.id]: result.message || result.detail || (result.ok === false ? 'Подключение не удалось' : 'Подключение успешно') })) }
    catch (err) { setTestResult(r => ({ ...r, [profile.id]: err instanceof Error ? err.message : 'Тест не пройден' })) }
    finally { setTesting(undefined) }
  }
  return <>
    {loaded.error && <Alert message={loaded.error}/>}
    <SectionHead title={`${profiles.length} профилей LLM`} text="Учётные данные и эндпоинты для агентов" action={<button className="primary" onClick={() => setEditing(emptyProfile)}><Plus size={17}/>Новый профиль</button>}/>
    {profiles.length === 0 ? <Empty icon={KeyRound} title="Нет профилей LLM" text="Добавьте эндпоинт провайдера и API-ключ перед настройкой агентов."/> :
      <div className="card-grid">{profiles.map(profile => <article className="entity-card provider-card" key={profile.id}>
        <div className="entity-top"><span className="entity-avatar"><KeyRound/></span><StatusDot status={profile.enabled ? (profile.has_api_key ? 'online' : 'pending') : 'paused'}/></div>
        <h3>{profile.name}</h3><p>{profile.base_url || 'Эндпоинт провайдера по умолчанию'}</p>
        <div className="chip-row"><span className="chip">{profile.provider}</span><span className="chip">{profile.default_model || 'Без модели по умолчанию'}</span></div>
        <div className={`secret-state ${profile.has_api_key ? 'configured' : ''}`}><ShieldCheck size={14}/>{profile.has_api_key ? 'API-ключ настроен · ••••••••' : 'API-ключ отсутствует'}</div>
        {profile.http_proxy && <div className="secret-state configured"><Globe2 size={14}/>HTTP-прокси: {profile.http_proxy.replace(/\/\/[^@]+@/, '//***@')}</div>}
        {testResult[profile.id] && <small className="inline-result">{testResult[profile.id]}</small>}
        <div className="card-actions"><button className="secondary" disabled={testing === profile.id} onClick={() => void test(profile)}>{testing === profile.id ? <LoaderCircle className="spin" size={15}/> : <Wifi size={15}/>}Тест</button><button className="secondary" onClick={() => setEditing(profile)}>Изменить</button><button className="icon-button danger-ghost" onClick={() => setDeleting(profile)}><Trash2 size={17}/></button></div>
      </article>)}</div>}
    {editing && <LlmProfileForm value={editing} onClose={() => setEditing(null)} onSave={async (value, id) => {
      const saved = id ? await api.llmProfiles.update(id, value) : await api.llmProfiles.create(value as LlmProfileWrite)
      loaded.setData(id ? profiles.map(p => p.id === saved.id ? saved : p) : [...profiles, saved]); setEditing(null)
    }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.llmProfiles.remove(deleting.id); loaded.setData(profiles.filter(p => p.id !== deleting.id)) }}/>}
  </>
}

function LlmProfileForm({ value, onClose, onSave }: { value: Partial<LlmProfile> | LlmProfileWrite; onClose: () => void; onSave: (v: Partial<LlmProfileWrite>, id?: string) => Promise<void> }) {
  const id = 'id' in value ? value.id : undefined
  const [form, setForm] = useState<Partial<LlmProfileWrite>>({ name: value.name, provider: value.provider, base_url: value.base_url, default_model: value.default_model, enabled: value.enabled, http_proxy: value.http_proxy || '', api_key: '' })
  const [busy, setBusy] = useState(false); const [error, setError] = useState('')
  const patch = (p: Partial<LlmProfileWrite>) => setForm(f => ({ ...f, ...p }))
  const profilePresets = profileModelPresets(form.provider)
  const profilePresetIds = profilePresets.map(item => item.id)
  const profileModelSelect = profilePresetIds.includes(form.default_model || '') ? form.default_model! : '__custom__'
  return <Modal title={id ? 'Изменение профиля LLM' : 'Новый профиль LLM'} subtitle="Ключи только для записи и не загружаются обратно в форму." onClose={onClose}><form onSubmit={async e => {
    e.preventDefault(); setBusy(true); setError('')
    const payload = { ...form, http_proxy: (form.http_proxy || '').trim() || null }; if (!payload.api_key) delete payload.api_key
    try { await onSave(payload, id) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось сохранить профиль'); setBusy(false); patch({ api_key: '' }) }
  }}>
    {error && <Alert message={error}/>}<div className="form-grid">
      <Field label="Имя профиля"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Production OpenAI"/></Field>
      <Field label="Провайдер"><select value={form.provider} onChange={e => {
        const nextProvider = e.target.value as LlmProfileWrite['provider']
        const presets = profileModelPresets(nextProvider)
        const nextModel = presets.some(item => item.id === form.default_model) ? form.default_model : presets[0]?.id || form.default_model
        const baseUrl = nextProvider === 'deepseek' ? 'https://api.deepseek.com' : nextProvider === 'openai' ? 'https://api.openai.com/v1' : form.base_url
        patch({ provider: nextProvider, default_model: nextModel, base_url: baseUrl })
      }}><option value="openai">OpenAI</option><option value="deepseek">DeepSeek</option><option value="custom-openai-compatible">Пользовательский / совместимый</option></select></Field>
      <Field label="Базовый URL" wide><input required type="url" value={form.base_url || ''} onChange={e => patch({ base_url: e.target.value })} placeholder="https://api.example.com/v1"/></Field>
      <Field label="Модель по умолчанию">
        {profilePresets.length > 0 ? <>
          <select value={profileModelSelect} onChange={e => {
            const value = e.target.value
            patch({ default_model: value === '__custom__' ? '' : value })
          }}>
            {profilePresets.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
            <option value="__custom__">Другая модель…</option>
          </select>
          {profileModelSelect === '__custom__' && <input required value={form.default_model || ''} onChange={e => patch({ default_model: e.target.value })} placeholder="gpt-5.6-terra" style={{ marginTop: '0.5rem' }}/>}
        </> : <input required value={form.default_model || ''} onChange={e => patch({ default_model: e.target.value })} placeholder="gpt-5.6-terra"/>}
      </Field>
      <Field label={id ? 'Заменить API-ключ' : 'API-ключ'} hint={id ? 'Оставьте пустым, чтобы сохранить текущий ключ' : 'Хранится безопасно на сервере'}><input required={!id} autoComplete="new-password" type="password" value={form.api_key || ''} onChange={e => patch({ api_key: e.target.value })} placeholder={id ? 'Оставьте пустым для сохранения' : 'sk-…'}/></Field>
      <Field label="HTTP-прокси OpenAI" hint="Для SIP Realtime нужен туннель CONNECT на api.openai.com:443 (как в Mtz). Обычный HTTP-прокси для Chat Completions часто не умеет WSS. Примеры: http://user:pass@host:8080 или socks5h://user:pass@host:1080. Из Docker не указывайте 127.0.0.1 — host.docker.internal или LAN IP хоста." wide><input value={form.http_proxy || ''} onChange={e => patch({ http_proxy: e.target.value })} placeholder="socks5h://127.0.0.1:1080" autoComplete="off"/></Field>
      <div className="toggle-box wide"><Toggle label="Профиль включён" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div>
    </div><div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Отмена</button><button className="primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16}/>}Сохранить профиль</button></div>
  </form></Modal>
}

const emptySip: Partial<SipAccount> & { password?: string } = {
  name: '', sip_server: 'voice.telphin.com:5068', domain: 'sip.telphin.com', login: '',
  auth_username: '', transport: 'udp', display_name: '', enabled: true, register_on_startup: true,
  max_concurrent_calls: 1, ring_delay_seconds: 4, password: '',
}

function SipScreen() {
  const { data = [], setData, loading, error, refresh } = useLoad(api.sip.list, [])
  const [editing, setEditing] = useState<(Partial<SipAccount> & { password?: string }) | null>(null)
  const [deleting, setDeleting] = useState<SipAccount | null>(null)
  const [busyId, setBusyId] = useState<string>()
  const [actionError, setActionError] = useState('')
  if (loading) return <Loading/>
  function statusHint(account: SipAccount) {
    if (account.registered) return 'REGISTER OK — аккаунт зарегистрирован в АТС'
    if (account.last_error) return account.last_error
    if (!account.has_password) return 'Пароль не задан — сохраните пароль и нажмите REGISTER'
    if (account.registration_status && account.registration_status !== 'offline' && account.registration_status !== 'idle') {
      return `Статус REGISTER: ${account.registration_status}`
    }
    return 'Ещё не зарегистрирован. Нажмите REGISTER — если ошибка, она появится здесь и в ответе API.'
  }
  return <>
    {(error || actionError) && <Alert message={error || actionError}/>}
    <SectionHead
      title={`${data.length} SIP-аккаунтов`}
      text="«Ожидание» = включён, но REGISTER ещё не успешен. Смотрите статус и ошибку на карточке."
      action={<div style={{ display: 'flex', gap: 8 }}>
        <button className="secondary" onClick={() => { setActionError(''); void refresh() }}><RefreshCw size={16}/>Обновить</button>
        <button className="primary" onClick={() => setEditing(emptySip)}><Plus size={17}/>Добавить SIP</button>
      </div>}
    />
    {data.length === 0 ? <Empty icon={Phone} title="Нет SIP-аккаунтов" text="Добавьте логин/пароль АТС, затем привяжите аккаунт к агенту."/> :
      <div className="card-grid">{data.map(account => <article className="entity-card" key={account.id}>
        <div className="entity-top"><span className="entity-avatar"><Phone/></span><StatusDot status={(account.status || (account.registered ? 'online' : account.enabled ? 'pending' : 'paused')) as Status}/></div>
        <h3>{account.name}</h3>
        <p>{account.login}@{account.domain}</p>
        <div className="chip-row">
          <span className="chip">{account.sip_server}</span>
          <span className="chip">{account.transport}</span>
          <span className="chip">{account.registered ? 'registered' : (account.registration_status || 'offline')}</span>
        </div>
        <div className={`secret-state ${account.has_password ? 'configured' : ''}`}><ShieldCheck size={14}/>{account.has_password ? 'Пароль настроен · ••••••••' : 'Пароль отсутствует'}</div>
        <small className="inline-result" style={{ display: 'block', marginTop: 8, color: account.registered ? undefined : 'var(--danger, #c44)' }}>
          {statusHint(account)}
        </small>
        <div className="card-actions">
          <button className="secondary" disabled={busyId === account.id} onClick={async () => {
            setBusyId(account.id); setActionError('')
            try {
              const saved = await api.sip.register(account.id)
              setData(data.map(a => a.id === saved.id ? saved : a))
              if (!saved.registered) {
                setActionError(saved.last_error || saved.registration_status || 'REGISTER не удался')
              }
            } catch (err) {
              setActionError(err instanceof Error ? err.message : 'REGISTER не удался')
              await refresh()
            } finally { setBusyId(undefined) }
          }}>{busyId === account.id ? <LoaderCircle className="spin" size={15}/> : <Wifi size={15}/>}REGISTER</button>
          <button className="secondary" onClick={() => setEditing(account)}>Изменить</button>
          <button className="icon-button danger-ghost" onClick={() => setDeleting(account)}><Trash2 size={17}/></button>
        </div>
      </article>)}</div>}
    {editing && <SipAccountForm value={editing} onClose={() => setEditing(null)} onSave={async value => {
      const saved = value.id
        ? await api.sip.update(value.id, value)
        : await api.sip.create(value as Partial<SipAccount> & { name: string; login: string; password?: string })
      setData(value.id ? data.map(a => a.id === saved.id ? saved : a) : [...data, saved])
      setEditing(null)
      if (saved.enabled && !saved.registered) {
        setActionError(saved.last_error || `После сохранения REGISTER не прошёл: ${saved.registration_status || 'ожидание'}`)
      }
    }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => {
      await api.sip.remove(deleting.id); setData(data.filter(a => a.id !== deleting.id))
    }}/>}
  </>
}

function SipAccountForm({ value, onClose, onSave }: {
  value: Partial<SipAccount> & { password?: string }
  onClose: () => void
  onSave: (v: Partial<SipAccount> & { password?: string; clear_password?: boolean }) => Promise<void>
}) {
  const [form, setForm] = useState(value)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const patch = (v: Partial<typeof form>) => setForm(f => ({ ...f, ...v }))
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('')
    try { await onSave(form) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось сохранить'); setBusy(false) }
  }
  return <Modal title={form.id ? 'SIP-аккаунт' : 'Новый SIP-аккаунт'} subtitle="Параметры регистрации в АТС" onClose={onClose}>
    <form onSubmit={submit}>
      {error && <Alert message={error}/>}
      <div className="form-grid">
        <Field label="Имя"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Telphin sales"/></Field>
        <Field label="Логин"><input required value={form.login || ''} onChange={e => patch({ login: e.target.value })} placeholder="062xxx"/></Field>
        <Field label="SIP server" hint="host:port"><input required value={form.sip_server || ''} onChange={e => patch({ sip_server: e.target.value })} placeholder="voice.telphin.com:5068"/></Field>
        <Field label="Domain"><input required value={form.domain || ''} onChange={e => patch({ domain: e.target.value })} placeholder="sip.telphin.com"/></Field>
        <Field label="Auth username" hint="Часто совпадает с логином"><input value={form.auth_username || ''} onChange={e => patch({ auth_username: e.target.value })} placeholder={form.login || ''}/></Field>
        <Field label="Пароль" hint={form.id ? 'Оставьте пустым, чтобы не менять' : 'Обязателен'}><input type="password" value={form.password || ''} onChange={e => patch({ password: e.target.value })} autoComplete="new-password" required={!form.id}/></Field>
        <Field label="Transport"><select value={form.transport || 'udp'} onChange={e => patch({ transport: e.target.value })}><option value="udp">UDP</option><option value="tcp">TCP</option></select></Field>
        <Field label="Proxy (опц.)"><input value={form.sip_proxy || ''} onChange={e => patch({ sip_proxy: e.target.value })} placeholder="пусто = sip_server"/></Field>
        <Field label="Display name"><input value={form.display_name || ''} onChange={e => patch({ display_name: e.target.value })}/></Field>
        <Field label="Caller ID"><input value={form.caller_id || ''} onChange={e => patch({ caller_id: e.target.value })}/></Field>
        <Field label="Public IP" hint="Необязательно. Пусто = авто IP хоста (как softphone). В Docker часто нужен LAN/белый IP хоста, не 172.*"><input value={form.public_ip || ''} onChange={e => patch({ public_ip: e.target.value })} placeholder="авто или 192.168.x.x"/></Field>
        <Field label="STUN"><input value={form.stun_server || ''} onChange={e => patch({ stun_server: e.target.value })}/></Field>
        <Field label="Макс. параллельных звонков"><input type="number" min={1} max={32} value={form.max_concurrent_calls ?? 1} onChange={e => patch({ max_concurrent_calls: Number(e.target.value) || 1 })}/></Field>
        <Field label="Гудок до ответа, сек" hint="Сколько секунд абонент слышит гудок (180 Ringing), прежде чем агент возьмёт трубку. 0 — сразу."><input type="number" min={0} max={30} step={0.5} value={form.ring_delay_seconds ?? 4} onChange={e => patch({ ring_delay_seconds: Number(e.target.value) })}/></Field>
        <div className="toggle-box"><Toggle label="Включён — REGISTER и входящие автоматически" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div>
      </div>
      <div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Отмена</button><button className="primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16}/>}Сохранить</button></div>
    </form>
  </Modal>
}

function CallsScreen() {
  const agents = useLoad(api.agents.list, [])
  const { data, loading, error, refresh, setData } = useLoad(() => api.sip.calls(false), [])
  const [agentId, setAgentId] = useState('')
  const [number, setNumber] = useState('')
  const [busy, setBusy] = useState(false)
  const [dialError, setDialError] = useState('')
  const [clearing, setClearing] = useState(false)
  useEffect(() => {
    const timer = window.setInterval(() => { void refresh() }, 5000)
    return () => window.clearInterval(timer)
  }, [refresh])
  if (loading || agents.loading) return <Loading/>
  const items = data?.items || []
  const active = data?.active || []
  const sipAgents = (agents.data || []).filter(a => a.sip_account_id)
  async function dial(e: FormEvent) {
    e.preventDefault(); setBusy(true); setDialError('')
    try {
      await api.sip.dial({ agent_id: agentId, number })
      await refresh()
      setNumber('')
    } catch (err) {
      setDialError(err instanceof Error ? err.message : 'Не удалось позвонить')
    } finally { setBusy(false) }
  }
  return <>
    {(error || dialError) && <Alert message={error || dialError}/>}
    <SectionHead title="SIP-звонки" text="Исходящие и входящие звонки агентов через OpenAI Realtime" action={<div className="head-actions"><button className="danger" onClick={() => setClearing(true)}><Trash2 size={16}/>Удалить всё</button><button className="secondary" onClick={refresh}><RefreshCw size={16}/>Обновить</button></div>}/>
    <section className="panel">
      <SectionHead title="Новый звонок" text="Агент должен иметь SIP-аккаунт, инструмент sip и профиль OpenAI"/>
      <form onSubmit={dial} className="form-grid">
        <Field label="Агент"><select required value={agentId} onChange={e => setAgentId(e.target.value)}><option value="">Выберите агента</option>{sipAgents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></Field>
        <Field label="Номер"><input required value={number} onChange={e => setNumber(e.target.value)} placeholder="+79001234567"/></Field>
        <div className="modal-actions" style={{ gridColumn: '1 / -1' }}><button className="primary" disabled={busy || !agentId}>{busy ? <LoaderCircle className="spin" size={16}/> : <PhoneCall size={16}/>}Позвонить</button></div>
      </form>
    </section>
    {active.length > 0 && <section className="panel">
      <SectionHead title={`Активные (${active.length})`} text="Живые сессии SIP UA"/>
      <div className="card-grid">{active.map((call, idx) => {
        const id = String(call.db_id || call.sip_call_id || idx)
        return <article className="entity-card" key={id}>
          <div className="entity-top"><span className="entity-avatar"><PhoneCall/></span><StatusDot status="online"/></div>
          <h3>{String(call.remote_number || '—')}</h3>
          <p>{String(call.direction || '')} · {String(call.status || '')}</p>
          <div className="card-actions"><button className="secondary" onClick={async () => { await api.sip.hangup(id); await refresh() }}><PhoneOff size={15}/>Сбросить</button></div>
        </article>
      })}</div>
    </section>}
    <section className="panel">
      <SectionHead title={`История (${items.length})`} text="Транскрипты Realtime сохраняются в карточке звонка"/>
      {items.length === 0 ? <Empty icon={PhoneCall} title="Звонков пока нет" text="Исходящие и входящие появятся здесь."/> :
        <div className="log-view">{items.map((call: SipCall) => <div className="log-row" key={call.id}>
          <time>{call.started_at ? new Date(call.started_at).toLocaleString() : new Date(call.created_at || Date.now()).toLocaleString()}</time>
          <span className={`log-level ${call.status === 'ended' ? 'info' : call.status === 'failed' ? 'error' : 'warning'}`}>{call.status}</span>
          <strong>{call.direction} · {call.remote_number}</strong>
          <p>{call.transcript ? call.transcript.slice(0, 240) : (call.hangup_cause || 'без транскрипта')}</p>
          {['dialing', 'ringing', 'answered', 'early'].includes(call.status) &&
            <button className="secondary compact" onClick={async () => { await api.sip.hangup(call.id); setData(await api.sip.calls(false)) }}><PhoneOff size={14}/>Hangup</button>}
        </div>)}</div>}
    </section>
    {clearing && <ConfirmClearJournals onClose={() => setClearing(false)} onCleared={() => void refresh()}/>}
  </>
}

function TelegramScreen() {
  const { data = [], setData, loading, error } = useLoad(api.telegram.list, [])
  const [flow, setFlow] = useState<'details' | 'code' | null>(null)
  const [name, setName] = useState(''); const [phone, setPhone] = useState(''); const [code, setCode] = useState(''); const [password, setPassword] = useState('')
  const [apiId, setApiId] = useState(''); const [apiHash, setApiHash] = useState('')
  const [httpProxy, setHttpProxy] = useState(''); const [mtHost, setMtHost] = useState(''); const [mtPort, setMtPort] = useState('443'); const [mtDc, setMtDc] = useState('')
  const [session, setSession] = useState(''); const [busy, setBusy] = useState(false); const [flowError, setFlowError] = useState('')
  const [deleting, setDeleting] = useState<TelegramAccount | null>(null)
  const [editingProxy, setEditingProxy] = useState<TelegramAccount | null>(null)
  if (loading) return <Loading/>
  function resetNetwork() { setHttpProxy(''); setMtHost(''); setMtPort('443'); setMtDc('') }
  function networkHint(a: TelegramAccount) {
    const parts: string[] = []
    if (a.http_proxy) parts.push(`HTTP ${a.http_proxy.replace(/\/\/[^@]+@/, '//***@')}`)
    if (a.mtproto_host && a.mtproto_dc_id) parts.push(`MTProto DC${a.mtproto_dc_id} ${a.mtproto_host}:${a.mtproto_port || 443}`)
    return parts.length ? parts.join(' · ') : 'без прокси'
  }
  async function start(e: FormEvent) {
    e.preventDefault(); setBusy(true); setFlowError('')
    try {
      const result = await api.telegram.startLogin({
        name, phone, api_id: Number(apiId), api_hash: apiHash,
        http_proxy: httpProxy.trim() || undefined,
        mtproto_host: mtHost.trim() || undefined,
        mtproto_port: mtHost.trim() && mtPort ? Number(mtPort) : undefined,
        mtproto_dc_id: mtHost.trim() && mtDc ? Number(mtDc) : undefined,
      })
      setApiHash(''); setSession(result.session_id); setFlow('code')
    }
    catch (err) { setFlowError(err instanceof Error ? err.message : 'Не удалось отправить код') } finally { setBusy(false) }
  }
  async function verify(e: FormEvent) {
    e.preventDefault(); setBusy(true); setFlowError('')
    try {
      const account = await api.telegram.verifyCode({ session_id: session, code, password: password || undefined })
      setData([...data.filter(a => a.id !== account.id), account]); setFlow(null); setName(''); setPhone(''); setApiId(''); setApiHash(''); setPassword(''); setCode(''); resetNetwork()
    }
    catch (err) { setFlowError(err instanceof Error ? err.message : 'Проверка не удалась') } finally { setBusy(false) }
  }
  return <>
    {error && <Alert message={error}/>}
    <SectionHead title={`${data.length} аккаунтов Telegram`} text="Пользовательские сессии для агентов" action={<button className="primary" onClick={() => setFlow('details')}><Plus size={17}/>Подключить аккаунт</button>}/>
    {data.length === 0 ? <Empty icon={MessageCircle} title="Нет аккаунтов Telegram" text="Подключите аккаунт по номеру телефона и коду подтверждения."/> :
      <div className="list-panel">{data.map(a => <div className="account-row" key={a.id}><span className="entity-avatar telegram"><MessageCircle/></span><div className="grow"><strong>{a.name}</strong><small>{a.username ? `@${a.username}` : a.phone} · API ID {a.api_id} · {a.has_api_hash ? 'хеш приложения защищён' : 'хеш приложения отсутствует'} · {networkHint(a)}</small></div><StatusDot status={(a.readiness === 'ready' ? 'online' : a.readiness ? 'pending' : a.status) as Status}/><button className="secondary compact" onClick={() => setEditingProxy(a)}>Сеть</button><button className="icon-button danger-ghost" onClick={() => setDeleting(a)}><Trash2 size={17}/></button></div>)}</div>}
    {flow && <Modal title={flow === 'details' ? 'Подключение Telegram' : 'Введите код подтверждения'} subtitle={flow === 'details' ? 'Мы отправим код входа в приложение Telegram.' : `Код отправлен на ${phone}`} onClose={() => { setApiHash(''); setPassword(''); setFlow(null) }}>
      {flowError && <Alert message={flowError}/>}
      {flow === 'details' ? <form onSubmit={start}><div className="notice wide"><KeyRound size={16}/><span>Создайте приложение на <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org</a>, затем введите его API ID и hash. Hash отправляется один раз и больше не отображается.</span></div><div className="form-grid"><Field label="Имя аккаунта"><input required value={name} onChange={e => setName(e.target.value)} placeholder="Аккаунт поддержки"/></Field><Field label="Номер телефона" hint="Укажите международный код страны"><input required value={phone} onChange={e => setPhone(e.target.value)} placeholder="+1 555 000 0000"/></Field><Field label="Telegram API ID"><input required min="1" inputMode="numeric" type="number" value={apiId} onChange={e => setApiId(e.target.value)} placeholder="12345678"/></Field><Field label="Telegram API hash"><input required autoComplete="new-password" type="password" value={apiHash} onChange={e => setApiHash(e.target.value)} placeholder="32-символьный хеш приложения"/></Field><Field label="HTTP-прокси" hint="Необязательно. Пример: http://user:pass@host:8080" wide><input value={httpProxy} onChange={e => setHttpProxy(e.target.value)} placeholder="http://127.0.0.1:8080" autoComplete="off"/></Field><Field label="MTProto host" hint="Необязательно — адрес DC Telegram"><input value={mtHost} onChange={e => setMtHost(e.target.value)} placeholder="149.154.167.50" autoComplete="off"/></Field><Field label="MTProto DC ID"><input required={Boolean(mtHost.trim())} inputMode="numeric" type="number" min="1" max="5" value={mtDc} onChange={e => setMtDc(e.target.value)} placeholder="2" disabled={!mtHost.trim()}/></Field><Field label="MTProto порт"><input inputMode="numeric" type="number" min="1" max="65535" value={mtPort} onChange={e => setMtPort(e.target.value)} placeholder="443" disabled={!mtHost.trim()}/></Field></div><div className="modal-actions"><button type="button" className="secondary" onClick={() => { setApiHash(''); setFlow(null) }}>Отмена</button><button className="primary" disabled={busy}>Отправить код</button></div></form> :
      <form onSubmit={verify}><div className="form-grid"><Field label="Код подтверждения"><input autoFocus required value={code} onChange={e => setCode(e.target.value)} placeholder="12345"/></Field><Field label="Пароль 2FA" hint="Только если включено на аккаунте"><input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Необязательно"/></Field></div><div className="modal-actions"><button type="button" className="secondary" onClick={() => setFlow('details')}>Назад</button><button className="primary" disabled={busy}>Подтвердить и подключить</button></div></form>}
    </Modal>}
    {editingProxy && <TelegramProxyForm account={editingProxy} onClose={() => setEditingProxy(null)} onSave={async payload => {
      const saved = await api.telegram.updateProxy(editingProxy.id, payload)
      setData(data.map(a => a.id === saved.id ? saved : a)); setEditingProxy(null)
    }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.telegram.remove(deleting.id); setData(data.filter(a => a.id !== deleting.id)) }}/>}
  </>
}

function TelegramProxyForm({ account, onClose, onSave }: {
  account: TelegramAccount
  onClose: () => void
  onSave: (payload: {
    http_proxy?: string | null
    mtproto_host?: string | null
    mtproto_port?: number | null
    mtproto_dc_id?: number | null
    clear_proxy?: boolean
  }) => Promise<void>
}) {
  const [httpProxy, setHttpProxy] = useState(account.http_proxy || '')
  const [mtHost, setMtHost] = useState(account.mtproto_host || '')
  const [mtPort, setMtPort] = useState(String(account.mtproto_port || 443))
  const [mtDc, setMtDc] = useState(account.mtproto_dc_id ? String(account.mtproto_dc_id) : '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  return <Modal title="Сеть Telegram" subtitle={`${account.name} · ${account.phone}`} onClose={onClose}>
    <form onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError('')
      try {
        const proxy = httpProxy.trim()
        const host = mtHost.trim()
        if (!proxy && !host) await onSave({ clear_proxy: true })
        else await onSave({
          http_proxy: proxy || null,
          mtproto_host: host || null,
          mtproto_port: host ? Number(mtPort || 443) : null,
          mtproto_dc_id: host && mtDc ? Number(mtDc) : null,
        })
      } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось сохранить настройки сети'); setBusy(false) }
    }}>
      {error && <Alert message={error}/>}
      <div className="form-grid">
        <Field label="HTTP-прокси" hint="Одна строка. Пример: http://user:pass@host:8080. Пусто — без HTTP-прокси" wide><input value={httpProxy} onChange={e => setHttpProxy(e.target.value)} placeholder="http://127.0.0.1:8080" autoComplete="off"/></Field>
        <Field label="MTProto host" hint="Адрес DC Telegram. Пусто — DC по умолчанию"><input value={mtHost} onChange={e => setMtHost(e.target.value)} placeholder="149.154.167.50" autoComplete="off"/></Field>
        <Field label="MTProto DC ID"><input required={Boolean(mtHost.trim())} inputMode="numeric" type="number" min="1" max="5" value={mtDc} onChange={e => setMtDc(e.target.value)} placeholder="2" disabled={!mtHost.trim()}/></Field>
        <Field label="MTProto порт"><input inputMode="numeric" type="number" min="1" max="65535" value={mtPort} onChange={e => setMtPort(e.target.value)} placeholder="443" disabled={!mtHost.trim()}/></Field>
      </div>
      <div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Отмена</button><button className="primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16}/>}Сохранить</button></div>
    </form>
  </Modal>
}

function exactDate(value?: string | null) {
  if (!value) return 'Нет сообщений'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' })
}

function relativeDate(value?: string | null) {
  if (!value) return 'никогда'
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return 'неизвестно'
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000))
  if (seconds < 60) return `${seconds} с назад`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} мин назад`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} ч назад`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} д назад`
  const months = Math.floor(days / 30)
  return months < 12 ? `${months} мес назад` : `${Math.floor(months / 12)} г назад`
}

function ConversationDetailModal({ id, agentName, onClose, onClear }: {
  id: string; agentName: string; onClose: () => void; onClear: (conversation: Conversation) => void
}) {
  const loaded = useLoad(() => api.conversations.get(id), [id])
  const conversation = loaded.data
  const messages = useMemo(() => [...(conversation?.messages || [])].sort((a, b) =>
    new Date(a.message_at || a.created_at).getTime() - new Date(b.message_at || b.created_at).getTime()
  ), [conversation])
  return <Modal title="Детали диалога" subtitle={`${agentName} · Диалог ${id}`} onClose={onClose}>
    {loaded.error && <><Alert message={loaded.error}/><button className="secondary" onClick={loaded.refresh}><RefreshCw size={15}/>Повторить</button></>}
    {loaded.loading && !conversation ? <Loading/> : conversation && <>
      <div className="conversation-facts">
        <span><small>Пользователь</small><strong>{conversation.user_id}</strong></span>
        <span><small>Чат</small><strong>{conversation.chat_id}</strong></span>
        <span><small>Сообщения</small><strong>{conversation.message_count}</strong></span>
        <span><small>Последняя активность</small><strong>{exactDate(conversation.last_message_at)}</strong></span>
      </div>
      <section className="summary-box">
        <span>Сводка</span>
        <p>{conversation.rolling_summary || 'Сводка ещё не сформирована.'}</p>
      </section>
      <div className="transcript-head"><h3>Недавний транскрипт</h3><span>{messages.length} сообщений · хронологически</span></div>
      <div className="transcript">
        {messages.length === 0 ? <p className="transcript-empty">Недавних сообщений нет.</p> : messages.map(message => {
          const user = ['user', 'incoming', 'inbound'].includes(message.direction.toLowerCase())
          return <article className={`transcript-message ${user ? 'user' : 'agent'}`} key={message.id}>
            <div><strong>{user ? 'Пользователь' : 'Агент'}</strong><time>{exactDate(message.message_at || message.created_at)}</time></div>
            <p>{message.text}</p>
          </article>
        })}
      </div>
      <div className="modal-actions"><button className="danger" onClick={() => onClear(conversation)}><Trash2 size={16}/>Очистить диалог</button><button className="secondary" onClick={onClose}>Закрыть</button></div>
    </>}
  </Modal>
}

function ClearConversationModal({ conversation, onClose, onCleared }: {
  conversation: Conversation; onClose: () => void; onCleared: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  return <Modal title="Очистить диалог?" subtitle="Это удалит сохранённый транскрипт и контекст. Действие необратимо." onClose={onClose}>
    {error && <Alert message={error}/>}
    <div className="clear-context"><MessagesSquare size={22}/><div><strong>Пользователь {conversation.user_id}</strong><small>Чат {conversation.chat_id} · {conversation.message_count} сообщений</small></div></div>
    <div className="modal-actions"><button className="secondary" onClick={onClose}>Отмена</button><button className="danger" disabled={busy} onClick={async () => {
      setBusy(true); setError('')
      try { await api.conversations.clear(conversation.id); onCleared() }
      catch (err) { setError(err instanceof Error ? err.message : 'Не удалось очистить диалог'); setBusy(false) }
    }}>{busy ? <LoaderCircle className="spin" size={16}/> : <Trash2 size={16}/>}Очистить диалог</button></div>
  </Modal>
}

function ConversationsScreen() {
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [agentId, setAgentId] = useState('')
  const loaded = useLoad(() => api.conversations.list(agentId || undefined, query || undefined), [agentId, query])
  const agents = useLoad(api.agents.list, [])
  const items = Array.isArray(loaded.data) ? loaded.data : loaded.data?.items || []
  const total = Array.isArray(loaded.data) ? loaded.data.length : loaded.data?.total ?? items.length
  const [selected, setSelected] = useState<Conversation | null>(null)
  const [clearing, setClearing] = useState<Conversation | null>(null)
  const [clearingAll, setClearingAll] = useState(false)
  useEffect(() => {
    const interval = window.setInterval(() => void loaded.refresh(), 30_000)
    return () => window.clearInterval(interval)
  }, [loaded.refresh])
  const agentName = (id: string) => agents.data?.find(agent => String(agent.id) === String(id))?.name || id
  return <>
    <SectionHead title={`${total} диалогов`} text={total > items.length ? `Показаны последние ${items.length}; обновление каждые 30 секунд` : 'Контекст обновляется каждые 30 секунд'} action={<div className="head-actions"><button className="danger" onClick={() => setClearingAll(true)}><Trash2 size={16}/>Удалить всё</button><button className="secondary compact" disabled={loaded.loading} onClick={loaded.refresh}><RefreshCw className={loaded.loading ? 'spin' : ''} size={15}/>Обновить</button></div>}/>
    <form className="filter-bar conversation-filters" onSubmit={event => { event.preventDefault(); setQuery(search.trim()) }}>
      <div className="search-box"><Search size={17}/><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Поиск по пользователю, чату или сводке…"/></div>
      <select aria-label="Фильтр по агенту" value={agentId} onChange={event => setAgentId(event.target.value)}><option value="">Все агенты</option>{agents.data?.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select>
      <button className="secondary">Поиск</button>
      {(query || agentId) && <button type="button" className="secondary" onClick={() => { setSearch(''); setQuery(''); setAgentId('') }}>Сбросить</button>}
    </form>
    {(loaded.error || agents.error) && <Alert message={loaded.error || agents.error}/>}
    {loaded.loading && !loaded.data ? <Loading/> : items.length === 0 ? <Empty icon={MessagesSquare} title="Диалоги не найдены" text="Контекст диалогов появится после обмена сообщениями агентов."/> :
      <div className="conversation-list">{items.map(conversation => <button className="conversation-card" key={conversation.id} onClick={() => setSelected(conversation)}>
        <div className="conversation-primary"><span className="entity-avatar"><MessagesSquare/></span><div><strong>{agentName(conversation.agent_id)}</strong><small>Агент {conversation.agent_id}</small></div></div>
        <div className="conversation-identity"><span><small>Пользователь</small><strong>{conversation.user_id}</strong></span><span><small>Чат</small><strong>{conversation.chat_id}</strong></span><span><small>Аккаунт</small><strong>{conversation.account_id}</strong></span></div>
        <div className="conversation-summary"><span>{conversation.rolling_summary || 'Сводки пока нет.'}</span></div>
        <div className="conversation-activity"><strong>{conversation.message_count}</strong><small>сообщений</small><time>{exactDate(conversation.last_message_at)}</time><span>{relativeDate(conversation.last_message_at)}</span><ChevronRight size={17}/></div>
      </button>)}</div>}
    {selected && <ConversationDetailModal id={selected.id} agentName={agentName(selected.agent_id)} onClose={() => setSelected(null)} onClear={conversation => setClearing(conversation)}/>}
    {clearing && <ClearConversationModal conversation={clearing} onClose={() => setClearing(null)} onCleared={() => { setClearing(null); setSelected(null); void loaded.refresh() }}/>}
    {clearingAll && <ConfirmClearJournals agentId={agentId || undefined} agentName={agentId ? agentName(agentId) : undefined} onClose={() => setClearingAll(false)} onCleared={() => { setClearingAll(false); setSelected(null); void loaded.refresh() }}/>}
  </>
}

function EmployeeScreen() {
  const agents = useLoad(api.agents.list, [])
  const overview = useLoad(api.employees.list, [])
  const consults = useLoad(() => api.consultations.list(undefined, 'open'), [])
  const [agentId, setAgentId] = useState('')
  const [state, setState] = useState<EmployeeState | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, string>>({})
  const [sections, setSections] = useState<Record<string, string>>({})
  const [mission, setMission] = useState('')
  const [roleTitle, setRoleTitle] = useState('')
  const [heartbeat, setHeartbeat] = useState(15)
  const [workStart, setWorkStart] = useState('09:00')
  const [workEnd, setWorkEnd] = useState('18:00')
  const [timezone, setTimezone] = useState('UTC')
  const [budget, setBudget] = useState(48)
  const [autonomy, setAutonomy] = useState(false)

  useEffect(() => {
    if (!agentId && agents.data?.length) setAgentId(String(agents.data[0].id))
  }, [agents.data, agentId])

  async function load(id: string) {
    if (!id) return
    setLoading(true); setError('')
    try {
      const data = await api.agents.employee(id)
      setState(data)
      setSections(data.prompt_sections || {})
      setMission(data.profile.mission || '')
      setRoleTitle(data.profile.role_title || '')
      setHeartbeat(data.profile.heartbeat_minutes || 15)
      setWorkStart(data.profile.workday_start || '09:00')
      setWorkEnd(data.profile.workday_end || '18:00')
      setTimezone(data.profile.timezone || 'UTC')
      setBudget(data.profile.budget_ticks_per_day || 48)
      setAutonomy(Boolean(data.profile.autonomy_enabled))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось загрузить сотрудника')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (agentId) void load(agentId) }, [agentId])

  async function saveProfile() {
    if (!agentId) return
    setBusy('save'); setError('')
    try {
      const data = await api.agents.updateEmployee(agentId, {
        autonomy_enabled: autonomy,
        heartbeat_minutes: heartbeat,
        workday_start: workStart,
        workday_end: workEnd,
        timezone,
        budget_ticks_per_day: budget,
        role_title: roleTitle,
        mission,
        prompt_sections: sections,
      })
      setState(data)
      await overview.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось сохранить')
    } finally { setBusy('') }
  }

  const openConsults = consults.data?.items || []

  return <>
    <SectionHead
      title="Автономный сотрудник"
      text={`Автономных: ${overview.data?.autonomous_agents ?? 0} · на паузе: ${overview.data?.paused_agents ?? 0} · открытых консультаций: ${overview.data?.open_consultations ?? openConsults.length}`}
      action={<div className="head-actions">
        <select value={agentId} onChange={e => setAgentId(e.target.value)} aria-label="Агент">
          <option value="">Выберите агента</option>
          {(agents.data || []).map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <button className="secondary" disabled={!agentId || loading} onClick={() => void load(agentId)}><RefreshCw size={15}/>Обновить</button>
      </div>}
    />
    {(error || agents.error || overview.error || consults.error) && <Alert message={error || agents.error || overview.error || consults.error}/>}
    {loading && !state ? <Loading/> : !agentId ? <Empty icon={Briefcase} title="Выберите агента" text="Включите автономию и задайте миссию — сотрудник начнёт жить по heartbeat."/> : state && <>
      <section className="panel">
        <SectionHead title={`${state.agent_name}`} text={`Тиков сегодня: ${state.profile.ticks_used_today}/${state.profile.budget_ticks_per_day} · последний тик: ${state.profile.last_tick_at ? new Date(state.profile.last_tick_at).toLocaleString() : '—'}`}
          action={<div className="head-actions">
            <button className="secondary" disabled={!!busy} onClick={async () => {
              setBusy('pause')
              try {
                await api.agents.pauseEmployee(agentId, !state.profile.paused)
                await load(agentId); await overview.refresh()
              } catch (err) { setError(err instanceof Error ? err.message : 'Пауза не удалась') }
              finally { setBusy('') }
            }}>{state.profile.paused ? 'Снять паузу' : 'Пауза'}</button>
            <button className="primary" disabled={!!busy} onClick={async () => {
              setBusy('tick')
              try { await api.agents.tickEmployee(agentId); await load(agentId) }
              catch (err) { setError(err instanceof Error ? err.message : 'Тик не удался') }
              finally { setBusy('') }
            }}>{busy === 'tick' ? <LoaderCircle className="spin" size={15}/> : <Play size={15}/>}Force tick</button>
          </div>}
        />
        <div className="form-grid">
          <div className="toggle-box"><Toggle label="Автономия включена" checked={autonomy} onChange={setAutonomy}/></div>
          <Field label="Должность"><input value={roleTitle} onChange={e => setRoleTitle(e.target.value)} placeholder="Менеджер по продажам"/></Field>
          <Field label="Heartbeat (мин)"><input type="number" min={1} max={120} value={heartbeat} onChange={e => setHeartbeat(Number(e.target.value) || 15)}/></Field>
          <Field label="Бюджет тиков/день"><input type="number" min={1} max={500} value={budget} onChange={e => setBudget(Number(e.target.value) || 48)}/></Field>
          <Field label="Начало дня"><input value={workStart} onChange={e => setWorkStart(e.target.value)} placeholder="09:00"/></Field>
          <Field label="Конец дня"><input value={workEnd} onChange={e => setWorkEnd(e.target.value)} placeholder="18:00"/></Field>
          <Field label="Часовой пояс"><input value={timezone} onChange={e => setTimezone(e.target.value)} placeholder="Asia/Yekaterinburg"/></Field>
          <Field label="Миссия" wide><textarea rows={3} value={mission} onChange={e => setMission(e.target.value)} placeholder="Что сотрудник должен достигать"/></Field>
        </div>
        <div className="form-grid" style={{ marginTop: 12 }}>
          {([
            ['identity', 'Личность', 'Только руководитель'],
            ['role', 'Роль', 'Только руководитель'],
            ['rules', 'Правила', 'Только руководитель'],
            ['skills', 'Навыки', 'Сотрудник может править сам'],
            ['tone', 'Тон общения', 'Сотрудник может править сам'],
            ['self_notes', 'Заметки сотрудника', 'Сотрудник может править сам'],
          ] as const).map(([key, label, hint]) => (
            <Field key={key} label={label} wide hint={hint}>
              <textarea rows={key === 'identity' || key === 'rules' ? 5 : 3} value={sections[key] || ''} onChange={e => setSections(s => ({ ...s, [key]: e.target.value }))}/>
            </Field>
          ))}
        </div>
        <div className="modal-actions"><button className="primary" disabled={!!busy} onClick={() => void saveProfile()}>{busy === 'save' ? <LoaderCircle className="spin" size={15}/> : null}Сохранить сотрудника</button></div>
      </section>

      <section className="panel">
        <SectionHead title={`Планы (${state.plans.length})`} text="Час / день / неделя / месяц"/>
        {state.plans.length === 0 ? <Empty icon={CalendarClock} title="Планов пока нет" text="Появятся на первом тике или когда сотрудник создаст их сам."/> :
          <div className="card-grid">{state.plans.map(plan => <article className="entity-card" key={plan.id}>
            <div className="entity-top"><span className="chip">{plan.horizon}</span><StatusDot status={plan.status === 'active' ? 'online' : plan.status === 'done' ? 'paused' : 'pending'}/></div>
            <h3>{plan.title || `План #${plan.id}`}</h3>
            <p style={{ whiteSpace: 'pre-wrap' }}>{(plan.body?.steps || []).map(s => `${s.status === 'done' ? '✓' : '·'} ${s.title}`).join('\n') || 'Шагов нет'}</p>
          </article>)}</div>}
      </section>

      <section className="panel">
        <SectionHead title={`Потребности (${state.needs.length})`}/>
        {state.needs.length === 0 ? <p className="transcript-empty">Открытых потребностей нет.</p> :
          <div className="list-panel">{state.needs.map(need => <div className="server-row" key={need.id}>
            <span className="chip">{need.kind}</span>
            <div className="grow"><strong>{need.title}</strong><small>{need.status} · p={need.priority} · {need.detail.slice(0, 160)}</small></div>
          </div>)}</div>}
      </section>

      <section className="panel">
        <SectionHead title={`Консультации (${openConsults.length} открытых)`} text="Ответы также в Telegram: /answer id · /approve id · /reject id"/>
        {openConsults.length === 0 ? <Empty icon={MessageCircle} title="Очередь пуста" text="Когда сотруднику что-то нужно — запрос появится здесь и у админов в Telegram."/> :
          <div className="list-panel">{openConsults.map((item: Consultation) => <div className="server-row" key={item.id} style={{ alignItems: 'flex-start', paddingTop: 12, paddingBottom: 12 }}>
            <span className="chip">{item.requires_approval ? 'approval' : 'consult'}</span>
            <div className="grow">
              <strong>#{item.id} · агент {item.agent_id}</strong>
              <small>{item.question}</small>
              {item.context && <small>{item.context.slice(0, 240)}</small>}
              <textarea rows={2} style={{ marginTop: 8, width: '100%' }} placeholder="Ответ руководителя…" value={answerDrafts[item.id] || ''} onChange={e => setAnswerDrafts(d => ({ ...d, [item.id]: e.target.value }))}/>
              <div className="head-actions" style={{ marginTop: 8 }}>
                <button className="secondary compact" onClick={async () => { await api.consultations.resolve(item.id, { status: 'answered', answer_text: answerDrafts[item.id] || '' }); await consults.refresh(); await load(agentId) }}>Ответить</button>
                {item.requires_approval && <>
                  <button className="primary compact" onClick={async () => { await api.consultations.resolve(item.id, { status: 'approved', answer_text: answerDrafts[item.id] || 'approved' }); await consults.refresh(); await load(agentId) }}>Одобрить</button>
                  <button className="danger compact" onClick={async () => { await api.consultations.resolve(item.id, { status: 'rejected', answer_text: answerDrafts[item.id] || 'rejected' }); await consults.refresh(); await load(agentId) }}>Отклонить</button>
                </>}
              </div>
            </div>
          </div>)}</div>}
      </section>
    </>}
  </>
}

function MemoryScreen() {
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('')
  const loaded = useLoad(() => api.memory.list(query), [query])
  const items = Array.isArray(loaded.data) ? loaded.data : loaded.data?.items || []
  const [deleting, setDeleting] = useState<MemoryItem | null>(null)
  const [migrateMsg, setMigrateMsg] = useState('')
  const [migrating, setMigrating] = useState(false)
  const [clearing, setClearing] = useState(false)
  async function migrate() {
    setMigrating(true); setMigrateMsg('')
    try {
      const result = await api.memory.migrate()
      setMigrateMsg(`Перенесено: ${result.migrated}, ошибок: ${result.failed}, осталось в RAM: ${result.remaining}`)
      await loaded.refresh()
    } catch (err) {
      setMigrateMsg(err instanceof Error ? err.message : 'Не удалось перенести память')
    } finally {
      setMigrating(false)
    }
  }
  return <>
    <SectionHead title="Сохранённый контекст" text="Поиск семантической и структурированной памяти агентов" action={<div className="head-actions"><button className="danger" onClick={() => setClearing(true)}><Trash2 size={16}/>Удалить всё</button><button className="secondary" disabled={migrating} onClick={() => void migrate()}>{migrating ? <LoaderCircle className="spin" size={16}/> : null}Перенести RAM → Qdrant</button></div>}/>
    {migrateMsg && <Alert message={migrateMsg}/>}
    <form className="filter-bar" onSubmit={e => { e.preventDefault(); setQuery(search) }}><div className="search-box"><Search size={17}/><input value={search} onChange={e => setSearch(e.target.value)} placeholder="Поиск по содержимому, ключам или области…"/></div><button className="secondary">Поиск</button></form>
    {loaded.error && <Alert message={loaded.error}/>}
    {loaded.loading ? <Loading/> : items.length === 0 ? <Empty icon={BrainCircuit} title="Подходящих записей нет" text="Записи памяти агентов появятся по мере создания."/> :
      <div className="memory-list">{items.map(item => <article className="memory-card" key={item.id}><div className="memory-head"><div><span className="scope">{item.scope}</span><strong>{item.key}</strong></div><button className="icon-button danger-ghost" onClick={() => setDeleting(item)}><Trash2 size={16}/></button></div><p>{item.content}</p><div className="entity-meta"><span>{item.agent_id ? `Агент ${item.agent_id}` : 'Глобально'}</span>{item.created_at && <time>{new Date(item.created_at).toLocaleString()}</time>}</div></article>)}</div>}
    {deleting && <ConfirmDelete name={deleting.key} onClose={() => setDeleting(null)} onDelete={async () => { await api.memory.remove(deleting.id); loaded.setData(Array.isArray(loaded.data) ? loaded.data.filter(i => i.id !== deleting.id) : loaded.data ? { ...loaded.data, items: loaded.data.items.filter(i => i.id !== deleting.id) } : loaded.data) }}/>}
    {clearing && <ConfirmClearJournals onClose={() => setClearing(false)} onCleared={() => void loaded.refresh()}/>}
  </>
}

const emptyMcp: Omit<McpServer, 'id'> = { name: '', url: '', transport: 'sse', command: '', args: [], env: {}, enabled: true }

function parseMcpHeaders(value: string): Record<string, string> {
  const trimmed = value.trim()
  if (!trimmed) return {}
  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>
      const servers = parsed.mcpServers as Record<string, { headers?: Record<string, unknown> }> | undefined
      const source = servers
        ? Object.values(servers)[0]?.headers || {}
        : parsed
      return Object.fromEntries(Object.entries(source).map(([key, item]) => [key, String(item)]))
    } catch {
      // Allow partially typed JSON; line parser below keeps the form editable.
    }
  }
  return Object.fromEntries(value.split('\n').map(line => line.trim()).filter(Boolean).map(line => {
    const equals = line.indexOf('=')
    const colon = line.indexOf(':')
    const index = equals >= 0 ? equals : colon
    return index < 0
      ? [line, '']
      : [line.slice(0, index).trim(), line.slice(index + 1).trim()]
  }))
}

function McpScreen() {
  const loaded = useLoad(api.mcp.list, []); const data = loaded.data || []
  const [editing, setEditing] = useState<Partial<McpServer> | null>(null); const [deleting, setDeleting] = useState<McpServer | null>(null)
  if (loaded.loading) return <Loading/>
  return <>
    {loaded.error && <Alert message={loaded.error}/>}<SectionHead title={`${data.length} серверов инструментов`} text="Подключения Model Context Protocol" action={<button className="primary" onClick={() => setEditing(emptyMcp)}><Plus size={17}/>Добавить сервер</button>}/>
    {data.length === 0 ? <Empty icon={ServerCog} title="Нет MCP-серверов" text="Подключите удалённый SSE/HTTP-сервер или локальный stdio-процесс."/> :
    <div className="list-panel">{data.map(server => <div className="server-row" key={server.id}><span className="entity-avatar"><ServerCog/></span><div className="grow"><strong>{server.name}</strong><small>{server.transport === 'stdio' ? server.command : server.url}{server.connection_error ? ` · ${server.connection_error}` : ''}</small></div><span className="chip">{server.transport}</span><StatusDot status={server.connection_status === 'connected' ? 'online' : server.connection_status === 'error' ? 'error' : server.enabled ? 'pending' : 'paused'}/><button className="secondary compact" onClick={() => setEditing(server)}>Изменить</button><button className="icon-button danger-ghost" onClick={() => setDeleting(server)}><Trash2 size={17}/></button></div>)}</div>}
    {editing && <McpForm value={editing} onClose={() => setEditing(null)} onSave={async v => {
      const payload = {
        name: v.name || '',
        transport: v.transport || 'sse',
        command: v.command || '',
        args: v.args || [],
        url: v.url || '',
        enabled: v.enabled ?? true,
        ...(v.env && Object.keys(v.env).length ? { env: v.env } : {}),
      }
      const saved = v.id ? await api.mcp.update(v.id, payload) : await api.mcp.create(payload as Omit<McpServer, 'id'>)
      loaded.setData(v.id ? data.map(s => s.id === saved.id ? saved : s) : [...data, saved]); setEditing(null)
    }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.mcp.remove(deleting.id); loaded.setData(data.filter(s => s.id !== deleting.id)) }}/>}
  </>
}
function McpForm({ value, onClose, onSave }: { value: Partial<McpServer>; onClose: () => void; onSave: (v: Partial<McpServer>) => Promise<void> }) {
  const [form, setForm] = useState<Partial<McpServer>>({ ...value, env: {} }); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  const patch = (p: Partial<McpServer>) => setForm(f => ({ ...f, ...p }))
  return <Modal title={form.id ? 'Изменение MCP-сервера' : 'Добавление MCP-сервера'} subtitle="Секреты окружения шифруются и только для записи." onClose={onClose}><form onSubmit={async e => { e.preventDefault(); setBusy(true); const payload = { ...form }; if (form.id && Object.keys(form.env || {}).length === 0) delete payload.env; try { await onSave(payload) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось сохранить'); setBusy(false); patch({ env: {} }) } }}>
    {error && <Alert message={error}/>}<div className="form-grid"><Field label="Имя"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Внутренние инструменты"/></Field><Field label="Транспорт"><select value={form.transport} onChange={e => patch({ transport: e.target.value as McpServer['transport'] })}><option value="sse">SSE</option><option value="streamable-http">Streamable HTTP</option><option value="stdio">stdio</option></select></Field>
    {form.transport === 'stdio' ? <><Field label="Команда"><input required value={form.command || ''} onChange={e => patch({ command: e.target.value })} placeholder="npx"/></Field><Field label="Аргументы" hint="По одному на строку"><textarea rows={3} value={(form.args || []).join('\n')} onChange={e => patch({ args: e.target.value.split('\n').filter(Boolean) })}/></Field></> : <Field label="URL сервера" wide><input required type="url" value={form.url || ''} onChange={e => patch({ url: e.target.value })} placeholder="https://tools.example.com/mcp"/></Field>}
    <Field label={form.transport === 'stdio' ? 'Переменные окружения' : 'HTTP-заголовки'} hint={form.transport === 'stdio' ? (form.id ? 'Шифруются при хранении. Оставьте пустым для сохранения значений; секреты не подставляются обратно.' : 'KEY=value, по одному на строку. Шифруются при хранении.') : (form.id ? 'Формат KEY=value, Header: value или JSON. Оставьте пустым, чтобы сохранить.' : 'Формат KEY=value, Header: value или JSON. Пример:\nAuthorization=Bearer ict_mcp_…')} wide><textarea autoComplete="off" rows={4} value={Object.entries(form.env || {}).map(([k, v]) => `${k}=${v}`).join('\n')} onChange={e => patch({ env: parseMcpHeaders(e.target.value) })} placeholder={form.transport === 'stdio' ? 'KEY=value' : 'Authorization=Bearer ict_mcp_…'}/></Field><div className="toggle-box wide"><Toggle label="Сервер включён" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div></div>
    <div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Отмена</button><button className="primary" disabled={busy}>Сохранить сервер</button></div></form></Modal>
}

const emptyCron: Omit<CronJob, 'id'> = { name: '', agent_id: '', schedule: '0 9 * * *', prompt: '', timezone: 'UTC', enabled: true }
function cronDateTimeLocal(value?: string, timezone = 'UTC') {
  if (!value) return ''
  if (!/[zZ]|[+-]\d{2}:\d{2}$/.test(value)) return value.slice(0, 16)
  try {
    return new Intl.DateTimeFormat('sv-SE', {
      timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(new Date(value)).replace(' ', 'T')
  } catch {
    return value.slice(0, 16)
  }
}
function cronScheduleLabel(job: CronJob) {
  if (!job.run_once_at) return `${job.schedule} · ${job.timezone}`
  try {
    return `Однократно: ${new Date(job.run_once_at).toLocaleString('ru-RU', { timeZone: job.timezone })} · ${job.timezone}`
  } catch {
    return `Однократно: ${job.run_once_at} · ${job.timezone}`
  }
}
function CronScreen() {
  const jobs = useLoad(api.cron.list, []); const agents = useLoad(api.agents.list, []); const data = jobs.data || []
  const [editing, setEditing] = useState<Partial<CronJob> | null>(null); const [deleting, setDeleting] = useState<CronJob | null>(null)
  if (jobs.loading || agents.loading) return <Loading/>
  return <>
    {(jobs.error || agents.error) && <Alert message={jobs.error || agents.error}/>}<SectionHead title={`${data.length} расписаний`} text="Автономные задачи по cron" action={<button className="primary" onClick={() => setEditing({ ...emptyCron, agent_id: agents.data?.[0]?.id || '' })}><Plus size={17}/>Новое расписание</button>}/>
    {data.length === 0 ? <Empty icon={CalendarClock} title="Нет запланированных задач" text="Запланируйте повторяющиеся промпты для любого настроенного агента."/> :
    <div className="list-panel">{data.map(job => <div className="server-row" key={job.id}><span className="entity-avatar amber"><CalendarClock/></span><div className="grow"><strong>{job.name}</strong><small>{cronScheduleLabel(job)}</small></div><span className="chip">{agents.data?.find(a => a.id === job.agent_id)?.name || job.agent_id}</span><StatusDot status={job.enabled ? (job.status || 'active') : 'paused'}/><button className="secondary compact" onClick={() => setEditing(job)}>Изменить</button><button className="icon-button danger-ghost" onClick={() => setDeleting(job)}><Trash2 size={17}/></button></div>)}</div>}
    {editing && <CronForm value={editing} agents={agents.data || []} onClose={() => setEditing(null)} onSave={async v => { const saved = v.id ? await api.cron.update(v.id, v) : await api.cron.create(v as Omit<CronJob, 'id'>); jobs.setData(v.id ? data.map(j => j.id === saved.id ? saved : j) : [...data, saved]); setEditing(null) }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.cron.remove(deleting.id); jobs.setData(data.filter(j => j.id !== deleting.id)) }}/>}
  </>
}
function CronForm({ value, agents, onClose, onSave }: { value: Partial<CronJob>; agents: Agent[]; onClose: () => void; onSave: (v: Partial<CronJob>) => Promise<void> }) {
  const [form, setForm] = useState<Partial<CronJob>>(() => ({
    ...value,
    run_once_at: cronDateTimeLocal(value.run_once_at, value.timezone),
  })); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  const patch = (p: Partial<CronJob>) => setForm(f => ({ ...f, ...p }))
  return <Modal title={form.id ? 'Изменение расписания' : 'Новое расписание'} onClose={onClose}><form onSubmit={async e => { e.preventDefault(); setBusy(true); try { await onSave(form) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось сохранить'); setBusy(false) } }}>
    {error && <Alert message={error}/>}<div className="form-grid"><Field label="Имя"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Ежедневная сводка"/></Field><Field label="Агент"><select required value={form.agent_id} onChange={e => patch({ agent_id: e.target.value })}><option value="">Выберите агента</option>{agents.map(a => <option value={a.id} key={a.id}>{a.name}</option>)}</select></Field><Field label="Cron-выражение" hint={form.run_once_at ? 'Не используется для одноразового запуска' : 'минута час день месяц день_недели'}><input required={!form.run_once_at} disabled={Boolean(form.run_once_at)} value={form.run_once_at ? '@once' : form.schedule || ''} onChange={e => patch({ schedule: e.target.value })} placeholder="0 9 * * *"/></Field><Field label="Дата и время одноразового запуска" hint="Оставьте пустым для обычного cron-расписания"><input type="datetime-local" value={form.run_once_at || ''} onChange={e => patch({ run_once_at: e.target.value || undefined })}/></Field><Field label="Часовой пояс"><input required value={form.timezone || 'UTC'} onChange={e => patch({ timezone: e.target.value })} placeholder="Asia/Yekaterinburg"/></Field><Field label="Промпт" wide><textarea required rows={6} value={form.prompt || ''} onChange={e => patch({ prompt: e.target.value })} placeholder="Сформируйте и отправьте ежедневную сводку…"/></Field><div className="toggle-box wide"><Toggle label="Расписание включено" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div></div>
    <div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Отмена</button><button className="primary" disabled={busy}>Сохранить расписание</button></div></form></Modal>
}

function RuntimeScreen() {
  const loaded = useLoad(api.settings.runtime, []); const profiles = useLoad(api.llmProfiles.list, [])
  const [form, setForm] = useState<RuntimeSettings>(); const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false); const [error, setError] = useState(''); const [searchResult, setSearchResult] = useState('')
  const [clearing, setClearing] = useState(false)
  useEffect(() => { if (loaded.data) setForm({
    ...loaded.data,
    timezone: loaded.data.timezone ?? 'UTC',
    telegram_history_limit: loaded.data.telegram_history_limit ?? 50,
    recent_context_messages: loaded.data.recent_context_messages ?? 20,
    context_max_chars: loaded.data.context_max_chars ?? 32000,
    summarization_enabled: loaded.data.summarization_enabled ?? true,
    summarize_after_messages: loaded.data.summarize_after_messages ?? 30,
    mem0_api_key: '',
    tavily_api_key: '',
  }) }, [loaded.data])
  if (loaded.loading || profiles.loading || !form) return <Loading/>
  const patch = (p: Partial<RuntimeSettings>) => setForm(f => f ? ({ ...f, ...p }) : f)
  const number = (key: keyof RuntimeSettings, value: string) => patch({ [key]: Number(value) } as Partial<RuntimeSettings>)
  async function save(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError(''); setSaved(false)
    const payload: RuntimeSettings = { ...(form as RuntimeSettings) }
    if (!payload.mem0_api_key) delete payload.mem0_api_key
    if (!payload.tavily_api_key) delete payload.tavily_api_key
    try { const result = await api.settings.updateRuntime(payload); setForm({ ...result, mem0_api_key: '', tavily_api_key: '' }); setSaved(true); setTimeout(() => setSaved(false), 2500) }
    catch (err) { setError(err instanceof Error ? err.message : 'Не удалось сохранить настройки runtime'); patch({ mem0_api_key: '', tavily_api_key: '' }) }
    finally { setBusy(false) }
  }
  async function testSearch() {
    setSearchResult('Тестирование…')
    try { const result = await api.settings.testSearch(); setSearchResult(result.message || result.detail || (result.ok === false ? 'Тест поиска не пройден' : 'Подключение к поиску успешно')) }
    catch (err) { setSearchResult(err instanceof Error ? err.message : 'Тест поиска не пройден') }
  }
  return <form className="settings-layout runtime-layout" onSubmit={save}>
    {(loaded.error || profiles.error || error) && <Alert message={loaded.error || profiles.error || error}/>}
    <section className="panel"><SectionHead title="Веб-поиск" text="Основной поиск для агентов. Открытие страниц — через MCP (Playwright), если подключён." action={<button type="button" className="secondary compact" onClick={() => void testSearch()}><Wifi size={14}/>Тест поиска</button>}/>
      <div className="form-grid">
        <Field label="Провайдер поиска"><select required value={form.search_provider} onChange={e => patch({ search_provider: e.target.value })}><option value="tavily">Tavily</option><option value="searxng">SearXNG</option><option value="ddg">DuckDuckGo</option></select></Field>
        {form.search_provider === 'searxng' && <Field label="URL SearXNG" hint="Из Docker API: http://172.17.0.1:8080 (не localhost)"><input type="url" value={form.searxng_url || ''} onChange={e => patch({ searxng_url: e.target.value || null })} placeholder="http://172.17.0.1:8080"/></Field>}
        {form.search_provider === 'tavily' && <>
          <Field label="API-ключ Tavily" hint={form.has_tavily_api_key ? 'Настроен · ••••••••. Оставьте пустым для сохранения.' : 'Ключ с https://tavily.com. Шифруется в БД.'} wide><input autoComplete="new-password" type="password" value={form.tavily_api_key || ''} onChange={e => patch({ tavily_api_key: e.target.value })} placeholder={form.has_tavily_api_key ? 'Оставьте пустым для сохранения' : 'tvly-…'}/></Field>
          <Field label="HTTP-прокси Tavily" hint="Если API отвечает 403 — укажите исходящий прокси. Пример: http://user:pass@host:8080" wide><input value={form.tavily_http_proxy || ''} onChange={e => patch({ tavily_http_proxy: e.target.value || null })} placeholder="http://127.0.0.1:8080" autoComplete="off"/></Field>
        </>}
      </div>
      {searchResult && <div className="inline-result standalone">{searchResult}</div>}
    </section>
    <section className="panel"><SectionHead title="Контекст диалога" text="Управляет недавним транскриптом Telegram и контекстом для агентов"/>
      <div className="form-grid">
        <Field label="Часовой пояс" hint="Часовой пояс IANA для передачи агенту текущей даты и времени (например, Europe/London)."><input required value={form.timezone} onChange={e => patch({ timezone: e.target.value })} placeholder="UTC"/></Field>
        <Field label="Лимит истории Telegram" hint="Максимум сообщений из Telegram при инициализации контекста."><input required min="1" max="500" type="number" value={form.telegram_history_limit} onChange={e => number('telegram_history_limit', e.target.value)}/></Field>
        <Field label="Недавние сообщения контекста" hint="Сколько последних сообщений включать дословно."><input required min="1" max="500" type="number" value={form.recent_context_messages} onChange={e => number('recent_context_messages', e.target.value)}/></Field>
        <Field label="Максимум символов контекста" hint="Лимит символов для собранного контекста диалога."><input required min="1000" max="200000" type="number" value={form.context_max_chars} onChange={e => number('context_max_chars', e.target.value)}/></Field>
        <Field label="Суммаризация после сообщений" hint="Создавать или обновлять сводку после указанного числа сообщений."><input required min="2" max="5000" disabled={!form.summarization_enabled} type="number" value={form.summarize_after_messages} onChange={e => number('summarize_after_messages', e.target.value)}/></Field>
        <div className="toggle-box"><Toggle label="Суммаризация включена" checked={form.summarization_enabled} onChange={v => patch({ summarization_enabled: v })}/></div>
      </div>
    </section>
    <section className="panel"><SectionHead title="Бэкенд памяти" text="Долговременная семантическая память и хранилище эмбеддингов" action={<button type="button" className="danger compact" onClick={() => setClearing(true)}><Trash2 size={15}/>Удалить всё</button>}/>
      <div className="form-grid">
        <Field label="Бэкенд памяти"><select value={form.memory_backend} onChange={e => patch({ memory_backend: e.target.value })}><option value="local">Local Mem0 + Qdrant</option><option value="platform">Mem0 Platform</option></select></Field>
        <Field label="Профиль LLM для памяти" hint="Используется для извлечения фактов; embeddings создаются локально через FastEmbed"><select value={form.memory_llm_profile_id || ''} onChange={e => patch({ memory_llm_profile_id: e.target.value || null })}><option value="">Без отдельного профиля</option>{profiles.data?.map(p => <option key={p.id} value={p.id}>{p.name} · {p.default_model}</option>)}</select></Field>
        <Field label="API-ключ Mem0" hint={form.memory_backend === 'local' ? 'Для Local Mem0 + Qdrant не нужен — только для Mem0 Platform.' : (form.has_mem0_api_key ? 'Настроен · ••••••••. Оставьте пустым для сохранения.' : 'Не настроен. Секрет только для записи.')}><input autoComplete="new-password" type="password" value={form.mem0_api_key || ''} onChange={e => patch({ mem0_api_key: e.target.value })} placeholder={form.memory_backend === 'local' ? 'Не требуется для локального режима' : (form.has_mem0_api_key ? 'Оставьте пустым для сохранения' : 'Введите API-ключ')} disabled={form.memory_backend === 'local'}/></Field>
        <Field label="URL Qdrant" hint="В Docker Compose: http://qdrant:6333"><input type="text" value={form.qdrant_url || ''} onChange={e => patch({ qdrant_url: e.target.value || null })} placeholder="http://qdrant:6333"/></Field>
        <div className="toggle-box wide"><Toggle label="Память включена" checked={form.memory_enabled} onChange={v => patch({ memory_enabled: v })}/></div>
      </div>
      {form.memory_error && <Alert message={`Память degraded: ${form.memory_error}`}/>}
    </section>
    <section className="panel"><SectionHead title="Имитация человеческого общения" text="Присутствие «набирает» и темп исходящих сообщений"/>
      <div className="form-grid"><Field label="Мин. набор (сек)"><input min="0" step=".1" type="number" value={form.typing_min_seconds} onChange={e => number('typing_min_seconds', e.target.value)}/></Field><Field label="Макс. набор (сек)"><input min="0" step=".1" type="number" value={form.typing_max_seconds} onChange={e => number('typing_max_seconds', e.target.value)}/></Field><Field label="Джиттер набора (сек)"><input min="0" step=".1" type="number" value={form.typing_jitter_seconds} onChange={e => number('typing_jitter_seconds', e.target.value)}/></Field><Field label="Размер фрагмента сообщения"><input min="256" max="4096" type="number" value={form.typing_chunk_size} onChange={e => number('typing_chunk_size', e.target.value)}/></Field><div className="toggle-box wide"><Toggle label="Отправлять статус «онлайн» и «набирает»" checked={form.typing_presence} onChange={v => patch({ typing_presence: v })}/></div></div>
    </section>
    <section className="panel"><SectionHead title="Выполнение задач" text="Параллелизм и лимиты цикла инструментов"/><div className="form-grid"><Field label="Воркеры задач"><input required min="1" type="number" value={form.task_workers} onChange={e => number('task_workers', e.target.value)}/></Field><Field label="Максимум раундов инструментов"><input required min="1" type="number" value={form.max_tool_rounds} onChange={e => number('max_tool_rounds', e.target.value)}/></Field></div></section>
    <div className="save-bar"><span>{saved && <><CheckCircle2 size={17}/>Настройки runtime сохранены</>}</span><button className="primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16}/>}Сохранить runtime</button></div>
    {clearing && <ConfirmClearJournals onClose={() => setClearing(false)} onCleared={() => setClearing(false)}/>}
  </form>
}

function SettingsScreen() {
  const loaded = useLoad(api.settings.get, []); const agents = useLoad(api.agents.list, [])
  const [form, setForm] = useState<AdminSettings>(); const [saved, setSaved] = useState(false); const [error, setError] = useState('')
  useEffect(() => { if (loaded.data) setForm(loaded.data) }, [loaded.data])
  if (loaded.loading || !form) return <Loading/>
  return <form className="settings-layout" onSubmit={async e => { e.preventDefault(); setError(''); try { const result = await api.settings.update(form); setForm(result); setSaved(true); setTimeout(() => setSaved(false), 2500) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось сохранить настройки') } }}>
    {error && <Alert message={error}/>}
    <section className="panel"><SectionHead title="Доступ администратора" text="Telegram user ID с правами админ-команд"/>
      <Field label="Telegram ID администраторов" hint="Числовые ID через запятую"><textarea rows={4} value={form.admin_ids.join(', ')} onChange={e => setForm({ ...form, admin_ids: e.target.value.split(',').map(v => v.trim()).filter(Boolean) })} placeholder="123456789, 987654321"/></Field>
    </section>
    <section className="panel"><SectionHead title="Маршрутизация эскалации" text="Куда агенты отправляют запросы, требующие внимания человека"/>
      <div className="form-grid"><Field label="Агент эскалации"><select value={form.escalation_agent_id || ''} onChange={e => setForm({ ...form, escalation_agent_id: e.target.value || undefined })}><option value="">Нет</option>{agents.data?.map(a => <option value={a.id} key={a.id}>{a.name}</option>)}</select></Field><Field label="Chat ID эскалации"><input value={form.escalation_chat_id || ''} onChange={e => setForm({ ...form, escalation_chat_id: e.target.value || undefined })} placeholder="-100123456789"/></Field></div>
    </section>
    <section className="panel"><SectionHead title="Уведомления"/><div className="setting-lines"><Toggle label="Уведомлять администраторов об ошибках агентов" checked={form.notify_on_error} onChange={v => setForm({ ...form, notify_on_error: v })}/><Toggle label="Уведомлять об эскалации к человеку" checked={form.notify_on_escalation} onChange={v => setForm({ ...form, notify_on_escalation: v })}/></div></section>
    <div className="save-bar"><span>{saved && <><CheckCircle2 size={17}/>Настройки сохранены</>}</span><button className="primary">Сохранить изменения</button></div>
  </form>
}

function LiveScreen({ mode }: { mode: 'logs' | 'tasks' }) {
  const logsLoad = useLoad(() => api.logs(), []); const tasksLoad = useLoad(api.tasks, [])
  const [connected, setConnected] = useState(false); const [search, setSearch] = useState(''); const [level, setLevel] = useState('')
  const [logs, setLogs] = useState<LogEntry[]>([]); const [tasks, setTasks] = useState<AgentTask[]>([])
  const [clearing, setClearing] = useState(false)
  useEffect(() => { if (logsLoad.data) setLogs(Array.isArray(logsLoad.data) ? logsLoad.data : logsLoad.data.items) }, [logsLoad.data])
  useEffect(() => { if (tasksLoad.data) setTasks(Array.isArray(tasksLoad.data) ? tasksLoad.data : tasksLoad.data.items) }, [tasksLoad.data])
  useEffect(() => {
    const socket = openLiveSocket(payload => {
      if (!payload || typeof payload !== 'object') return
      const event = payload as { type?: string; data?: LogEntry | AgentTask }
      if (event.type === 'log' && event.data) setLogs(v => [event.data as LogEntry, ...v].slice(0, 500))
      if ((event.type === 'task' || event.type === 'task.updated') && event.data) setTasks(v => {
        const task = event.data as AgentTask; const exists = v.some(t => t.id === task.id)
        return exists ? v.map(t => t.id === task.id ? task : t) : [task, ...v]
      })
    })
    socket.onopen = () => setConnected(true); socket.onclose = () => setConnected(false); socket.onerror = () => setConnected(false)
    return () => socket.close()
  }, [])
  const filteredLogs = useMemo(() => logs.filter(l => (!level || l.level === level) && (!search || `${l.source} ${l.message}`.toLowerCase().includes(search.toLowerCase()))), [logs, level, search])
  const filteredTasks = useMemo(() => tasks.filter(t => !search || `${t.title} ${t.payload || ''}`.toLowerCase().includes(search.toLowerCase())), [tasks, search])
  const loading = mode === 'logs' ? logsLoad.loading : tasksLoad.loading; const error = mode === 'logs' ? logsLoad.error : tasksLoad.error
  return <>
    <SectionHead title={mode === 'logs' ? `${filteredLogs.length} недавних событий` : `${filteredTasks.length} задач`} text={connected ? 'Получение обновлений в реальном времени' : 'Поток недоступен — показаны данные API'} action={<div className="head-actions">{mode === 'logs' && <button className="danger" onClick={() => setClearing(true)}><Trash2 size={16}/>Удалить всё</button>}<span className={`live-pill ${connected ? '' : 'muted'}`}>{connected ? <Wifi size={14}/> : <WifiOff size={14}/>} {connected ? 'Подключено' : 'Офлайн'}</span></div>}/>
    <div className="filter-bar"><div className="search-box"><Search size={17}/><input value={search} onChange={e => setSearch(e.target.value)} placeholder={mode === 'logs' ? 'Поиск по логам…' : 'Поиск по задачам…'}/></div>{mode === 'logs' && <select value={level} onChange={e => setLevel(e.target.value)}><option value="">Все уровни</option><option>debug</option><option>info</option><option>warning</option><option>error</option></select>}</div>
    {error && <Alert message={error}/>}
    {loading ? <Loading/> : mode === 'logs' ? <div className="log-view">{filteredLogs.length === 0 ? <Empty icon={FileText} title="Нет событий логов" text="События runtime появятся здесь."/> : filteredLogs.map(log => <div className="log-row" key={log.id}><time>{new Date(log.timestamp).toLocaleTimeString()}</time><span className={`log-level ${log.level}`}>{log.level}</span><strong>{log.source}</strong><p>{log.message}</p></div>)}</div> :
      <div className="task-board">{(['queued', 'running', 'completed', 'failed'] as AgentTask['status'][]).map(status => <section className="task-column" key={status}><h3><span className={`task-dot ${status}`}/>{taskStatusLabel[status] || status}<small>{filteredTasks.filter(t => t.status === status).length}</small></h3>{filteredTasks.filter(t => t.status === status).map(task => <article className="task-card" key={task.id}><strong>{task.title}</strong>{task.payload && <p>{task.payload}</p>}<div><span>{task.from_agent_id || 'система'} → {task.to_agent_id}</span><time>{new Date(task.created_at).toLocaleString()}</time></div></article>)}</section>)}</div>}
    {clearing && <ConfirmClearJournals onClose={() => setClearing(false)} onCleared={() => { setLogs([]); void logsLoad.refresh() }}/>}
  </>
}

export default function App() {
  const pages = new Set<Page>(nav.map(item => item.id))
  const [authenticated, setAuthenticated] = useState(Boolean(localStorage.getItem('ice_token')))
  const [page, setPage] = useState<Page>(() => {
    const stored = sessionStorage.getItem('ice_page') as Page | null
    return stored && pages.has(stored) ? stored : 'dashboard'
  })
  useEffect(() => { const unauthorized = () => { localStorage.removeItem('ice_token'); setAuthenticated(false) }; window.addEventListener('ice:unauthorized', unauthorized); return () => window.removeEventListener('ice:unauthorized', unauthorized) }, [])
  useEffect(() => { sessionStorage.setItem('ice_page', page) }, [page])
  if (!authenticated) return <Login onLogin={() => setAuthenticated(true)}/>
  const screen = {
    dashboard: <DashboardScreen go={setPage}/>, agents: <AgentsScreen/>, telegram: <TelegramScreen/>,
    sip: <SipScreen/>, calls: <CallsScreen/>,
    conversations: <ConversationsScreen/>, employee: <EmployeeScreen/>, connections: <ConnectionsScreen/>, runtime: <RuntimeScreen/>, memory: <MemoryScreen/>, mcp: <McpScreen/>, cron: <CronScreen/>, settings: <SettingsScreen/>,
    logs: <LiveScreen mode="logs"/>, tasks: <LiveScreen mode="tasks"/>,
  }[page] ?? <DashboardScreen go={setPage}/>
  return <Shell page={page} setPage={setPage} logout={() => { localStorage.removeItem('ice_token'); setAuthenticated(false) }}>{screen}</Shell>
}
