import { expect, test } from 'claude-code/testing'

import {
  boundaries,
  buildModelSql,
  buildSql,
  fmt,
  fmtUsd,
  parseModelRows,
  parseRates,
  parseRatesHistory,
  parseRows,
  shortModel,
} from './lib'

test('boundaries order: month <= week <= day', () => {
  const b = boundaries(new Date('2026-10-07T15:30:00'))
  expect(b.month <= b.week).toBe(true)
  expect(b.week <= b.day).toBe(true)
  expect(b.day.length).toBe(19)
})

test('buildSql quotes the session id and names all four periods', () => {
  const sql = buildSql("s'1", boundaries(new Date('2026-10-07T15:30:00')))
  expect(sql.includes("session_id='s''1'")).toBe(true)
  for (const p of ['session', 'day', 'week', 'month']) {
    expect(sql.includes(`'${p}' AS period`)).toBe(true)
  }
  expect(sql.split('UNION ALL').length).toBe(4)
})

test('parseRows fills missing periods with zeros', () => {
  const s = parseRows(
    [{ period: 'day', n: 5, denied: 1, jev: 2, cost: 0.01, tok: 100, saved: 10, eff: 50 }],
    '12:00:00',
  )
  expect(s.periods.day.n).toBe(5)
  expect(s.periods.day.eff).toBe(50)
  expect(s.periods.week.n).toBe(0)
  expect(s.periods.session.cost).toBe(0)
})

test('fmt compacts large numbers', () => {
  expect(fmt(532)).toBe('532')
  expect(fmt(1500)).toBe('1.5k')
  expect(fmt(91000)).toBe('91k')
  expect(fmt(10700000)).toBe('10.7M')
})

test('buildModelSql credits later requests by model and quotes the session id', () => {
  const sql = buildModelSql("s'1", boundaries(new Date('2026-10-07T15:30:00')))
  expect(sql.includes("session_id='s''1'")).toBe(true)
  expect(sql.includes("outcome='trimmed'")).toBe(true)
  expect(sql.includes('JOIN compaction_req')).toBe(true)
  expect(sql.includes('GROUP BY 1')).toBe(true)
  for (const p of ['session', 'day', 'week', 'month']) {
    expect(sql.includes(`eff_${p}`)).toBe(true)
  }
})

test('parseModelRows reads per-period columns and tolerates junk', () => {
  const m = parseModelRows([{ model: 'claude-sonnet-5-5', eff_day: 40, eff_month: '90' }])
  expect(m[0]?.model).toBe('claude-sonnet-5-5')
  expect(m[0]?.eff.day).toBe(40)
  expect(m[0]?.eff.month).toBe(90)
  expect(m[0]?.eff.week).toBe(0)
  expect(parseModelRows('nope').length).toBe(0)
})

test('shortModel drops the claude- prefix and date suffix', () => {
  expect(shortModel('claude-sonnet-5-5')).toBe('sonnet-5-5')
  expect(shortModel('claude-haiku-4-5-20251001')).toBe('haiku-4-5')
})

test('buildSql prices Jev tokens at the given rates when cost_usd is null', () => {
  const sql = buildSql('s', boundaries(new Date('2026-10-07T15:30:00')), { input: 4.2e-8, output: 1e-7 })
  expect(sql.includes('COALESCE(cost_usd, COALESCE(input_tokens,0)*4.2e-8+COALESCE(output_tokens,0)*1e-7)')).toBe(true)
})

test('parseRates accepts "in out" and rejects junk', () => {
  expect(parseRates('4.2e-08 0\n')).toEqual({ input: 4.2e-8, output: 0 })
  expect(parseRates('usage: nope')).toBe(null)
  expect(parseRates('-1 0')).toBe(null)
})

test('fmtUsd keeps precision for tiny amounts', () => {
  expect(fmtUsd(0.00042)).toBe('$0.0004')
  // 1.2346, not 1.2345: the latter is stored as 1.23449..., which toFixed
  // rounds down, so it tests the float representation rather than fmtUsd.
  expect(fmtUsd(1.2346)).toBe('$1.235')
})

test('parseRatesHistory keeps valid lines, sorted oldest first', () => {
  const out = '2026-10-12 5e-8 0\njunk line\n2026-10-01 4.2e-8 0\n2026-10-20 -1 0\n'
  expect(parseRatesHistory(out)).toEqual([
    { day: '2026-10-01', input: 4.2e-8, output: 0 },
    { day: '2026-10-12', input: 5e-8, output: 0 },
  ])
  expect(parseRatesHistory('')).toEqual([])
})

test('buildSql without history keeps the flat rate', () => {
  const b = boundaries(new Date('2026-10-07T15:30:00'))
  const sql = buildSql('s', b, { input: 1e-7, output: 2e-7 })
  expect(sql.includes('*1e-7')).toBe(true)
  expect(sql.includes('CASE WHEN substr')).toBe(false)
})

test('buildSql with history costs each day at its own rate', () => {
  const b = boundaries(new Date('2026-10-07T15:30:00'))
  const history = [
    { day: '2026-10-01', input: 4.2e-8, output: 0 },
    { day: '2026-10-05', input: 5e-8, output: 1e-9 },
  ]
  const sql = buildSql('s', b, { input: 9e-8, output: 0 }, history)
  // Newest day first, oldest rate as the fallback for older events.
  expect(sql.includes("WHEN substr(ts,1,10)>='2026-10-05' THEN 5e-8")).toBe(true)
  expect(sql.includes('ELSE 4.2e-8 END')).toBe(true)
  expect(sql.includes("WHEN substr(ts,1,10)>='2026-10-05' THEN 1e-9")).toBe(true)
  // The flat current rate is not used once history exists.
  expect(sql.includes('9e-8')).toBe(false)
})

test('buildSql with a single history day uses it as a flat rate', () => {
  const b = boundaries(new Date('2026-10-07T15:30:00'))
  const sql = buildSql('s', b, { input: 9e-8, output: 0 }, [
    { day: '2026-10-01', input: 4.2e-8, output: 0 },
  ])
  expect(sql.includes('*4.2e-8')).toBe(true)
  expect(sql.includes('CASE')).toBe(false)
})
