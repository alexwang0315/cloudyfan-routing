import { expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { applyWords, signals, statusText, tags } from '../hooks/register'

test('the panel settings become the same tags the skill reads', async () => {
  expect(tags({ risk: 'high', route: 'path2', escalation: 'off', max_usd: '0.50', setting: 'sonnet-xhigh' })).toEqual([
    '#高風險',
    '#路徑二',
    '#模型 sonnet-xhigh',
    '#不升級',
    '#上限 0.50',
  ])
  expect(tags({ enabled: true })).toEqual([])
})

test('/mrc words set and clear one control', async () => {
  expect(applyWords({}, ['風險', '高'])).toEqual({ risk: 'high' })
  expect(applyWords({ risk: 'high', route: 'path2' }, ['路徑', '自動'])).toEqual({ risk: 'high' })
  expect(applyWords({}, ['上限', '0.5'])).toEqual({ max_usd: '0.5' })
  expect(typeof applyWords({}, ['上限', '-1'])).toBe('string')
  expect(typeof applyWords({}, ['顏色', '紅'])).toBe('string')
})

test('risk checklist and status line', async () => {
  expect(signals('明天對外發布新聞稿給客戶')).toEqual(['對外發布', '牽涉客戶'])
  expect(signals('把 1 加 1')).toEqual([])
  expect(statusText({ risk: 'high' }, { line: '本題：開放題／x／高風險／約 US$0.11', total: null })).toBe(
    'MRC｜高風險／自動分流／模型 自動／自動升級／上限 無｜本題：開放題／x／高風險／約 US$0.11',
  )
})

const COMPOSER = { kind: 'composer' } as const
const mrc = (args: string) => ({ command: 'mrc', args, origin: COMPOSER, presentation: { isFullscreen: false, columns: 80 } })
const typed = (text: string) => ({ text, wait: false, origin: COMPOSER })

async function engine(on: On) {
  on('command.register', () => ({ value: { command: 'mrc' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('prompt.submit', (_, e) => ({ text: e.text, context: e.context }))
}

test('a risky prompt is held for the person to decide; a set risk passes with context', async ($, on) => {
  await engine(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run(mrc(''))

  const held = await $.prompt.submit(typed('明天對外發布新聞稿'))
  expect('drop' in held && held.drop).toContain('對外發布')

  await $.command.run(mrc('風險 高'))
  const sent = await $.prompt.submit(typed('明天對外發布新聞稿'))
  expect('text' in sent && sent.context?.some(c => c.includes('#高風險'))).toBe(true)
})

test('with the controls off, prompts pass untouched', async ($, on) => {
  await engine(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const sent = await $.prompt.submit(typed('刪除這個檔案'))
  expect('text' in sent && sent.text).toBe('刪除這個檔案')
  expect('text' in sent && (sent.context ?? []).length).toBe(0)
})
