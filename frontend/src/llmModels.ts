export type LlmModelPreset = { id: string; label: string }

export const OPENAI_AGENT_MODELS: LlmModelPreset[] = [
  { id: 'gpt-5.6-terra', label: 'GPT-5.6 Terra — баланс качества и цены' },
  { id: 'gpt-5.6-luna', label: 'GPT-5.6 Luna — быстрая и экономичная' },
]

export const DEEPSEEK_AGENT_MODELS: LlmModelPreset[] = [
  { id: 'deepseek-chat', label: 'DeepSeek Chat' },
  { id: 'deepseek-reasoner', label: 'DeepSeek Reasoner' },
]

export const AGENT_MODEL_PRESETS: Record<string, LlmModelPreset[]> = {
  openai: OPENAI_AGENT_MODELS,
  deepseek: DEEPSEEK_AGENT_MODELS,
}

export function agentModelPresets(provider?: string): LlmModelPreset[] {
  return AGENT_MODEL_PRESETS[provider || 'openai'] || OPENAI_AGENT_MODELS
}

export function profileModelPresets(provider?: string): LlmModelPreset[] {
  if (provider === 'deepseek') return DEEPSEEK_AGENT_MODELS
  if (provider === 'openai' || !provider) return OPENAI_AGENT_MODELS
  return []
}
