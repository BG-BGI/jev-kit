import type { ModelStats, Period, PeriodStats, Stats } from '../types'

/** USD per token for a Jev call, input and output. */
export type Rates = { input: number; output: number }
/** The gateway's typesafe-ai/jev row, used until the cache says otherwise. */
export const JEV_RATES: Rates = { input: 4.2e-8, output: 0 }

/** `jev-rates` prints "<input> <output>"; anything else keeps the default. */
export function parseRates(out: string): Rates | null {
  const [i, o] = out.trim().split(/\s+/).map(Number)
  return Number.isFinite(i) && Number.isFinite(o) && i >= 0 && o >= 0 ? { input: i, output: o } : null
}

export function fmtUsd(n: number): string {
  return '$' + n.toFixed(n < 0.01 ? 4 : 3)
}

export type Bounds = { day: string; week: string; month: string }

/** Local-midnight boundaries for today, Monday of this week and the 1st of
 *  the month, as UTC ISO prefixes that compare lexicographically with the
 *  `ts` column (ISO-8601 UTC). */
export function boundaries(now: Date): Bounds {
  const day = new Date(now)
  day.setHours(0, 0, 0, 0)
  const week = new Date(day)
  week.setDate(day.getDate() - ((day.getDay() + 6) % 7))
  const month = new Date(day)
  month.setDate(1)
  const iso = (d: Date) => d.toISOString().slice(0, 19)
  return { day: iso(day), week: iso(week), month: iso(month) }
}

/** One statement, one row per period, uniform columns, so `sqlite3 -json`
 *  answers a single JSON array. */
export function buildSql(sessionId: string, b: Bounds, rates: Rates = JEV_RATES): string {
  const sid = sessionId.replace(/'/g, "''")
  const periods: Array<[Period, string]> = [
    ['session', `session_id='${sid}'`],
    ['day', `ts>='${b.day}'`],
    ['week', `ts>='${b.week}'`],
    ['month', `ts>='${b.month}'`],
  ]
  return (
    periods
      .map(
        ([p, w]) => `SELECT '${p}' AS period,
 (SELECT COUNT(*) FROM events WHERE ${w}) AS n,
 (SELECT COALESCE(SUM(denied),0) FROM events WHERE ${w}) AS denied,
 (SELECT COALESCE(SUM(jev_called),0) FROM events WHERE ${w}) AS jev,
 (SELECT COALESCE(SUM(COALESCE(cost_usd, COALESCE(input_tokens,0)*${rates.input}+COALESCE(output_tokens,0)*${rates.output})),0.0) FROM events WHERE ${w}) AS cost,
 (SELECT COALESCE(SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),0) FROM events WHERE ${w}) AS tok,
 (SELECT COALESCE(SUM(chars_before-chars_after),0)/4 FROM compaction WHERE outcome='trimmed' AND ${w}) AS saved,
 (SELECT COALESCE(SUM((chars_before-chars_after)*COALESCE(requests_after,0)),0)/4 FROM compaction WHERE outcome='trimmed' AND ${w}) AS eff`,
      )
      .join('\nUNION ALL\n') + ';'
  )
}

/** Total saved per model: each trim's tokens times the later requests that ran
 *  on each model, so a /model switch splits the credit. Trims scraped before
 *  compaction_req existed have no rows there until the next scrape. */
export function buildModelSql(sessionId: string, b: Bounds): string {
  const sid = sessionId.replace(/'/g, "''")
  const since = [b.day, b.week, b.month].sort()[0]
  const cols = (
    [
      ['session', `c.session_id='${sid}'`],
      ['day', `c.ts>='${b.day}'`],
      ['week', `c.ts>='${b.week}'`],
      ['month', `c.ts>='${b.month}'`],
    ] as Array<[Period, string]>
  )
    .map(
      ([p, w]) =>
        `COALESCE(SUM(CASE WHEN ${w} THEN (c.chars_before-c.chars_after)*r.requests ELSE 0 END),0)/4 AS eff_${p}`,
    )
    .join(', ')
  return `SELECT r.model AS model, ${cols}
FROM compaction c JOIN compaction_req r ON r.row_hash=c.row_hash
WHERE c.outcome='trimmed' AND (c.ts>='${since}' OR c.session_id='${sid}')
GROUP BY 1 ORDER BY eff_month DESC;`
}

export function parseModelRows(rows: unknown): ModelStats[] {
  if (!Array.isArray(rows)) return []
  const P: Period[] = ['session', 'day', 'week', 'month']
  return rows.map(row => {
    const r = row as Record<string, unknown>
    const pick = (k: string) =>
      Object.fromEntries(P.map(p => [p, Number(r[`${k}_${p}`]) || 0])) as Record<Period, number>
    return { model: String(r.model ?? 'unknown'), eff: pick('eff') }
  })
}

/** claude-sonnet-5-5 -> sonnet-5-5, so the name fits the pane. */
export function shortModel(m: string): string {
  return m.replace(/^claude-/, '').replace(/-\d{8}$/, '')
}

const EMPTY: PeriodStats = { n: 0, denied: 0, jev: 0, cost: 0, tok: 0, saved: 0, eff: 0 }

export function parseRows(rows: unknown, updatedAt: string, models: ModelStats[] = []): Stats {
  const periods: Record<Period, PeriodStats> = {
    session: { ...EMPTY },
    day: { ...EMPTY },
    week: { ...EMPTY },
    month: { ...EMPTY },
  }
  if (Array.isArray(rows)) {
    for (const row of rows) {
      const r = row as Record<string, unknown>
      const p = r.period as Period
      if (!(p in periods)) continue
      periods[p] = {
        n: Number(r.n) || 0,
        denied: Number(r.denied) || 0,
        jev: Number(r.jev) || 0,
        cost: Number(r.cost) || 0,
        tok: Number(r.tok) || 0,
        saved: Number(r.saved) || 0,
        eff: Number(r.eff) || 0,
      }
    }
  }
  return { periods, models, updatedAt }
}

export function fmt(n: number): string {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 10000) return Math.round(n / 1000) + 'k'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}

export function pad(s: string, w: number): string {
  return s.length >= w ? s : ' '.repeat(w - s.length) + s
}
