import type { CSSProperties } from 'react'
import type { EventType } from '../types'
import { ALL_TYPES, TYPE_COLORS, TYPE_ZH } from '../util'

interface Props {
  windowMin: number
  setWindowMin: (m: number) => void
  types: Set<EventType>
  toggleType: (t: EventType) => void
  pendingCount: number
  onOpenPending: () => void
  unknownCount: number
  onOpenGeodata: () => void
  status: string
  demoMode: boolean
}

const PRESETS = [15, 60, 120, 180]

export default function Toolbar({
  windowMin,
  setWindowMin,
  types,
  toggleType,
  pendingCount,
  onOpenPending,
  unknownCount,
  onOpenGeodata,
  status,
  demoMode,
}: Props) {
  return (
    <div className="toolbar">
      <div className="brand">
        <span className="status-dot" data-status={status} title={status} />
        <b>战况监控</b>
        {demoMode && <span className="demo-tag">本地样本数据 · 非真实事件</span>}
      </div>
      <div className="seg">
        {PRESETS.map((m) => (
          <button key={m} className={windowMin === m ? 'on' : ''} onClick={() => setWindowMin(m)}>
            {m} 分钟
          </button>
        ))}
      </div>
      <div className="types">
        {ALL_TYPES.map((t) => (
          <button
            key={t}
            className={types.has(t) ? 'chip on' : 'chip'}
            style={{ '--c': TYPE_COLORS[t] } as CSSProperties}
            onClick={() => toggleType(t)}
          >
            <i style={{ background: TYPE_COLORS[t] }} />
            {TYPE_ZH[t]}
          </button>
        ))}
      </div>
      <button className="pending-btn" onClick={onOpenPending}>
        待处理{pendingCount > 0 ? ` · ${pendingCount}` : ''}
      </button>
      <button className="pending-btn geo-btn" onClick={onOpenGeodata} title="地名库:检索、编辑、补录">
        地名库{unknownCount > 0 ? ` · 待补 ${unknownCount}` : ''}
      </button>
    </div>
  )
}
