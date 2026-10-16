export type EventType = 'explosion' | 'shelling' | 'drone' | 'missile' | 'air_alert'
export type Verification = 'unverified' | 'auto_confirmed' | 'confirmed' | 'rejected'

export const VERIFICATION_ZH: Record<Verification, string> = {
  unverified: '未核实',
  auto_confirmed: '多源印证',
  confirmed: '已确认',
  rejected: '已拒绝',
}

export interface Place {
  id: number
  name: string
  name_original: string
  lat: number
  lng: number
  admin1: string | null
  admin1_name: string | null
  admin2: string | null
  precision: string
}

export interface MapEvent {
  id: number
  seq: number
  event_type: EventType
  place: Place | null
  geometry: { type: string; coordinates: number[] }
  occurred_at: string | null
  ended_at: string | null
  occurred_uncertainty_s: number | null
  verification: Verification
  report_count: number
  version: number
}

export interface EvidenceItem {
  message_id: number
  role: string
  text: string
  published_at: string
  forward_json: string
  source_id: number
  original_url: string | null
}

export interface EventDetail extends MapEvent {
  evidence: EvidenceItem[]
  reason: { at: string; operator?: string; action: string; reason?: string }[]
}

export interface PendingItem {
  candidate_id: number
  event_type: EventType | null
  place_raw: string
  place_id: number | null
  reason: string | null
  message_text: string
  published_at: string
}

// 地名库条目(管理端)
export interface PlaceListItem {
  id: number
  name: string
  name_zh: string | null
  lat: number
  lng: number
  admin1: string | null
  admin2: string | null
  precision: string
  source: string
}

export interface PlaceDetail extends PlaceListItem {
  names: string[]
}

export interface PlaceInput {
  canonical_name: string
  names: string[]
  lat: number
  lng: number
  admin1: string | null
  admin2: string | null
  precision: string
  name_zh: string | null
}

// 待补地名:报文中出现但地名库未收录的专名
export interface UnknownPlace {
  id: number
  name: string
  raw: string
  report_count: number
  status: string
  context: string
  sample_message_id: number | null
  source_id: number | null
  first_seen_at: string | null
  last_seen_at: string | null
  resolved_place_id: number | null
}

export interface RematchResult {
  unknown: { unknown_resolved: number; messages_reprocessed: number; events: number }
  pending: { retried: number; resolved: number; events_touched: number; auto_confirmed: number }
}
