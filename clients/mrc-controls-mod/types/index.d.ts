export type Controls = {
  enabled?: boolean
  risk?: 'normal' | 'high'
  route?: 'path1' | 'path2'
  setting?: string
  escalation?: 'off'
  max_usd?: string
}
export type Pending = { text: string; labels: string[] }
export type LastTask = { line: string; total: string | null }

declare module 'claude-code' {
  interface PluginState {
    'mrc-controls': { controls: Shaped<Controls>; pending: Pending | null; last: LastTask | null }
  }
}
