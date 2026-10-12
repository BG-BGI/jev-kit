export type Period = 'session' | 'day' | 'week' | 'month'

export type PeriodStats = {
  n: number
  denied: number
  jev: number
  cost: number
  tok: number
  saved: number
  eff: number
}

export type ModelStats = { model: string; eff: Record<Period, number> }

/** Jev's USD-per-token rates as of one UTC day, from the pricing history. */
export type DailyRates = { day: string; input: number; output: number }

export type Stats = {
  periods: Record<Period, PeriodStats>
  models: ModelStats[]
  updatedAt: string
}

declare module 'claude-code' {
  interface PluginState {
    'jev-stats': {
      stats: Stats | null
      sessionId: string
      rates: { input: number; output: number }
      ratesHistory: DailyRates[]
    }
  }
}
