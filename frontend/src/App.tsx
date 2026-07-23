import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity, Bot, BrainCircuit, CalendarClock, CheckCircle2, ChevronRight, CircleAlert,
  Clock3, Database, FileText, Globe2, KeyRound, LayoutDashboard, Link2, LoaderCircle, LogOut, Menu,
  MessageCircle, MessagesSquare, Moon, Plus, RefreshCw, Search, ServerCog, Settings, ShieldCheck,
  Sparkles, Trash2, Users, Wifi, WifiOff, X, Zap,
} from 'lucide-react'
import { api, openLiveSocket } from './api'
import type {
  AdminSettings, Agent, AgentTask, CronJob, Dashboard, LogEntry, McpServer,
  Conversation, ConversationDetail, LlmProfile, LlmProfileWrite, MemoryItem, RuntimeSettings,
  Status, TelegramAccount,
} from './types'

type Page = 'dashboard' | 'agents' | 'connections' | 'runtime' | 'telegram' | 'conversations' | 'memory' | 'mcp' | 'cron' | 'settings' | 'logs' | 'tasks'
type Icon = typeof LayoutDashboard

const nav: { id: Page; label: string; icon: Icon; group?: string }[] = [
  { id: 'dashboard', label: 'Overview', icon: LayoutDashboard, group: 'Workspace' },
  { id: 'agents', label: 'Agents', icon: Bot },
  { id: 'connections', label: 'Connections', icon: KeyRound },
  { id: 'telegram', label: 'Telegram', icon: MessageCircle },
  { id: 'conversations', label: 'Conversations', icon: MessagesSquare },
  { id: 'memory', label: 'Memory', icon: BrainCircuit },
  { id: 'mcp', label: 'MCP servers', icon: ServerCog, group: 'Automation' },
  { id: 'cron', label: 'Schedules', icon: CalendarClock },
  { id: 'tasks', label: 'Agent tasks', icon: Zap },
  { id: 'logs', label: 'System logs', icon: FileText, group: 'System' },
  { id: 'runtime', label: 'Runtime settings', icon: Globe2 },
  { id: 'settings', label: 'Admin settings', icon: Settings },
]

const title: Record<Page, [string, string]> = {
  dashboard: ['Control center', 'Live status of your Ice.agent workspace'],
  agents: ['Agents', 'Configure intelligence, connections, and capabilities'],
  connections: ['Connections & providers', 'Manage LLM credentials and model endpoints'],
  telegram: ['Telegram accounts', 'Manage connected user and bot sessions'],
  conversations: ['Conversations', 'Inspect recent agent context and transcripts'],
  memory: ['Memory', 'Inspect and manage stored agent context'],
  mcp: ['MCP servers', 'Connect agents to external tools and resources'],
  cron: ['Schedules', 'Run agent prompts on recurring schedules'],
  settings: ['Admin settings', 'Access control and escalation routing'],
  runtime: ['Runtime settings', 'Search, memory, typing, and worker behavior'],
  logs: ['System logs', 'Trace runtime events across services'],
  tasks: ['Inter-agent tasks', 'Live coordination and work delegation'],
}

function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const refresh = useCallback(async () => {
    setLoading(true); setError('')
    try { setData(await loader()) } catch (e) { setError(e instanceof Error ? e.message : 'Something went wrong') }
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
      <button className="close-menu" onClick={() => setOpen(false)} aria-label="Close menu"><X/></button>
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
        <div className="system-mini"><span className="pulse"/><div><strong>System operational</strong><small>All services connected</small></div></div>
        <button className="nav-item" onClick={logout}><LogOut size={18}/>Sign out</button>
      </div>
    </aside>
    {open && <button className="scrim" onClick={() => setOpen(false)} aria-label="Close menu"/>}
    <main>
      <header className="topbar">
        <button className="menu-button" onClick={() => setOpen(true)}><Menu/></button>
        <div><h1>{(title[page] || title.dashboard)[0]}</h1><p>{(title[page] || title.dashboard)[1]}</p></div>
        <div className="top-actions"><span className="live-pill"><span className="pulse"/>Live</span><button className="avatar">IA</button></div>
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
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to sign in') }
    finally { setBusy(false) }
  }
  return <div className="login-page">
    <div className="login-orb one"/><div className="login-orb two"/>
    <form className="login-card" onSubmit={submit}>
      <div className="brand login-brand"><span className="brand-mark"><Sparkles size={22}/></span><span>Ice<span>.agent</span></span></div>
      <div className="login-copy"><h1>Welcome back</h1><p>Sign in to your agent control center.</p></div>
      {error && <Alert message={error}/>}
      <Field label="Username"><input autoFocus required value={username} onChange={e => setUsername(e.target.value)} placeholder="admin"/></Field>
      <Field label="Password"><input required type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••"/></Field>
      <button className="primary login-submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={18}/> : <ShieldCheck size={18}/>} Sign in</button>
      <small className="secure-note"><ShieldCheck size={13}/> Protected administration area</small>
    </form>
  </div>
}

function Loading() { return <div className="state"><LoaderCircle className="spin"/><span>Loading workspace data…</span></div> }
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
  return <span className={`status ${status}`}><i/>{status}</span>
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
  return <Modal title="Delete item?" subtitle={`“${name}” will be removed permanently.`} onClose={onClose}>
    <div className="modal-actions"><button className="secondary" onClick={onClose}>Cancel</button><button className="danger" disabled={busy} onClick={async () => { setBusy(true); await onDelete(); onClose() }}><Trash2 size={16}/>Delete</button></div>
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
  if (error) return <><Alert message={error}/><button className="secondary" onClick={refresh}>Retry</button></>
  if (!data) return <Empty title="No dashboard data" text="The API returned an empty overview payload."/>
  const d = data
  const agents = d.agents ?? { total: d.counts?.agents ?? d.agents_count ?? 0, online: 0, errors: 0 }
  const telegram = d.telegram_accounts ?? { total: d.counts?.telegram_accounts ?? d.telegram_accounts_count ?? 0, connected: 0 }
  const tasks = d.tasks ?? { running: 0, queued: 0, completed_today: 0 }
  const mcp = d.mcp_servers ?? { total: d.counts?.mcp_servers ?? d.mcp_servers_count ?? 0, online: 0 }
  const memoryItems = d.memory_items ?? 0
  const configuration = healthItems(d)
  const conversationCount = d.counts?.conversations ?? d.counts?.active_conversations ?? d.conversations_count ?? d.active_conversations_count
  const stats = [
    ['Active agents', agents.online, `${agents.total} configured`, Bot, 'violet'],
    ['Telegram', telegram.connected, `${telegram.total} accounts`, MessageCircle, 'blue'],
    ['Running tasks', tasks.running, `${tasks.queued} queued`, Zap, 'amber'],
    ['Memory records', memoryItems, 'Stored context', BrainCircuit, 'cyan'],
    ...(conversationCount === undefined
      ? []
      : [['Active conversations', conversationCount, 'Conversation contexts', MessagesSquare, 'violet'] as const]),
  ] as const
  return <>
    <div className="hero-card">
      <div><span className="eyebrow"><Activity size={14}/> Workspace health</span><h2>Everything is running smoothly.</h2><p>{agents.online} agents are active and ready to handle requests.</p></div>
      <div className="health-ring"><strong>{agents.errors ? '!' : '99.9%'}</strong><span>{agents.errors ? 'attention' : 'uptime'}</span></div>
    </div>
    <div className="stat-grid">{stats.map(([label, value, sub, Icon, color]) =>
      <div className="stat-card" key={label}><span className={`stat-icon ${color}`}><Icon/></span><div className="stat-value">{value}</div><strong>{label}</strong><small>{sub}</small></div>
    )}</div>
    {configuration.length > 0 && <section className="panel health-panel">
      <SectionHead title="Configuration readiness" text="Provider and connection checks reported by the API" action={<button className="secondary compact" onClick={() => go('connections')}>Manage</button>}/>
      <div className="health-grid">{configuration.map((item, index) => {
        const ready = item.ready ?? ['ready', 'online', 'active', 'ok', 'connected', 'configured'].includes(String(item.status).toLowerCase())
        return <div className={`health-item ${ready ? 'ready' : 'attention'}`} key={`${item.name}-${index}`}>
          {ready ? <CheckCircle2 size={18}/> : <CircleAlert size={18}/>}<div><strong>{String(item.name).replaceAll('_', ' ')}</strong><small>{item.detail || item.status || (ready ? 'Ready' : 'Needs configuration')}</small></div>
        </div>
      })}</div>
    </section>}
    <div className="dashboard-grid">
      <section className="panel"><SectionHead title="Service status" text="Infrastructure connections"/>
        <div className="service-list">
          {[['Agent runtime', `${agents.online}/${agents.total}`, agents.errors ? 'error' : 'online'],
            ['MCP gateway', `${mcp.online}/${mcp.total}`, mcp.online ? 'online' : 'offline'],
            ['Telegram bridge', `${telegram.connected} connected`, telegram.connected ? 'online' : 'offline'],
            ['Task worker', `${tasks.running} running`, 'online']].map(([name, val, status]) =>
            <div className="service-row" key={name}><span className={`service-icon ${status}`}><Wifi size={17}/></span><div><strong>{name}</strong><small>{val}</small></div><StatusDot status={status as Status}/></div>)}
        </div>
      </section>
      <section className="panel"><SectionHead title="Quick actions" text="Common workspace tasks"/>
        <div className="quick-grid">
          {[['Create an agent', 'Configure a new AI worker', Bot, 'agents'], ['Connect Telegram', 'Add a messaging account', MessageCircle, 'telegram'],
            ['Add MCP server', 'Connect external tools', ServerCog, 'mcp'], ['Schedule a job', 'Automate a recurring task', Clock3, 'cron']].map(([label, sub, Icon, page]) =>
            <button className="quick-action" key={label as string} onClick={() => go(page as Page)}><span><Icon size={19}/></span><div><strong>{label as string}</strong><small>{sub as string}</small></div><ChevronRight size={16}/></button>)}
        </div>
      </section>
    </div>
  </>
}

const emptyAgent: Omit<Agent, 'id'> = {
  name: '', description: '', prompt: '', model: '',
  tools: [], links: [], typing_enabled: true, enabled: true,
}
function AgentsScreen() {
  const { data = [], setData, loading, error, refresh } = useLoad(api.agents.list, [])
  const profiles = useLoad(api.llmProfiles.list, [])
  const telegram = useLoad(api.telegram.list, [])
  const [editing, setEditing] = useState<Partial<Agent> | null>(null)
  const [deleting, setDeleting] = useState<Agent | null>(null)
  if (loading || profiles.loading || telegram.loading) return <Loading/>
  const profileName = (id?: string) => profiles.data?.find(p => String(p.id) === String(id))?.name || (id ? 'Unknown LLM profile' : 'No LLM profile')
  const telegramName = (id?: string) => telegram.data?.find(a => String(a.id) === String(id))?.name || (id ? 'Unknown Telegram account' : 'No Telegram account')
  return <>
    {error && <Alert message={error}/>}
    <SectionHead title={`${data.length} configured agents`} text="Each agent has isolated behavior and connections" action={<button className="primary" onClick={() => setEditing(emptyAgent)}><Plus size={17}/>New agent</button>}/>
    {data.length === 0 ? <Empty icon={Bot} title="No agents yet" text="Create your first autonomous agent to get started."/> :
      <div className="card-grid">{data.map(agent => <article className="entity-card" key={agent.id}>
        <div className="entity-top"><span className="entity-avatar"><Bot/></span><StatusDot status={agent.status || (agent.enabled && agent.llm_profile_id ? 'online' : agent.enabled ? 'pending' : 'paused')}/></div>
        <h3>{agent.name}</h3><p>{agent.description || 'No description provided.'}</p>
        <div className="binding-list"><span><KeyRound size={13}/>{profileName(agent.llm_profile_id)}</span><span><MessageCircle size={13}/>{telegramName(agent.telegram_account_id)}</span></div>
        <div className="chip-row"><span className="chip">{agent.model || 'No model'}</span>{agent.tools.slice(0, 2).map(t => <span className="chip" key={t}>{t}</span>)}</div>
        <div className="entity-meta"><span><Link2 size={14}/>{agent.links.length} links</span><span>{agent.typing_enabled ? 'Typing on' : 'Typing off'}</span></div>
        <div className="card-actions"><button className="secondary" onClick={() => setEditing(agent)}>Configure</button><button className="icon-button danger-ghost" onClick={() => setDeleting(agent)}><Trash2 size={17}/></button></div>
      </article>)}</div>}
    {editing && <AgentForm value={editing} agents={data} profiles={profiles.data || []} telegram={telegram.data || []} onClose={() => setEditing(null)} onSave={async value => {
      if (value.id) { const saved = await api.agents.update(value.id, value); setData(data.map(a => a.id === saved.id ? saved : a)) }
      else { const saved = await api.agents.create(value as Omit<Agent, 'id'>); setData([...data, saved]) }
      setEditing(null)
    }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.agents.remove(deleting.id); setData(data.filter(a => a.id !== deleting.id)) }}/>}
    {error && <button className="secondary" onClick={refresh}><RefreshCw size={16}/>Retry</button>}
  </>
}

function AgentForm({ value, agents, profiles, telegram, onClose, onSave }: { value: Partial<Agent>; agents: Agent[]; profiles: LlmProfile[]; telegram: TelegramAccount[]; onClose: () => void; onSave: (v: Partial<Agent>) => Promise<void> }) {
  const [form, setForm] = useState(value)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const toolOptions = ['web_search', 'memory', 'code_execution', 'telegram', 'filesystem', 'mcp']
  const linkTarget = (link: Agent['links'][number]) =>
    typeof link === 'string' ? link : String(link.target_agent_id ?? link.agent_id ?? link.id)
  const linked = (id: string) => (form.links || []).some(link => linkTarget(link) === String(id))
  const patch = (v: Partial<Agent>) => setForm(f => ({ ...f, ...v }))
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('')
    try { await onSave(form) } catch (err) { setError(err instanceof Error ? err.message : 'Unable to save agent'); setBusy(false) }
  }
  return <Modal title={form.id ? 'Configure agent' : 'Create agent'} subtitle="Define personality, runtime, and collaboration." onClose={onClose}>
    <form onSubmit={submit}>
      {error && <Alert message={error}/>}
      <div className="form-grid">
        <Field label="Name"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Research assistant"/></Field>
        <Field label="Description"><input value={form.description || ''} onChange={e => patch({ description: e.target.value })} placeholder="What this agent does"/></Field>
        <Field label="LLM profile" hint="Only enabled profiles can run new work"><select value={form.llm_profile_id || ''} onChange={e => { const profile = profiles.find(p => String(p.id) === e.target.value); patch({ llm_profile_id: e.target.value || undefined, model: profile?.default_model || form.model || '' }) }}><option value="">Select LLM profile</option>{profiles.map(p => <option value={p.id} key={p.id}>{p.name} · {p.provider}{p.enabled ? '' : ' (disabled)'}</option>)}</select></Field>
        <Field label="Model" hint="Editable override; selecting a profile sets its default"><input required value={form.model || ''} onChange={e => patch({ model: e.target.value })} placeholder="Profile default model"/></Field>
        <Field label="Telegram account" hint="Optional messaging identity"><select value={form.telegram_account_id || ''} onChange={e => patch({ telegram_account_id: e.target.value || undefined })}><option value="">No Telegram account</option>{telegram.map(a => <option value={a.id} key={a.id}>{a.name} · {a.phone}{a.readiness && a.readiness !== 'ready' ? ` (${a.readiness})` : ''}</option>)}</select></Field>
        <Field label="System prompt" wide><textarea required rows={7} value={form.prompt || ''} onChange={e => patch({ prompt: e.target.value })} placeholder="You are a helpful agent…"/></Field>
        <Field label="Tools" wide><div className="check-grid">{toolOptions.map(tool => <label className="check" key={tool}><input type="checkbox" checked={(form.tools || []).includes(tool)} onChange={() => patch({ tools: (form.tools || []).includes(tool) ? form.tools!.filter(t => t !== tool) : [...(form.tools || []), tool] })}/><span>{tool.replace('_', ' ')}</span></label>)}</div></Field>
        <Field label="Agent links" hint="Allow this agent to delegate work" wide><div className="check-grid">{agents.filter(a => String(a.id) !== String(form.id)).map(a => <label className="check" key={a.id}><input type="checkbox" checked={linked(a.id)} onChange={() => patch({ links: linked(a.id) ? (form.links || []).filter(link => linkTarget(link) !== String(a.id)) : [...(form.links || []), a.id] })}/><span>{a.name}</span></label>)}</div></Field>
        <div className="toggle-box"><Toggle label="Show typing indicator" checked={form.typing_enabled ?? true} onChange={v => patch({ typing_enabled: v })}/></div>
        <div className="toggle-box"><Toggle label="Agent enabled" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div>
      </div>
      <div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16}/>}Save agent</button></div>
    </form>
  </Modal>
}

const emptyProfile: LlmProfileWrite = { name: '', provider: 'openai', base_url: 'https://api.openai.com/v1', default_model: '', enabled: true, api_key: '' }
function ConnectionsScreen() {
  const loaded = useLoad(api.llmProfiles.list, []); const profiles = loaded.data || []
  const [editing, setEditing] = useState<Partial<LlmProfile> | LlmProfileWrite | null>(null)
  const [deleting, setDeleting] = useState<LlmProfile | null>(null)
  const [testing, setTesting] = useState<string>(); const [testResult, setTestResult] = useState<Record<string, string>>({})
  if (loaded.loading) return <Loading/>
  async function test(profile: LlmProfile) {
    setTesting(profile.id); setTestResult(r => ({ ...r, [profile.id]: '' }))
    try { const result = await api.llmProfiles.test(profile.id); setTestResult(r => ({ ...r, [profile.id]: result.message || result.detail || (result.ok === false ? 'Connection failed' : 'Connection successful') })) }
    catch (err) { setTestResult(r => ({ ...r, [profile.id]: err instanceof Error ? err.message : 'Test failed' })) }
    finally { setTesting(undefined) }
  }
  return <>
    {loaded.error && <Alert message={loaded.error}/>}
    <SectionHead title={`${profiles.length} LLM profiles`} text="Credentials and endpoints available to agents" action={<button className="primary" onClick={() => setEditing(emptyProfile)}><Plus size={17}/>New profile</button>}/>
    {profiles.length === 0 ? <Empty icon={KeyRound} title="No LLM profiles" text="Add a provider endpoint and API credential before configuring agents."/> :
      <div className="card-grid">{profiles.map(profile => <article className="entity-card provider-card" key={profile.id}>
        <div className="entity-top"><span className="entity-avatar"><KeyRound/></span><StatusDot status={profile.enabled ? (profile.has_api_key ? 'online' : 'pending') : 'paused'}/></div>
        <h3>{profile.name}</h3><p>{profile.base_url || 'Provider default endpoint'}</p>
        <div className="chip-row"><span className="chip">{profile.provider}</span><span className="chip">{profile.default_model || 'No default model'}</span></div>
        <div className={`secret-state ${profile.has_api_key ? 'configured' : ''}`}><ShieldCheck size={14}/>{profile.has_api_key ? 'API key configured · ••••••••' : 'API key missing'}</div>
        {testResult[profile.id] && <small className="inline-result">{testResult[profile.id]}</small>}
        <div className="card-actions"><button className="secondary" disabled={testing === profile.id} onClick={() => void test(profile)}>{testing === profile.id ? <LoaderCircle className="spin" size={15}/> : <Wifi size={15}/>}Test</button><button className="secondary" onClick={() => setEditing(profile)}>Edit</button><button className="icon-button danger-ghost" onClick={() => setDeleting(profile)}><Trash2 size={17}/></button></div>
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
  const [form, setForm] = useState<Partial<LlmProfileWrite>>({ name: value.name, provider: value.provider, base_url: value.base_url, default_model: value.default_model, enabled: value.enabled, api_key: '' })
  const [busy, setBusy] = useState(false); const [error, setError] = useState('')
  const patch = (p: Partial<LlmProfileWrite>) => setForm(f => ({ ...f, ...p }))
  return <Modal title={id ? 'Edit LLM profile' : 'New LLM profile'} subtitle="Keys are write-only and never loaded back into this form." onClose={onClose}><form onSubmit={async e => {
    e.preventDefault(); setBusy(true); setError('')
    const payload = { ...form }; if (!payload.api_key) delete payload.api_key
    try { await onSave(payload, id) } catch (err) { setError(err instanceof Error ? err.message : 'Unable to save profile'); setBusy(false); patch({ api_key: '' }) }
  }}>
    {error && <Alert message={error}/>}<div className="form-grid">
      <Field label="Profile name"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Production OpenAI"/></Field>
      <Field label="Provider"><select value={form.provider} onChange={e => patch({ provider: e.target.value as LlmProfileWrite['provider'] })}><option value="openai">OpenAI</option><option value="deepseek">DeepSeek</option><option value="custom-openai-compatible">Custom / compatible</option></select></Field>
      <Field label="Base URL" wide><input required type="url" value={form.base_url || ''} onChange={e => patch({ base_url: e.target.value })} placeholder="https://api.example.com/v1"/></Field>
      <Field label="Default model"><input required value={form.default_model || ''} onChange={e => patch({ default_model: e.target.value })} placeholder="gpt-4o"/></Field>
      <Field label={id ? 'Replace API key' : 'API key'} hint={id ? 'Leave blank to preserve the stored key' : 'Stored securely by the backend'}><input required={!id} autoComplete="new-password" type="password" value={form.api_key || ''} onChange={e => patch({ api_key: e.target.value })} placeholder={id ? 'Leave blank to preserve' : 'sk-…'}/></Field>
      <div className="toggle-box wide"><Toggle label="Profile enabled" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div>
    </div><div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16}/>}Save profile</button></div>
  </form></Modal>
}

function TelegramScreen() {
  const { data = [], setData, loading, error } = useLoad(api.telegram.list, [])
  const [flow, setFlow] = useState<'details' | 'code' | null>(null)
  const [name, setName] = useState(''); const [phone, setPhone] = useState(''); const [code, setCode] = useState(''); const [password, setPassword] = useState('')
  const [apiId, setApiId] = useState(''); const [apiHash, setApiHash] = useState('')
  const [session, setSession] = useState(''); const [busy, setBusy] = useState(false); const [flowError, setFlowError] = useState('')
  const [deleting, setDeleting] = useState<TelegramAccount | null>(null)
  if (loading) return <Loading/>
  async function start(e: FormEvent) {
    e.preventDefault(); setBusy(true); setFlowError('')
    try { const result = await api.telegram.startLogin({ name, phone, api_id: Number(apiId), api_hash: apiHash }); setApiHash(''); setSession(result.session_id); setFlow('code') }
    catch (err) { setFlowError(err instanceof Error ? err.message : 'Could not send code') } finally { setBusy(false) }
  }
  async function verify(e: FormEvent) {
    e.preventDefault(); setBusy(true); setFlowError('')
    try { const account = await api.telegram.verifyCode({ session_id: session, code, password: password || undefined }); setData([...data, account]); setFlow(null); setName(''); setPhone(''); setApiId(''); setApiHash(''); setPassword(''); setCode('') }
    catch (err) { setFlowError(err instanceof Error ? err.message : 'Verification failed') } finally { setBusy(false) }
  }
  return <>
    {error && <Alert message={error}/>}
    <SectionHead title={`${data.length} Telegram accounts`} text="User sessions used by your agents" action={<button className="primary" onClick={() => setFlow('details')}><Plus size={17}/>Connect account</button>}/>
    {data.length === 0 ? <Empty icon={MessageCircle} title="No Telegram accounts" text="Connect an account using its phone number and verification code."/> :
      <div className="list-panel">{data.map(a => <div className="account-row" key={a.id}><span className="entity-avatar telegram"><MessageCircle/></span><div className="grow"><strong>{a.name}</strong><small>{a.username ? `@${a.username}` : a.phone} · API ID {a.api_id} · {a.has_api_hash ? 'app hash secured' : 'app hash missing'}</small></div><StatusDot status={(a.readiness === 'ready' ? 'online' : a.readiness ? 'pending' : a.status) as Status}/><button className="icon-button danger-ghost" onClick={() => setDeleting(a)}><Trash2 size={17}/></button></div>)}</div>}
    {flow && <Modal title={flow === 'details' ? 'Connect Telegram' : 'Enter verification code'} subtitle={flow === 'details' ? 'We will send a login code to your Telegram app.' : `Code sent to ${phone}`} onClose={() => { setApiHash(''); setPassword(''); setFlow(null) }}>
      {flowError && <Alert message={flowError}/>}
      {flow === 'details' ? <form onSubmit={start}><div className="notice wide"><KeyRound size={16}/><span>Create an app at <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org</a>, then enter its API ID and hash. The hash is sent once and never displayed again.</span></div><div className="form-grid"><Field label="Account name"><input required value={name} onChange={e => setName(e.target.value)} placeholder="Support account"/></Field><Field label="Phone number" hint="Include international country code"><input required value={phone} onChange={e => setPhone(e.target.value)} placeholder="+1 555 000 0000"/></Field><Field label="Telegram API ID"><input required min="1" inputMode="numeric" type="number" value={apiId} onChange={e => setApiId(e.target.value)} placeholder="12345678"/></Field><Field label="Telegram API hash"><input required autoComplete="new-password" type="password" value={apiHash} onChange={e => setApiHash(e.target.value)} placeholder="32-character app hash"/></Field></div><div className="modal-actions"><button type="button" className="secondary" onClick={() => { setApiHash(''); setFlow(null) }}>Cancel</button><button className="primary" disabled={busy}>Send code</button></div></form> :
      <form onSubmit={verify}><div className="form-grid"><Field label="Verification code"><input autoFocus required value={code} onChange={e => setCode(e.target.value)} placeholder="12345"/></Field><Field label="2FA password" hint="Only if enabled on the account"><input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Optional"/></Field></div><div className="modal-actions"><button type="button" className="secondary" onClick={() => setFlow('details')}>Back</button><button className="primary" disabled={busy}>Verify & connect</button></div></form>}
    </Modal>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.telegram.remove(deleting.id); setData(data.filter(a => a.id !== deleting.id)) }}/>}
  </>
}

function exactDate(value?: string | null) {
  if (!value) return 'No messages yet'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' })
}

function relativeDate(value?: string | null) {
  if (!value) return 'never'
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return 'unknown'
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  const months = Math.floor(days / 30)
  return months < 12 ? `${months}mo ago` : `${Math.floor(months / 12)}y ago`
}

function ConversationDetailModal({ id, agentName, onClose, onClear }: {
  id: string; agentName: string; onClose: () => void; onClear: (conversation: Conversation) => void
}) {
  const loaded = useLoad(() => api.conversations.get(id), [id])
  const conversation = loaded.data
  const messages = useMemo(() => [...(conversation?.messages || [])].sort((a, b) =>
    new Date(a.message_at || a.created_at).getTime() - new Date(b.message_at || b.created_at).getTime()
  ), [conversation])
  return <Modal title="Conversation details" subtitle={`${agentName} · Conversation ${id}`} onClose={onClose}>
    {loaded.error && <><Alert message={loaded.error}/><button className="secondary" onClick={loaded.refresh}><RefreshCw size={15}/>Retry</button></>}
    {loaded.loading && !conversation ? <Loading/> : conversation && <>
      <div className="conversation-facts">
        <span><small>User</small><strong>{conversation.user_id}</strong></span>
        <span><small>Chat</small><strong>{conversation.chat_id}</strong></span>
        <span><small>Messages</small><strong>{conversation.message_count}</strong></span>
        <span><small>Last activity</small><strong>{exactDate(conversation.last_message_at)}</strong></span>
      </div>
      <section className="summary-box">
        <span>Rolling summary</span>
        <p>{conversation.rolling_summary || 'No rolling summary has been generated yet.'}</p>
      </section>
      <div className="transcript-head"><h3>Recent transcript</h3><span>{messages.length} messages · chronological</span></div>
      <div className="transcript">
        {messages.length === 0 ? <p className="transcript-empty">No recent messages are available.</p> : messages.map(message => {
          const user = ['user', 'incoming', 'inbound'].includes(message.direction.toLowerCase())
          return <article className={`transcript-message ${user ? 'user' : 'agent'}`} key={message.id}>
            <div><strong>{user ? 'User' : 'Agent'}</strong><time>{exactDate(message.message_at || message.created_at)}</time></div>
            <p>{message.text}</p>
          </article>
        })}
      </div>
      <div className="modal-actions"><button className="danger" onClick={() => onClear(conversation)}><Trash2 size={16}/>Clear conversation</button><button className="secondary" onClick={onClose}>Close</button></div>
    </>}
  </Modal>
}

function ClearConversationModal({ conversation, onClose, onCleared }: {
  conversation: Conversation; onClose: () => void; onCleared: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  return <Modal title="Clear conversation?" subtitle="This removes the stored transcript and rolling context. This action cannot be undone." onClose={onClose}>
    {error && <Alert message={error}/>}
    <div className="clear-context"><MessagesSquare size={22}/><div><strong>User {conversation.user_id}</strong><small>Chat {conversation.chat_id} · {conversation.message_count} messages</small></div></div>
    <div className="modal-actions"><button className="secondary" onClick={onClose}>Cancel</button><button className="danger" disabled={busy} onClick={async () => {
      setBusy(true); setError('')
      try { await api.conversations.clear(conversation.id); onCleared() }
      catch (err) { setError(err instanceof Error ? err.message : 'Unable to clear conversation'); setBusy(false) }
    }}>{busy ? <LoaderCircle className="spin" size={16}/> : <Trash2 size={16}/>}Clear conversation</button></div>
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
  useEffect(() => {
    const interval = window.setInterval(() => void loaded.refresh(), 30_000)
    return () => window.clearInterval(interval)
  }, [loaded.refresh])
  const agentName = (id: string) => agents.data?.find(agent => String(agent.id) === String(id))?.name || id
  return <>
    <SectionHead title={`${total} conversations`} text={total > items.length ? `Showing the latest ${items.length}; refreshed every 30 seconds` : 'Stored context refreshed every 30 seconds'} action={<button className="secondary compact" disabled={loaded.loading} onClick={loaded.refresh}><RefreshCw className={loaded.loading ? 'spin' : ''} size={15}/>Refresh</button>}/>
    <form className="filter-bar conversation-filters" onSubmit={event => { event.preventDefault(); setQuery(search.trim()) }}>
      <div className="search-box"><Search size={17}/><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search user, chat, or summary…"/></div>
      <select aria-label="Filter by agent" value={agentId} onChange={event => setAgentId(event.target.value)}><option value="">All agents</option>{agents.data?.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select>
      <button className="secondary">Search</button>
      {(query || agentId) && <button type="button" className="secondary" onClick={() => { setSearch(''); setQuery(''); setAgentId('') }}>Clear</button>}
    </form>
    {(loaded.error || agents.error) && <Alert message={loaded.error || agents.error}/>}
    {loaded.loading && !loaded.data ? <Loading/> : items.length === 0 ? <Empty icon={MessagesSquare} title="No conversations found" text="Conversation context will appear here after agents exchange messages."/> :
      <div className="conversation-list">{items.map(conversation => <button className="conversation-card" key={conversation.id} onClick={() => setSelected(conversation)}>
        <div className="conversation-primary"><span className="entity-avatar"><MessagesSquare/></span><div><strong>{agentName(conversation.agent_id)}</strong><small>Agent {conversation.agent_id}</small></div></div>
        <div className="conversation-identity"><span><small>User</small><strong>{conversation.user_id}</strong></span><span><small>Chat</small><strong>{conversation.chat_id}</strong></span><span><small>Account</small><strong>{conversation.account_id}</strong></span></div>
        <div className="conversation-summary"><span>{conversation.rolling_summary || 'No rolling summary yet.'}</span></div>
        <div className="conversation-activity"><strong>{conversation.message_count}</strong><small>messages</small><time>{exactDate(conversation.last_message_at)}</time><span>{relativeDate(conversation.last_message_at)}</span><ChevronRight size={17}/></div>
      </button>)}</div>}
    {selected && <ConversationDetailModal id={selected.id} agentName={agentName(selected.agent_id)} onClose={() => setSelected(null)} onClear={conversation => setClearing(conversation)}/>}
    {clearing && <ClearConversationModal conversation={clearing} onClose={() => setClearing(null)} onCleared={() => { setClearing(null); setSelected(null); void loaded.refresh() }}/>}
  </>
}

function MemoryScreen() {
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('')
  const loaded = useLoad(() => api.memory.list(query), [query])
  const items = Array.isArray(loaded.data) ? loaded.data : loaded.data?.items || []
  const [deleting, setDeleting] = useState<MemoryItem | null>(null)
  return <>
    <SectionHead title="Stored context" text="Search semantic and structured agent memories"/>
    <form className="filter-bar" onSubmit={e => { e.preventDefault(); setQuery(search) }}><div className="search-box"><Search size={17}/><input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search memory content, keys, or scope…"/></div><button className="secondary">Search</button></form>
    {loaded.error && <Alert message={loaded.error}/>}
    {loaded.loading ? <Loading/> : items.length === 0 ? <Empty icon={BrainCircuit} title="No matching memories" text="Agent memory records will appear here as they are created."/> :
      <div className="memory-list">{items.map(item => <article className="memory-card" key={item.id}><div className="memory-head"><div><span className="scope">{item.scope}</span><strong>{item.key}</strong></div><button className="icon-button danger-ghost" onClick={() => setDeleting(item)}><Trash2 size={16}/></button></div><p>{item.content}</p><div className="entity-meta"><span>{item.agent_id ? `Agent ${item.agent_id}` : 'Global'}</span><time>{new Date(item.created_at).toLocaleString()}</time></div></article>)}</div>}
    {deleting && <ConfirmDelete name={deleting.key} onClose={() => setDeleting(null)} onDelete={async () => { await api.memory.remove(deleting.id); loaded.setData(Array.isArray(loaded.data) ? loaded.data.filter(i => i.id !== deleting.id) : loaded.data ? { ...loaded.data, items: loaded.data.items.filter(i => i.id !== deleting.id) } : loaded.data) }}/>}
  </>
}

const emptyMcp: Omit<McpServer, 'id'> = { name: '', url: '', transport: 'sse', command: '', args: [], env: {}, enabled: true }
function McpScreen() {
  const loaded = useLoad(api.mcp.list, []); const data = loaded.data || []
  const [editing, setEditing] = useState<Partial<McpServer> | null>(null); const [deleting, setDeleting] = useState<McpServer | null>(null)
  if (loaded.loading) return <Loading/>
  return <>
    {loaded.error && <Alert message={loaded.error}/>}<SectionHead title={`${data.length} tool servers`} text="Model Context Protocol connections" action={<button className="primary" onClick={() => setEditing(emptyMcp)}><Plus size={17}/>Add server</button>}/>
    {data.length === 0 ? <Empty icon={ServerCog} title="No MCP servers" text="Connect a remote SSE/HTTP server or local stdio process."/> :
    <div className="list-panel">{data.map(server => <div className="server-row" key={server.id}><span className="entity-avatar"><ServerCog/></span><div className="grow"><strong>{server.name}</strong><small>{server.transport === 'stdio' ? server.command : server.url}</small></div><span className="chip">{server.transport}</span><StatusDot status={server.status || (server.enabled ? 'online' : 'paused')}/><button className="secondary compact" onClick={() => setEditing(server)}>Edit</button><button className="icon-button danger-ghost" onClick={() => setDeleting(server)}><Trash2 size={17}/></button></div>)}</div>}
    {editing && <McpForm value={editing} onClose={() => setEditing(null)} onSave={async v => { const saved = v.id ? await api.mcp.update(v.id, v) : await api.mcp.create(v as Omit<McpServer, 'id'>); loaded.setData(v.id ? data.map(s => s.id === saved.id ? saved : s) : [...data, saved]); setEditing(null) }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.mcp.remove(deleting.id); loaded.setData(data.filter(s => s.id !== deleting.id)) }}/>}
  </>
}
function McpForm({ value, onClose, onSave }: { value: Partial<McpServer>; onClose: () => void; onSave: (v: Partial<McpServer>) => Promise<void> }) {
  const [form, setForm] = useState<Partial<McpServer>>({ ...value, env: {} }); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  const patch = (p: Partial<McpServer>) => setForm(f => ({ ...f, ...p }))
  return <Modal title={form.id ? 'Edit MCP server' : 'Add MCP server'} subtitle="Environment secrets are encrypted and write-only." onClose={onClose}><form onSubmit={async e => { e.preventDefault(); setBusy(true); const payload = { ...form }; if (form.id && Object.keys(form.env || {}).length === 0) delete payload.env; try { await onSave(payload) } catch (err) { setError(err instanceof Error ? err.message : 'Unable to save'); setBusy(false); patch({ env: {} }) } }}>
    {error && <Alert message={error}/>}<div className="form-grid"><Field label="Name"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Internal tools"/></Field><Field label="Transport"><select value={form.transport} onChange={e => patch({ transport: e.target.value as McpServer['transport'] })}><option value="sse">SSE</option><option value="streamable-http">Streamable HTTP</option><option value="stdio">stdio</option></select></Field>
    {form.transport === 'stdio' ? <><Field label="Command"><input required value={form.command || ''} onChange={e => patch({ command: e.target.value })} placeholder="npx"/></Field><Field label="Arguments" hint="One per line"><textarea rows={3} value={(form.args || []).join('\n')} onChange={e => patch({ args: e.target.value.split('\n').filter(Boolean) })}/></Field></> : <Field label="Server URL" wide><input required type="url" value={form.url || ''} onChange={e => patch({ url: e.target.value })} placeholder="https://tools.example.com/mcp"/></Field>}
    <Field label="Environment variables" hint={form.id ? 'Encrypted at rest. Leave blank to preserve stored values; secrets are never repopulated.' : 'KEY=value, one per line. Encrypted at rest.'} wide><textarea autoComplete="off" rows={4} value={Object.entries(form.env || {}).map(([k, v]) => `${k}=${v}`).join('\n')} onChange={e => patch({ env: Object.fromEntries(e.target.value.split('\n').filter(Boolean).map(line => { const i = line.indexOf('='); return i < 0 ? [line, ''] : [line.slice(0, i), line.slice(i + 1)] })) })}/></Field><div className="toggle-box wide"><Toggle label="Server enabled" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div></div>
    <div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>Save server</button></div></form></Modal>
}

const emptyCron: Omit<CronJob, 'id'> = { name: '', agent_id: '', schedule: '0 9 * * *', prompt: '', timezone: 'UTC', enabled: true }
function CronScreen() {
  const jobs = useLoad(api.cron.list, []); const agents = useLoad(api.agents.list, []); const data = jobs.data || []
  const [editing, setEditing] = useState<Partial<CronJob> | null>(null); const [deleting, setDeleting] = useState<CronJob | null>(null)
  if (jobs.loading || agents.loading) return <Loading/>
  return <>
    {(jobs.error || agents.error) && <Alert message={jobs.error || agents.error}/>}<SectionHead title={`${data.length} schedules`} text="Cron-powered autonomous tasks" action={<button className="primary" onClick={() => setEditing({ ...emptyCron, agent_id: agents.data?.[0]?.id || '' })}><Plus size={17}/>New schedule</button>}/>
    {data.length === 0 ? <Empty icon={CalendarClock} title="No scheduled jobs" text="Schedule recurring prompts for any configured agent."/> :
    <div className="list-panel">{data.map(job => <div className="server-row" key={job.id}><span className="entity-avatar amber"><CalendarClock/></span><div className="grow"><strong>{job.name}</strong><small>{job.schedule} · {job.timezone}</small></div><span className="chip">{agents.data?.find(a => a.id === job.agent_id)?.name || job.agent_id}</span><StatusDot status={job.enabled ? (job.status || 'active') : 'paused'}/><button className="secondary compact" onClick={() => setEditing(job)}>Edit</button><button className="icon-button danger-ghost" onClick={() => setDeleting(job)}><Trash2 size={17}/></button></div>)}</div>}
    {editing && <CronForm value={editing} agents={agents.data || []} onClose={() => setEditing(null)} onSave={async v => { const saved = v.id ? await api.cron.update(v.id, v) : await api.cron.create(v as Omit<CronJob, 'id'>); jobs.setData(v.id ? data.map(j => j.id === saved.id ? saved : j) : [...data, saved]); setEditing(null) }}/>}
    {deleting && <ConfirmDelete name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => { await api.cron.remove(deleting.id); jobs.setData(data.filter(j => j.id !== deleting.id)) }}/>}
  </>
}
function CronForm({ value, agents, onClose, onSave }: { value: Partial<CronJob>; agents: Agent[]; onClose: () => void; onSave: (v: Partial<CronJob>) => Promise<void> }) {
  const [form, setForm] = useState(value); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  const patch = (p: Partial<CronJob>) => setForm(f => ({ ...f, ...p }))
  return <Modal title={form.id ? 'Edit schedule' : 'New schedule'} onClose={onClose}><form onSubmit={async e => { e.preventDefault(); setBusy(true); try { await onSave(form) } catch (err) { setError(err instanceof Error ? err.message : 'Unable to save'); setBusy(false) } }}>
    {error && <Alert message={error}/>}<div className="form-grid"><Field label="Name"><input required value={form.name || ''} onChange={e => patch({ name: e.target.value })} placeholder="Daily digest"/></Field><Field label="Agent"><select required value={form.agent_id} onChange={e => patch({ agent_id: e.target.value })}><option value="">Select agent</option>{agents.map(a => <option value={a.id} key={a.id}>{a.name}</option>)}</select></Field><Field label="Cron expression" hint="minute hour day month weekday"><input required value={form.schedule || ''} onChange={e => patch({ schedule: e.target.value })} placeholder="0 9 * * *"/></Field><Field label="Timezone"><input required value={form.timezone || 'UTC'} onChange={e => patch({ timezone: e.target.value })} placeholder="Europe/London"/></Field><Field label="Prompt" wide><textarea required rows={6} value={form.prompt || ''} onChange={e => patch({ prompt: e.target.value })} placeholder="Create and send the daily summary…"/></Field><div className="toggle-box wide"><Toggle label="Schedule enabled" checked={form.enabled ?? true} onChange={v => patch({ enabled: v })}/></div></div>
    <div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>Save schedule</button></div></form></Modal>
}

function RuntimeScreen() {
  const loaded = useLoad(api.settings.runtime, []); const profiles = useLoad(api.llmProfiles.list, [])
  const [form, setForm] = useState<RuntimeSettings>(); const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false); const [error, setError] = useState(''); const [searchResult, setSearchResult] = useState('')
  useEffect(() => { if (loaded.data) setForm({
    ...loaded.data,
    timezone: loaded.data.timezone ?? 'UTC',
    telegram_history_limit: loaded.data.telegram_history_limit ?? 50,
    recent_context_messages: loaded.data.recent_context_messages ?? 20,
    context_max_chars: loaded.data.context_max_chars ?? 32000,
    summarization_enabled: loaded.data.summarization_enabled ?? true,
    summarize_after_messages: loaded.data.summarize_after_messages ?? 30,
    mem0_api_key: '',
  }) }, [loaded.data])
  if (loaded.loading || profiles.loading || !form) return <Loading/>
  const patch = (p: Partial<RuntimeSettings>) => setForm(f => f ? ({ ...f, ...p }) : f)
  const number = (key: keyof RuntimeSettings, value: string) => patch({ [key]: Number(value) } as Partial<RuntimeSettings>)
  async function save(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError(''); setSaved(false)
    const payload: RuntimeSettings = { ...(form as RuntimeSettings) }; if (!payload.mem0_api_key) delete payload.mem0_api_key
    try { const result = await api.settings.updateRuntime(payload); setForm({ ...result, mem0_api_key: '' }); setSaved(true); setTimeout(() => setSaved(false), 2500) }
    catch (err) { setError(err instanceof Error ? err.message : 'Unable to save runtime settings'); patch({ mem0_api_key: '' }) }
    finally { setBusy(false) }
  }
  async function testSearch() {
    setSearchResult('Testing…')
    try { const result = await api.settings.testSearch(); setSearchResult(result.message || result.detail || (result.ok === false ? 'Search test failed' : 'Search connection successful')) }
    catch (err) { setSearchResult(err instanceof Error ? err.message : 'Search test failed') }
  }
  return <form className="settings-layout runtime-layout" onSubmit={save}>
    {(loaded.error || profiles.error || error) && <Alert message={loaded.error || profiles.error || error}/>}
    <section className="panel"><SectionHead title="Web search" text="Search service used by tool-enabled agents" action={<button type="button" className="secondary compact" onClick={() => void testSearch()}><Wifi size={14}/>Test search</button>}/>
      <div className="form-grid"><Field label="Search provider"><select required value={form.search_provider} onChange={e => patch({ search_provider: e.target.value })}><option value="ddg">DuckDuckGo</option><option value="searxng">SearXNG</option></select></Field><Field label="SearXNG URL"><input type="url" value={form.searxng_url || ''} onChange={e => patch({ searxng_url: e.target.value || null })} placeholder="https://search.example.com"/></Field></div>
      {searchResult && <div className="inline-result standalone">{searchResult}</div>}
    </section>
    <section className="panel"><SectionHead title="Conversation context" text="Controls the recent Telegram transcript and rolling context supplied to agents"/>
      <div className="form-grid">
        <Field label="Timezone" hint="IANA timezone used when telling the agent the current date and time (for example, Europe/London)."><input required value={form.timezone} onChange={e => patch({ timezone: e.target.value })} placeholder="UTC"/></Field>
        <Field label="Telegram history limit" hint="Maximum messages fetched from Telegram when context is initialized."><input required min="1" max="500" type="number" value={form.telegram_history_limit} onChange={e => number('telegram_history_limit', e.target.value)}/></Field>
        <Field label="Recent context messages" hint="Most recent conversation messages included verbatim."><input required min="1" max="500" type="number" value={form.recent_context_messages} onChange={e => number('recent_context_messages', e.target.value)}/></Field>
        <Field label="Maximum context characters" hint="Character budget for assembled conversation context."><input required min="1000" max="200000" type="number" value={form.context_max_chars} onChange={e => number('context_max_chars', e.target.value)}/></Field>
        <Field label="Summarize after messages" hint="Generate or refresh the rolling summary after this many messages."><input required min="2" max="5000" disabled={!form.summarization_enabled} type="number" value={form.summarize_after_messages} onChange={e => number('summarize_after_messages', e.target.value)}/></Field>
        <div className="toggle-box"><Toggle label="Rolling summarization enabled" checked={form.summarization_enabled} onChange={v => patch({ summarization_enabled: v })}/></div>
      </div>
    </section>
    <section className="panel"><SectionHead title="Memory backend" text="Long-term semantic memory and embedding storage"/>
      <div className="form-grid">
        <Field label="Memory backend"><select value={form.memory_backend} onChange={e => patch({ memory_backend: e.target.value })}><option value="local">Local Mem0 + Qdrant</option><option value="platform">Mem0 Platform</option></select></Field>
        <Field label="Memory LLM profile"><select value={form.memory_llm_profile_id || ''} onChange={e => patch({ memory_llm_profile_id: e.target.value || null })}><option value="">No dedicated profile</option>{profiles.data?.map(p => <option key={p.id} value={p.id}>{p.name} · {p.default_model}</option>)}</select></Field>
        <Field label="Mem0 API key" hint={form.has_mem0_api_key ? 'Configured · ••••••••. Leave blank to preserve it.' : 'Not configured. Secret is write-only.'}><input autoComplete="new-password" type="password" value={form.mem0_api_key || ''} onChange={e => patch({ mem0_api_key: e.target.value })} placeholder={form.has_mem0_api_key ? 'Leave blank to preserve' : 'Enter API key'}/></Field>
        <Field label="Qdrant URL"><input type="url" value={form.qdrant_url || ''} onChange={e => patch({ qdrant_url: e.target.value || null })} placeholder="http://qdrant:6333"/></Field>
        <div className="toggle-box wide"><Toggle label="Memory enabled" checked={form.memory_enabled} onChange={v => patch({ memory_enabled: v })}/></div>
      </div>
    </section>
    <section className="panel"><SectionHead title="Human-style messaging" text="Typing presence and outbound message pacing"/>
      <div className="form-grid"><Field label="Minimum typing (seconds)"><input min="0" step=".1" type="number" value={form.typing_min_seconds} onChange={e => number('typing_min_seconds', e.target.value)}/></Field><Field label="Maximum typing (seconds)"><input min="0" step=".1" type="number" value={form.typing_max_seconds} onChange={e => number('typing_max_seconds', e.target.value)}/></Field><Field label="Typing jitter (seconds)"><input min="0" step=".1" type="number" value={form.typing_jitter_seconds} onChange={e => number('typing_jitter_seconds', e.target.value)}/></Field><Field label="Message chunk size"><input min="256" max="4096" type="number" value={form.typing_chunk_size} onChange={e => number('typing_chunk_size', e.target.value)}/></Field><div className="toggle-box wide"><Toggle label="Send online and typing presence" checked={form.typing_presence} onChange={v => patch({ typing_presence: v })}/></div></div>
    </section>
    <section className="panel"><SectionHead title="Task execution" text="Concurrency and agent tool-loop limits"/><div className="form-grid"><Field label="Task workers"><input required min="1" type="number" value={form.task_workers} onChange={e => number('task_workers', e.target.value)}/></Field><Field label="Maximum tool rounds"><input required min="1" type="number" value={form.max_tool_rounds} onChange={e => number('max_tool_rounds', e.target.value)}/></Field></div></section>
    <div className="save-bar"><span>{saved && <><CheckCircle2 size={17}/>Runtime settings saved</>}</span><button className="primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16}/>}Save runtime</button></div>
  </form>
}

function SettingsScreen() {
  const loaded = useLoad(api.settings.get, []); const agents = useLoad(api.agents.list, [])
  const [form, setForm] = useState<AdminSettings>(); const [saved, setSaved] = useState(false); const [error, setError] = useState('')
  useEffect(() => { if (loaded.data) setForm(loaded.data) }, [loaded.data])
  if (loaded.loading || !form) return <Loading/>
  return <form className="settings-layout" onSubmit={async e => { e.preventDefault(); setError(''); try { const result = await api.settings.update(form); setForm(result); setSaved(true); setTimeout(() => setSaved(false), 2500) } catch (err) { setError(err instanceof Error ? err.message : 'Unable to save settings') } }}>
    {error && <Alert message={error}/>}
    <section className="panel"><SectionHead title="Administrator access" text="Telegram user IDs allowed to issue admin commands"/>
      <Field label="Admin Telegram IDs" hint="Comma-separated numeric user IDs"><textarea rows={4} value={form.admin_ids.join(', ')} onChange={e => setForm({ ...form, admin_ids: e.target.value.split(',').map(v => v.trim()).filter(Boolean) })} placeholder="123456789, 987654321"/></Field>
    </section>
    <section className="panel"><SectionHead title="Escalation routing" text="Where agents send requests requiring human attention"/>
      <div className="form-grid"><Field label="Escalation agent"><select value={form.escalation_agent_id || ''} onChange={e => setForm({ ...form, escalation_agent_id: e.target.value || undefined })}><option value="">None</option>{agents.data?.map(a => <option value={a.id} key={a.id}>{a.name}</option>)}</select></Field><Field label="Escalation chat ID"><input value={form.escalation_chat_id || ''} onChange={e => setForm({ ...form, escalation_chat_id: e.target.value || undefined })} placeholder="-100123456789"/></Field></div>
    </section>
    <section className="panel"><SectionHead title="Notifications"/><div className="setting-lines"><Toggle label="Notify administrators on agent errors" checked={form.notify_on_error} onChange={v => setForm({ ...form, notify_on_error: v })}/><Toggle label="Notify on human escalation" checked={form.notify_on_escalation} onChange={v => setForm({ ...form, notify_on_escalation: v })}/></div></section>
    <div className="save-bar"><span>{saved && <><CheckCircle2 size={17}/>Settings saved</>}</span><button className="primary">Save changes</button></div>
  </form>
}

function LiveScreen({ mode }: { mode: 'logs' | 'tasks' }) {
  const logsLoad = useLoad(() => api.logs(), []); const tasksLoad = useLoad(api.tasks, [])
  const [connected, setConnected] = useState(false); const [search, setSearch] = useState(''); const [level, setLevel] = useState('')
  const [logs, setLogs] = useState<LogEntry[]>([]); const [tasks, setTasks] = useState<AgentTask[]>([])
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
    <SectionHead title={mode === 'logs' ? `${filteredLogs.length} recent events` : `${filteredTasks.length} tasks`} text={connected ? 'Receiving live updates' : 'Live stream unavailable — showing API data'} action={<span className={`live-pill ${connected ? '' : 'muted'}`}>{connected ? <Wifi size={14}/> : <WifiOff size={14}/>} {connected ? 'Connected' : 'Offline'}</span>}/>
    <div className="filter-bar"><div className="search-box"><Search size={17}/><input value={search} onChange={e => setSearch(e.target.value)} placeholder={`Search ${mode}…`}/></div>{mode === 'logs' && <select value={level} onChange={e => setLevel(e.target.value)}><option value="">All levels</option><option>debug</option><option>info</option><option>warning</option><option>error</option></select>}</div>
    {error && <Alert message={error}/>}
    {loading ? <Loading/> : mode === 'logs' ? <div className="log-view">{filteredLogs.length === 0 ? <Empty icon={FileText} title="No log events" text="Runtime events will appear here."/> : filteredLogs.map(log => <div className="log-row" key={log.id}><time>{new Date(log.timestamp).toLocaleTimeString()}</time><span className={`log-level ${log.level}`}>{log.level}</span><strong>{log.source}</strong><p>{log.message}</p></div>)}</div> :
      <div className="task-board">{(['queued', 'running', 'completed', 'failed'] as AgentTask['status'][]).map(status => <section className="task-column" key={status}><h3><span className={`task-dot ${status}`}/>{status}<small>{filteredTasks.filter(t => t.status === status).length}</small></h3>{filteredTasks.filter(t => t.status === status).map(task => <article className="task-card" key={task.id}><strong>{task.title}</strong>{task.payload && <p>{task.payload}</p>}<div><span>{task.from_agent_id || 'system'} → {task.to_agent_id}</span><time>{new Date(task.created_at).toLocaleString()}</time></div></article>)}</section>)}</div>}
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
    conversations: <ConversationsScreen/>, connections: <ConnectionsScreen/>, runtime: <RuntimeScreen/>, memory: <MemoryScreen/>, mcp: <McpScreen/>, cron: <CronScreen/>, settings: <SettingsScreen/>,
    logs: <LiveScreen mode="logs"/>, tasks: <LiveScreen mode="tasks"/>,
  }[page] ?? <DashboardScreen go={setPage}/>
  return <Shell page={page} setPage={setPage} logout={() => { localStorage.removeItem('ice_token'); setAuthenticated(false) }}>{screen}</Shell>
}
