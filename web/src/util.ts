import type { EventType } from './types'

export const TYPE_COLORS: Record<EventType, string> = {
  explosion: '#c96a50',
  shelling: '#c89340',
  drone: '#8b6fbc',
  missile: '#bd576d',
  air_alert: '#d4c14f',
}

export const TYPE_ZH: Record<EventType, string> = {
  explosion: '爆炸',
  shelling: '炮击',
  drone: '无人机',
  missile: '导弹',
  air_alert: '空袭警报',
}

export const ALL_TYPES: EventType[] = ['explosion', 'shelling', 'drone', 'missile', 'air_alert']

export type Tier = 'fresh' | 'recent' | 'old'

export function tierOf(occurredAt: string | null, now: number): Tier {
  if (!occurredAt) return 'recent'
  const mins = (now - Date.parse(occurredAt)) / 60000
  if (mins <= 15) return 'fresh'
  if (mins <= 60) return 'recent'
  return 'old'
}

export function minsAgo(occurredAt: string | null, now: number): string {
  if (!occurredAt) return '时间未知'
  const mins = Math.round((now - Date.parse(occurredAt)) / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return m > 0 ? `${h} 小时 ${m} 分前` : `${h} 小时前`
}

export function fmtAbs(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`
}

export function tierOpacity(t: Tier): number {
  return t === 'fresh' ? 1 : t === 'recent' ? 0.78 : 0.44
}
