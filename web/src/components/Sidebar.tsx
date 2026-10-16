import { useEffect, useState } from 'react'
import type { EventDetail, EventType, MapEvent, PendingItem, PlaceListItem } from '../types'
import { VERIFICATION_ZH } from '../types'
import { fetchPlaces } from '../api'
import { ALL_TYPES, fmtAbs, minsAgo, TYPE_COLORS, TYPE_ZH } from '../util'

interface Props {
  events: MapEvent[]
  now: number
  selectedId: number | null
  detail: EventDetail | null
  onSelect: (id: number | null) => void
  pending: PendingItem[]
  showPending: boolean
  onClosePending: () => void
  onResolve: (cand: PendingItem, placeId: number, eventType: string, occurredAt: string | null) => Promise<void>
  onDismiss: (cand: PendingItem) => void
  onConfirm: () => void
  onReject: () => void
  onSplit: () => void
  busy: boolean
  onQuickAddPlace: (name: string) => void
}

function TypeChip({ t }: { t: EventType }) {
  return (
    <span className="type-chip" style={{ color: TYPE_COLORS[t], borderColor: `${TYPE_COLORS[t]}55` }}>
      {TYPE_ZH[t]}
    </span>
  )
}

function Feed({ events, now, selectedId, onSelect }: Pick<Props, 'events' | 'now' | 'selectedId' | 'onSelect'>) {
  return (
    <>
      <div className="side-head">
        <b>实时事件流</b>
        <span className="muted">{events.length} 条</span>
      </div>
      <div className="feed">
        {events.map((e) => (
          <div
            key={e.id}
            className={e.id === selectedId ? 'item on' : 'item'}
            onClick={() => onSelect(e.id)}
          >
            <span className="dot" style={{ background: TYPE_COLORS[e.event_type] }} />
            <TypeChip t={e.event_type} />
            <span className="place">{e.place?.name ?? '—'}</span>
            <span className="meta">
              {minsAgo(e.occurred_at, now)} · {e.report_count} 报告
              {e.verification === 'unverified' ? ' · 未核实' : e.verification === 'auto_confirmed' ? ' · 多源印证' : ''}
            </span>
          </div>
        ))}
        {events.length === 0 && <div className="empty">窗口内没有事件</div>}
      </div>
    </>
  )
}

function Detail({ detail, onClose, onConfirm, onReject, onSplit, busy }: {
  detail: EventDetail
  onClose: () => void
  onConfirm: () => void
  onReject: () => void
  onSplit: () => void
  busy: boolean
}) {
  const t = detail.event_type
  return (
    <>
      <div className="side-head">
        <button className="back" onClick={onClose}>← 返回</button>
        <b>{detail.place?.name ?? '—'}</b>
        <TypeChip t={t} />
      </div>
      <div className="detail">
        <div className="kv">
          <span>发生时间</span>
          <span>{fmtAbs(detail.occurred_at)}
            {detail.occurred_uncertainty_s ? ` (±${Math.round(detail.occurred_uncertainty_s / 60)} 分)` : ''}
          </span>
        </div>
        <div className="kv">
          <span>核验状态</span>
          <span className={`tag ${detail.verification}`}>{VERIFICATION_ZH[detail.verification]}</span>
        </div>
        <div className="kv">
          <span>报告数</span>
          <span>{detail.report_count}</span>
        </div>
        <div className="kv">
          <span>地点</span>
          <span>
            {detail.place?.name ?? '—'}
            {detail.place && detail.place.name_original !== detail.place.name
              ? `（${detail.place.name_original}）`
              : ''}
          </span>
        </div>
        <div className="kv">
          <span>行政区</span>
          <span>{detail.place?.admin1_name ?? detail.place?.admin1 ?? '—'} · {detail.place?.precision ?? '—'}</span>
        </div>
        <div className="sec-title">证据({detail.evidence.length})</div>
        <div className="evidence">
          {detail.evidence.map((ev) => (
            <div key={ev.message_id} className="ev">
              <p>{ev.text}</p>
              <span className="muted">
                {fmtAbs(ev.published_at)}
                {ev.forward_json && ev.forward_json !== '{}' ? ' · 转发' : ''}
              </span>
              {ev.original_url && (
                <a className="ev-link" href={ev.original_url} target="_blank" rel="noopener noreferrer">
                  原文 ↗
                </a>
              )}
            </div>
          ))}
        </div>
        {detail.reason.length > 0 && (
          <>
            <div className="sec-title">修订记录</div>
            <div className="evidence">
              {detail.reason.map((r, i) => (
                <div key={i} className="ev">
                  <p>{r.action}{r.reason ? `: ${r.reason}` : ''}{r.operator ? ` · ${r.operator}` : ''}</p>
                  <span className="muted">{r.at}</span>
                </div>
              ))}
            </div>
          </>
        )}
        <div className="actions">
          <button disabled={busy} onClick={onConfirm}>确认</button>
          <button disabled={busy} onClick={onReject}>拒绝</button>
          <button disabled={busy} onClick={onSplit}>拆分</button>
        </div>
      </div>
    </>
  )
}

function PendingView({ pending, onClose, onResolve, onDismiss, busy, onQuickAddPlace }: {
  pending: PendingItem[]
  onClose: () => void
  onResolve: Props['onResolve']
  onDismiss: Props['onDismiss']
  busy: boolean
  onQuickAddPlace: Props['onQuickAddPlace']
}) {
  const [openId, setOpenId] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [places, setPlaces] = useState<PlaceListItem[]>([])
  const [placeId, setPlaceId] = useState<string>('')
  const [etype, setEtype] = useState<string>('shelling')
  const [time, setTime] = useState<string>('')

  useEffect(() => {
    if (openId == null) return
    setQuery('')
    setPlaceId('')
    setTime('')
    fetchPlaces('').then(setPlaces).catch(() => setPlaces([]))
  }, [openId])

  useEffect(() => {
    if (openId == null) return
    const t = setTimeout(() => {
      fetchPlaces(query).then(setPlaces).catch(() => setPlaces([]))
    }, 250)
    return () => clearTimeout(t)
  }, [query, openId])

  return (
    <>
      <div className="side-head">
        <button className="back" onClick={onClose}>← 返回</button>
        <b>待处理</b>
        <span className="muted">{pending.length} 项</span>
      </div>
      <div className="feed">
        {pending.map((c) => (
          <div key={c.candidate_id} className="pitem">
            <div className="pitem-head" onClick={() => setOpenId(openId === c.candidate_id ? null : c.candidate_id)}>
              <span className="place">{c.place_raw || '(无地名)'}</span>
              <span className="muted">{c.reason === 'ambiguous_place' ? '同名歧义' : c.reason === 'unknown_type' ? '类型未知' : '未匹配'}</span>
            </div>
            <p className="ptext">{c.message_text}</p>
            <div className="pitem-actions">
              {c.place_raw && (
                <button
                  className="dismiss"
                  disabled={busy}
                  title="地名库没有这个地名时,去补录并重新匹配"
                  onClick={() => onQuickAddPlace(c.place_raw)}
                >
                  补录地名
                </button>
              )}
              <button className="dismiss" disabled={busy} onClick={() => onDismiss(c)}>
                忽略(噪音/非事件)
              </button>
            </div>
            {openId === c.candidate_id && (
              <div className="resolve">
                <input
                  placeholder="搜索地名…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
                <select value={placeId} onChange={(e) => setPlaceId(e.target.value)}>
                  <option value="">选择地点</option>
                  {places.map((p) => (
                    <option key={p.id} value={String(p.id)}>
                      {p.name}{p.admin1 ? ` (${p.admin1})` : ''}
                    </option>
                  ))}
                </select>
                <select value={etype} onChange={(e) => setEtype(e.target.value)}>
                  {ALL_TYPES.map((t) => (
                    <option key={t} value={t}>{TYPE_ZH[t]}</option>
                  ))}
                </select>
                <input
                  type="datetime-local"
                  value={time}
                  onChange={(e) => setTime(e.target.value)}
                />
                <button
                  disabled={busy || !placeId}
                  onClick={async () => {
                    const iso = time ? new Date(time).toISOString() : null
                    await onResolve(c, Number(placeId), etype, iso)
                    setOpenId(null)
                  }}
                >
                  解析为事件
                </button>
              </div>
            )}
          </div>
        ))}
        {pending.length === 0 && <div className="empty">队列已清空</div>}
      </div>
    </>
  )
}

export default function Sidebar(props: Props) {
  const { showPending, pending, detail, selectedId } = props
  return (
    <aside className="side">
      {showPending ? (
        <PendingView
          pending={pending}
          onClose={props.onClosePending}
          onResolve={props.onResolve}
          onDismiss={props.onDismiss}
          busy={props.busy}
          onQuickAddPlace={props.onQuickAddPlace}
        />
      ) : detail && selectedId != null ? (
        <Detail
          detail={detail}
          onClose={() => props.onSelect(null)}
          onConfirm={props.onConfirm}
          onReject={props.onReject}
          onSplit={props.onSplit}
          busy={props.busy}
        />
      ) : (
        <Feed
          events={props.events}
          now={props.now}
          selectedId={selectedId}
          onSelect={props.onSelect}
        />
      )}
    </aside>
  )
}
