import type { Period, PeriodStats, Stats } from '../types'

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
export function buildSql(sessionId: string, b: Bounds): string {
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
 (SELECT COALESCE(SUM(cost_usd),0.0) FROM events WHERE ${w}) AS cost,
 (SELECT COALESCE(SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),0) FROM events WHERE ${w}) AS tok,
 (SELECT COALESCE(SUM(chars_before-chars_after),0)/4 FROM compaction WHERE outcome='trimmed' AND ${w}) AS saved,
 (SELECT COALESCE(SUM((chars_before-chars_after)*COALESCE(requests_after,0)),0)/4 FROM compaction WHERE outcome='trimmed' AND ${w}) AS eff`,
      )
      .join('\nUNION ALL\n') + ';'
  )
}

const EMPTY: PeriodStats = { n: 0, denied: 0, jev: 0, cost: 0, tok: 0, saved: 0, eff: 0 }

export function parseRows(rows: unknown, updatedAt: string): Stats {
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
  return { periods, updatedAt }
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
