import { useEffect, useMemo, useState } from 'react'
import MapView from './components/MapView'
import Sidebar from './components/Sidebar'
import Toolbar from './components/Toolbar'
import Geodata, { type GeoPick } from './components/Geodata'
import {
  correctEvent,
  fetchDetail,
  fetchEvents,
  fetchPending,
  fetchSources,
  fetchUnknownPlaces,
  openEventStream,
  rejectPending,
  resolvePending,
  splitEvent,
} from './api'
import type { EventDetail, EventType, MapEvent, PendingItem } from './types'

// 发布视图:截图渲染时隐藏运营界面,只保留地图与冻结视图标签
const exportMode = new URLSearchParams(window.location.search).has('export')
// 发布上下文窗口(?window=分钟):默认 120;发布器可用更长窗口避免空图
const windowParam = parseInt(new URLSearchParams(window.location.search).get('window') ?? '', 10)

export default function App() {
  const [eventsById, setEventsById] = useState<Record<number, MapEvent>>({})
  const [windowMin, setWindowMin] = useState(Number.isFinite(windowParam) && windowParam > 0 ? windowParam : 120)
  const [types, setTypes] = useState<Set<EventType>>(new Set())
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<EventDetail | null>(null)
  const [pending, setPending] = useState<PendingItem[]>([])
  const [showPending, setShowPending] = useState(false)
  const [status, setStatus] = useState('connecting')
  const [busy, setBusy] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const [demoMode, setDemoMode] = useState(true)
  const [showGeodata, setShowGeodata] = useState(false)
  const [unknownCount, setUnknownCount] = useState(0)
  const [picking, setPicking] = useState(false)
  const [geoPrefill, setGeoPrefill] = useState<string | null>(null)
  const [picked, setPicked] = useState<GeoPick | null>(null)
  const [focus, setFocus] = useState<{ lng: number; lat: number } | null>(null)

  const visible = useMemo(() => {
    const cutoff = now - windowMin * 60000
    return Object.values(eventsById)
      .filter(
        (e) =>
          e.verification !== 'rejected' &&
          e.occurred_at != null &&
          Date.parse(e.occurred_at) >= cutoff &&
          (types.size === 0 || types.has(e.event_type)),
      )
      .sort((a, b) => (b.occurred_at ?? '').localeCompare(a.occurred_at ?? ''))
  }, [eventsById, types, windowMin, now])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const list = await fetchEvents(windowMin, [...types])
        if (!alive) return
        setEventsById((prev) => {
          const next = { ...prev }
          for (const e of list) next[e.id] = e
          return next
        })
        setStatus('connected')
      } catch {
        if (alive) setStatus('api-error')
      }
      try {
        const p = await fetchPending()
        if (alive) setPending(p)
      } catch {
        /* keep previous queue */
      }
      try {
        const srcs = await fetchSources()
        if (alive) setDemoMode(!srcs.some((s) => s.adapter === 'telegram'))
      } catch {
        /* keep previous mode */
      }
      try {
        const unk = await fetchUnknownPlaces()
        if (alive) setUnknownCount(unk.length)
      } catch {
        /* keep previous count */
      }
    }
    load()
    const close = openEventStream(
      (e) => setEventsById((prev) => ({ ...prev, [e.id]: e })),
      (s) => setStatus(s),
    )
    const timer = setInterval(() => {
      setNow(Date.now())
      load()
    }, 60000)
    return () => {
      alive = false
      close()
      clearInterval(timer)
    }
  }, [windowMin, types])

  useEffect(() => {
    if (selectedId == null) {
      setDetail(null)
      return
    }
    let alive = true
    fetchDetail(selectedId)
      .then((d) => {
        if (alive) setDetail(d)
      })
      .catch(() => {
        /* ignore */
      })
    return () => {
      alive = false
    }
  }, [selectedId])

  async function reloadAll() {
    const list = await fetchEvents(windowMin, [...types])
    setEventsById((prev) => {
      const next = { ...prev }
      for (const e of list) next[e.id] = e
      return next
    })
    setPending(await fetchPending())
    setUnknownCount((await fetchUnknownPlaces()).length)
    if (selectedId != null) setDetail(await fetchDetail(selectedId))
  }

  async function handleCorrect(verification: 'confirmed' | 'rejected') {
    if (!detail) return
    const reason =
      window.prompt(verification === 'confirmed' ? '确认理由(可选)' : '拒绝理由(可选)') ?? ''
    setBusy(true)
    try {
      await correctEvent(detail.id, { verification, reason })
      await reloadAll()
    } finally {
      setBusy(false)
    }
  }

  async function handleSplit() {
    if (!detail) return
    setBusy(true)
    try {
      await splitEvent(detail.id)
      setSelectedId(null)
      await reloadAll()
    } finally {
      setBusy(false)
    }
  }

  async function handleResolve(
    cand: PendingItem,
    placeId: number,
    eventType: string,
    occurredAt: string | null,
  ) {
    setBusy(true)
    try {
      await resolvePending(cand.candidate_id, { place_id: placeId, event_type: eventType, occurred_at: occurredAt })
      await reloadAll()
    } finally {
      setBusy(false)
    }
  }

  async function handleReject(cand: PendingItem) {
    setBusy(true)
    try {
      await rejectPending(cand.candidate_id, 'operator dismissed')
      await reloadAll()
    } finally {
      setBusy(false)
    }
  }

  function handlePick(lng: number, lat: number) {
    setPicked({ lng, lat })
    setFocus({ lng, lat })
    setPicking(false)
  }

  async function refreshAfterGeoChange() {
    setUnknownCount((await fetchUnknownPlaces()).length)
    await reloadAll()
  }

  function toggleType(t: EventType) {
    setTypes((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  return (
    <div className="app">
      {!exportMode && (
        <Toolbar
          windowMin={windowMin}
          setWindowMin={setWindowMin}
          types={types}
          toggleType={toggleType}
          pendingCount={pending.length}
          onOpenPending={() => {
            setShowPending(true)
            setShowGeodata(false)
          }}
          unknownCount={unknownCount}
          onOpenGeodata={() => {
            setShowGeodata((v) => !v)
            setPicking(false)
          }}
          status={status}
          demoMode={demoMode}
        />
      )}
      <div className="main">
        <MapView
          events={visible}
          now={now}
          selectedId={selectedId}
          onSelect={setSelectedId}
          picking={picking}
          onPick={handlePick}
          focus={focus}
        />
        {!exportMode && showGeodata && (
          <aside className="side">
            <Geodata
              picked={picked}
              picking={picking}
              onTogglePick={() => setPicking((v) => !v)}
              onSaved={refreshAfterGeoChange}
              onFocus={(lng, lat) => setFocus({ lng, lat })}
              prefill={geoPrefill}
              onPrefillUsed={() => setGeoPrefill(null)}
            />
          </aside>
        )}
        {!exportMode && !showGeodata && (
          <Sidebar
            events={visible}
            now={now}
            selectedId={selectedId}
            detail={detail}
            onSelect={setSelectedId}
            pending={pending}
            showPending={showPending}
            onClosePending={() => setShowPending(false)}
            onResolve={handleResolve}
            onDismiss={handleReject}
            onConfirm={() => handleCorrect('confirmed')}
            onReject={() => handleCorrect('rejected')}
            onSplit={handleSplit}
            busy={busy}
            onQuickAddPlace={(name) => {
              setShowPending(false)
              setShowGeodata(true)
              setGeoPrefill(name)
            }}
          />
        )}
      </div>
      {exportMode && (
        <div className="export-tag" id="export-tag">
          战况地图 · 发布视图 · 时间窗 {windowMin} 分钟 · 事件 {visible.length}
          {demoMode ? ' · 本地样本数据(非真实事件)' : ' · 实时采集'}
        </div>
      )}
    </div>
  )
}
