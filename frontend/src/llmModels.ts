export const OPENAI_AGENT_MODELS = [
  { id: 'gpt-5.6-terra', label: 'GPT-5.6 Terra — баланс качества и цены' },
  { id: 'gpt-5.6-luna', label: 'GPT-5.6 Luna — быстрая и экономичная' },
] as const

export const DEEPSEEK_AGENT_MODELS = [
  { id: 'deepseek-chat', label: 'DeepSeek Chat' },
  { id: 'deepseek-reasoner', label: 'DeepSeek Reasoner' },
] as const

export const AGENT_MODEL_PRESETS: Record<string, readonly { id: string; label: string }[]> = {
  openai: OPENAI_AGENT_MODELS,
  deepseek: DEEPSEEK_AGENT_MODELS,
}

export function agentModelPresets(provider?: string) {
  return AGENT_MODEL_PRESETS[provider || 'openai'] || OPENAI_AGENT_MODELS
}

export function profileModelPresets(provider?: string) {
  if (provider === 'deepseek') return DEEPSEEK_AGENT_MODELS
  if (provider === 'openai' || !provider) return OPENAI_AGENT_MODELS
  return []
}
