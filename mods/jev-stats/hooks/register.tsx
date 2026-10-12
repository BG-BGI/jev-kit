import { atom, read, update } from 'claude-code'
import type { Register, EngineInterface } from 'claude-code'

import type { PeriodStats, Stats } from '../types'
import { boundaries, buildModelSql, buildSql, fmt, pad, parseModelRows, parseRows, shortModel } from './lib'

const PANE = 'jev-stats'
const DB_SH = 'exec sqlite3 -json "$HOME/.local/state/airlock/metrics.db"'
const SCRAPE_SH =
  'cd "$HOME/.local/share/airlock/current" && exec python3 -m airlock.metrics scrape-compaction'

const stats = atom({ plugin: 'jev-stats', key: 'stats' } as const, null)
const sessionId = atom({ plugin: 'jev-stats', key: 'sessionId' } as const, '')

async function refresh($: EngineInterface): Promise<void> {
  const sid = await read($, sessionId)
  const b = boundaries(new Date())
  const query = async (sql: string): Promise<unknown> => {
    const ran = await $.process.run(['sh', '-c', DB_SH], { stdin: sql, timeoutMs: 10000 })
    if (ran.exitCode !== 0) return undefined
    try {
      return JSON.parse(ran.stdout || '[]')
    } catch {
      return undefined
    }
  }
  const rows = await query(buildSql(sid, b))
  if (rows === undefined) return
  // The model table is optional: an old db without the column just shows none.
  const modelRows = await query(buildModelSql(sid, b))
  const next = parseRows(rows, new Date().toLocaleTimeString(), parseModelRows(modelRows))
  await update($, stats, () => next)
  const day = next.periods.day
  $.ui.status(`jev ${fmt(day.n)} ev · ${fmt(day.denied)} deny · ${fmt(day.eff)} tok saved today`)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const sid = await $.session.id()
    await update($, sessionId, () => sid)
    await $.command.register({
      name: 'jev-stats',
      description: 'Show live jev-kit metrics for this session, today, this week and this month',
    })
    void refresh($)
    $.clock.every(5000, () => refresh($))
    // Compaction rows come from transcript scrapes; keep them fresh too.
    $.clock.every(180000, () =>
      $.process.run(['sh', '-c', SCRAPE_SH], { timeoutMs: 60000 }).catch(() => undefined),
    )

    return next(e)
  })

  on('command.run', { command: 'jev-stats' }, async $ => {
    await $.ui.open({ id: PANE, title: 'jev stats' })
    void refresh($)

    return { text: 'jev stats pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s: Stats | null = await read($, stats)
    if (!s) {
      return (
        <Box flexDirection="column">
          <Text dimColor>Waiting for first read of metrics.db...</Text>
        </Box>
      )
    }
    const cols: Array<['session' | 'day' | 'week' | 'month', string]> = [
      ['session', 'sess'],
      ['day', 'day'],
      ['week', 'week'],
      ['month', 'month'],
    ]
    const W = 9
    const NAME_W = 18
    const line = (label: string, value: (p: PeriodStats) => string) =>
      label.padEnd(12) + cols.map(([k]) => pad(value(s.periods[k]), W)).join('')

    return (
      <Box flexDirection="column">
        <Text bold>{' '.repeat(12) + cols.map(([, h]) => pad(h, W)).join('')}</Text>
        <Text>{line('events', p => fmt(p.n))}</Text>
        <Text>{line('denies', p => fmt(p.denied))}</Text>
        <Text>{line('jev calls', p => fmt(p.jev))}</Text>
        <Text>{line('jev tokens', p => fmt(p.tok))}</Text>
        <Text>{line('jev cost', p => '$' + p.cost.toFixed(3))}</Text>
        <Text>{line('trim saved', p => fmt(p.saved))}</Text>
        <Text>{line('total saved', p => fmt(p.eff))}</Text>
        {s.models.length > 0 && (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>{'saved by model'.padEnd(NAME_W) + cols.map(([, h]) => pad(h, W)).join('')}</Text>
            {s.models.map(m => (
              <Text key={m.model}>
                {shortModel(m.model).slice(0, NAME_W - 1).padEnd(NAME_W) +
                  cols.map(([k]) => pad(fmt(m.eff[k]), W)).join('')}
              </Text>
            ))}
          </Box>
        )}
        <Text dimColor>tokens; total saved = trim x later requests. {s.updatedAt}</Text>
      </Box>
    )
  })
}
