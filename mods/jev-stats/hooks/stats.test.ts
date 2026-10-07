import { expect, test } from 'claude-code/testing'

import { boundaries, buildSql, fmt, parseRows } from './lib'

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
