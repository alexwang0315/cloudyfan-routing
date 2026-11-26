import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Controls, LastTask, Pending } from '../types'
import { DEFINITION } from './definition'

const PANE = 'mrc-controls'
const controls = atom({ plugin: 'mrc-controls', key: 'controls' } as const, {} as Controls, { shape: 'controls-1' })
const pending = atom({ plugin: 'mrc-controls', key: 'pending' } as const, null as Pending | null)
const last = atom({ plugin: 'mrc-controls', key: 'last' } as const, null as LastTask | null)

type Field = keyof typeof DEFINITION.fields
const FIELDS = ['risk', 'route', 'setting', 'escalation', 'max_usd'] as const satisfies readonly Field[]

export function tags(c: Controls): string[] {
  const out: string[] = []
  for (const field of FIELDS) {
    const value = c[field]
    if (value === undefined) continue
    const spec = DEFINITION.fields[field]
    const tag = Object.entries(spec.tags).find(([, v]) => v === value)?.[0]
    if (tag !== undefined) out.push(tag)
    else if ('tag_with_argument' in spec) out.push(`${spec.tag_with_argument} ${value}`)
  }
  return out
}

export function signals(text: string): string[] {
  const lowered = text.toLowerCase()
  return DEFINITION.risk_checklist
    .filter(item => item.keywords.some(k => lowered.includes(k.toLowerCase())))
    .map(item => item.label)
}

export function statusText(c: Controls, task: LastTask | null): string {
  const risk = c.risk === 'high' ? '高風險' : '一般風險'
  const route = c.route === undefined ? '自動分流' : DEFINITION.fields.route.values[c.route]
  const parts = [risk, route, `模型 ${c.setting ?? '自動'}`, c.escalation === 'off' ? '不升級' : '自動升級',
    `上限 ${c.max_usd === undefined ? '無' : 'US$' + c.max_usd}`]
  const head = `MRC｜${parts.join('／')}`
  if (task === null) return head
  return `${head}｜${task.line}${task.total === null ? '' : '｜' + task.total}`
}

export function applyWords(c: Controls, words: string[]): Controls | string {
  const byLabel: Record<string, Field> = Object.fromEntries(FIELDS.map(f => [DEFINITION.fields[f].label, f]))
  const [label, word] = words
  const field = label === undefined ? undefined : byLabel[label]
  if (field === undefined || word === undefined || words.length !== 2) {
    return '用法：/mrc 風險 高｜/mrc 路徑 二｜/mrc 模型 sonnet-xhigh｜/mrc 升級 關｜/mrc 上限 0.5（「自動」「無」「開」改回預設）'
  }
  const spec = DEFINITION.fields[field]
  const next: Controls = { ...c }
  const known = (spec.words as Record<string, string | null>)[word]
  if (known === null) {
    delete next[field]
    return next
  }
  if (known !== undefined) {
    ;(next as Record<string, string>)[field] = known
    return next
  }
  if (field === 'max_usd' && /^(0|[1-9][0-9]{0,5})(\.[0-9]{1,6})?$/.test(word) && Number(word) > 0) {
    return { ...next, max_usd: word }
  }
  if (field === 'setting' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(word)) {
    return { ...next, setting: word }
  }
  return `${spec.command.slice(1)} 不接受「${word}」`
}

function contextBlock(c: Controls): string {
  return (
    'mrc-controls（mod）：使用者在 MRC 控制面板上設定了這題的控制，等同在訊息開頭加上這些單題標記：' +
    tags(c).join(' ') +
    '。照 mrc-controls skill 的規則把它們併入這題的 overrides。'
  )
}

async function refresh($: EngineInterface) {
  const c = await read($, controls)
  $.ui.status(c.enabled ? statusText(c, await read($, last)) : undefined)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'mrc',
      description: 'MRC 控制：開啟面板，或設定 風險／路徑／模型／升級／上限',
      argumentHint: '[風險 高｜路徑 二｜模型 <設定>｜升級 關｜上限 0.5]',
    })
    await refresh($)
    return next(e)
  })

  on('command.run', { command: 'mrc' }, async ($, e) => {
    const words = e.args.trim().split(/\s+/).filter(Boolean)
    if (words.length === 0) {
      await update($, controls, c => ({ ...c, enabled: true }))
      await $.ui.open({ id: PANE, title: 'MRC 控制' })
      await refresh($)
      return { text: 'MRC 控制已開啟。' }
    }
    const result = applyWords(await read($, controls), words)
    if (typeof result === 'string') return { text: result }
    await update($, controls, () => ({ ...result, enabled: true }))
    await refresh($)
    return { text: statusText(result, null) }
  })

  on('prompt.submit', async ($, e, next) => {
    const c = await read($, controls)
    if (e.command || !c.enabled) return next(e)
    const labels = signals(e.text)
    const hasRisk = c.risk !== undefined || /#(高風險|一般風險)/.test(e.text)
    if (labels.length > 0 && !hasRisk) {
      await update($, pending, () => ({ text: e.text, labels }))
      $.ui.invalidate('ui.render')
      return { drop: `MRC：這題可能是高風險（${labels.join('、')}）。請在輸入框上方選擇要不要改用高風險模式。` }
    }
    return tags(c).length === 0 ? next(e) : next({ ...e, context: [...(e.context ?? []), contextBlock(c)] })
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError || !e.command.includes('cloudyfan_jev.controls report')) return ran
    const lines = ran.result.stdout.split('\n')
    const line = lines.find(l => l.startsWith('本題：'))
    if (line !== undefined) {
      const total = lines.find(l => l.startsWith('累計：')) ?? null
      await update($, last, () => ({ line, total }))
      await refresh($)
    }
    return ran
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const held = await read($, pending)
    if (held === null) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const c = await read($, controls)
    const send = async (risk: 'high' | 'normal') => {
      await update($, pending, () => null)
      const prefix = tags({ ...c, risk }).join(' ')
      await $.prompt.submit({ text: `${prefix} ${held.text}`, asUser: true })
    }
    const cancel = async () => {
      await update($, pending, () => null)
      await $.prompt.fill({ text: held.text })
    }
    return (
      <Box flexDirection="column">
        <Text>
          MRC：這題可能是高風險（{held.labels.join('、')}）。{DEFINITION.high_risk_note}
        </Text>
        <Box>
          <Button key="high" label="改用高風險送出" onPress={() => send('high')} />
          <Button key="normal" label="維持一般送出" onPress={() => send('normal')} />
          <Button key="cancel" label="取消，放回輸入框" onPress={cancel} />
        </Box>
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const c = await read($, controls)
    const task = await read($, last)
    const set = (patch: Partial<Controls>, clear?: keyof Controls) => async () => {
      await update($, controls, old => {
        const next = { ...old, ...patch }
        if (clear !== undefined) delete next[clear]
        return next
      })
      await refresh($)
    }
    const row = (label: string, options: [string, boolean, () => Promise<void>][]) => (
      <Box key={label}>
        <Text>{label}：</Text>
        {options.map(([text, isOn, onPress]) => (
          <Button key={text} label={isOn ? `● ${text}` : text} onPress={onPress} />
        ))}
      </Box>
    )
    return (
      <Box flexDirection="column">
        {row('MRC 控制', [
          ['開', c.enabled === true, set({ enabled: true })],
          ['關', c.enabled !== true, set({ enabled: false })],
        ])}
        {row('風險', [
          ['一般', c.risk !== 'high', set({}, 'risk')],
          ['高', c.risk === 'high', set({ risk: 'high' })],
        ])}
        {row('路徑', [
          ['自動分流', c.route === undefined, set({}, 'route')],
          ['路徑一', c.route === 'path1', set({ route: 'path1' })],
          ['路徑二', c.route === 'path2', set({ route: 'path2' })],
        ])}
        {row('模型', [
          ['自動', c.setting === undefined, set({}, 'setting')],
          ...DEFINITION.suggested_settings.map(
            (s): [string, boolean, () => Promise<void>] => [s, c.setting === s, set({ setting: s })],
          ),
        ])}
        {row('升級', [
          ['自動', c.escalation === undefined, set({}, 'escalation')],
          ['不升級', c.escalation === 'off', set({ escalation: 'off' })],
        ])}
        {row('花費上限', [
          ['無', c.max_usd === undefined, set({}, 'max_usd')],
          ...DEFINITION.max_usd_presets.map(
            (v): [string, boolean, () => Promise<void>] => [`US$${v}`, c.max_usd === v, set({ max_usd: v })],
          ),
        ])}
        <Text dimColor>{task === null ? '還沒有送出的題目。' : `${task.line}${task.total ? '　' + task.total : ''}`}</Text>
        <Text dimColor>面板只組出覆寫；池外模型、超過契約上限的金額，MRC 一樣會拒絕。</Text>
      </Box>
    )
  })
}
