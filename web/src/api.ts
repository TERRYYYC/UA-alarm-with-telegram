import type {
  EventDetail,
  MapEvent,
  PendingItem,
  PlaceDetail,
  PlaceInput,
  PlaceListItem,
  RematchResult,
  UnknownPlace,
} from './types'

export interface SourceInfo {
  id: number
  adapter: string
  name: string
  enabled: boolean
  last_success_at: string | null
  gap_notes: string
}

async function req<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json() as Promise<T>
}

export function fetchEvents(windowMinutes: number, types: string[]): Promise<MapEvent[]> {
  const p = new URLSearchParams({ window_minutes: String(windowMinutes) })
  if (types.length) p.set('types', types.join(','))
  return fetch(`/api/v1/events?${p.toString()}`).then((r) => req<MapEvent[]>(r))
}

export function fetchDetail(id: number): Promise<EventDetail> {
  return fetch(`/api/v1/events/${id}`).then((r) => req<EventDetail>(r))
}

export function fetchPending(): Promise<PendingItem[]> {
  return fetch('/api/v1/pending').then((r) => req<PendingItem[]>(r))
}

export function fetchSources(): Promise<SourceInfo[]> {
  return fetch('/api/v1/sources').then((r) => req<SourceInfo[]>(r))
}

export function fetchPlaces(q: string): Promise<PlaceListItem[]> {
  return fetch(`/api/v1/places?q=${encodeURIComponent(q)}&limit=30`).then((r) => req<PlaceListItem[]>(r))
}

export function fetchPlaceDetail(id: number): Promise<PlaceDetail> {
  return fetch(`/api/v1/places/${id}`).then((r) => req<PlaceDetail>(r))
}

export function createPlace(body: PlaceInput): Promise<PlaceDetail> {
  return fetch('/api/v1/places', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => req<PlaceDetail>(r))
}

export function updatePlace(id: number, body: PlaceInput): Promise<PlaceDetail> {
  return fetch(`/api/v1/places/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => req<PlaceDetail>(r))
}

export function fetchUnknownPlaces(status = 'open'): Promise<UnknownPlace[]> {
  return fetch(`/api/v1/unknown-places?status=${status}&limit=50`).then((r) => req<UnknownPlace[]>(r))
}

export function ignoreUnknownPlace(id: number): Promise<{ status: string }> {
  return fetch(`/api/v1/unknown-places/${id}/ignore`, { method: 'POST' }).then((r) =>
    req<{ status: string }>(r),
  )
}

export function rematchAll(): Promise<RematchResult> {
  return fetch('/api/v1/admin/rematch', { method: 'POST' }).then((r) => req<RematchResult>(r))
}

export function correctEvent(
  id: number,
  body: { verification: string; reason: string; occurred_at?: string | null; place_id?: number | null },
): Promise<MapEvent> {
  return fetch(`/api/v1/events/${id}/correct`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => req<MapEvent>(r))
}

export function splitEvent(id: number): Promise<{ split_into: number[] }> {
  return fetch(`/api/v1/events/${id}/split`, { method: 'POST' }).then((r) =>
    req<{ split_into: number[] }>(r),
  )
}

export function resolvePending(
  id: number,
  body: { place_id: number; event_type?: string | null; occurred_at?: string | null },
): Promise<MapEvent> {
  return fetch(`/api/v1/pending/${id}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => req<MapEvent>(r))
}

export function rejectPending(id: number, reason: string): Promise<{ status: string }> {
  return fetch(`/api/v1/pending/${id}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  }).then((r) => req<{ status: string }>(r))
}

export function openEventStream(
  onEvent: (e: MapEvent) => void,
  onStatus: (s: string) => void,
): () => void {
  const es = new EventSource('/api/v1/stream?last_id=0')
  es.onopen = () => onStatus('connected')
  es.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data) as MapEvent)
    } catch {
      /* ignore malformed frame */
    }
  }
  es.onerror = () => onStatus('reconnecting')
  return () => es.close()
}
