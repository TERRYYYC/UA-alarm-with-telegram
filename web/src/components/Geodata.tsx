import { useEffect, useMemo, useState } from 'react'
import type { PlaceDetail, PlaceInput, PlaceListItem, UnknownPlace } from '../types'
import {
  createPlace,
  fetchPlaceDetail,
  fetchPlaces,
  fetchUnknownPlaces,
  ignoreUnknownPlace,
  rematchAll,
  updatePlace,
} from '../api'

const PRECISIONS: [string, string][] = [
  ['region', '州'],
  ['district', '区'],
  ['city', '城市'],
  ['town', '镇'],
  ['village', '村镇'],
  ['settlement', '居民点'],
]

const EMPTY_FORM: PlaceInput = {
  canonical_name: '',
  names: [],
  lat: NaN,
  lng: NaN,
  admin1: null,
  admin2: null,
  precision: 'settlement',
  name_zh: null,
}

export interface GeoPick {
  lat: number
  lng: number
}

interface Props {
  picked: GeoPick | null
  picking: boolean
  onTogglePick: () => void
  onSaved: () => void
  onFocus: (lng: number, lat: number) => void
  /** 从"待处理"跳转过来时预填的未解析地名 */
  prefill?: string | null
  onPrefillUsed?: () => void
}

export default function Geodata({
  picked,
  picking,
  onTogglePick,
  onSaved,
  onFocus,
  prefill,
  onPrefillUsed,
}: Props) {
  const [tab, setTab] = useState<'unknown' | 'search'>('unknown')
  const [unknown, setUnknown] = useState<UnknownPlace[]>([])
  const [places, setPlaces] = useState<PlaceListItem[]>([])
  const [query, setQuery] = useState('')
  const [editId, setEditId] = useState<number | null>(null)
  const [form, setForm] = useState<PlaceInput>(EMPTY_FORM)
  const [aliasText, setAliasText] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const refreshUnknown = () => fetchUnknownPlaces().then(setUnknown).catch(() => setUnknown([]))

  useEffect(() => {
    refreshUnknown()
  }, [])

  useEffect(() => {
    if (tab !== 'search') return
    if (!query.trim()) {
      setPlaces([]) // 空查询不列出全库(3 万条地名按字母序没有参考价值)
      return
    }
    const t = setTimeout(() => {
      fetchPlaces(query)
        .then(setPlaces)
        .catch(() => setPlaces([]))
    }, 220)
    return () => clearTimeout(t)
  }, [query, tab])

  // 从待处理队列跳过来:直接进入补录表单
  useEffect(() => {
    if (!prefill) return
    setTab('search')
    setEditId(null)
    setForm({ ...EMPTY_FORM, canonical_name: prefill })
    setAliasText(prefill)
    setNote(`「${prefill}」未收录:确认坐标(可在地图上点击)后保存,再点「重新匹配」`)
    onPrefillUsed?.()
  }, [prefill])

  // 地图拾取结果 → 写入编辑表单
  useEffect(() => {
    if (!picked) return
    setForm((f) => ({ ...f, lat: picked.lat, lng: picked.lng }))
  }, [picked])

  const canSave = useMemo(
    () => form.canonical_name.trim().length > 0 && Number.isFinite(form.lat) && Number.isFinite(form.lng),
    [form],
  )

  function startCreate(name = '', prefill?: Partial<PlaceInput>) {
    setEditId(null)
    setForm({ ...EMPTY_FORM, canonical_name: name, ...prefill })
    setAliasText(name)
    setNote(name ? '补录新地名:先在地图上点击该地位置' : '新增地名')
    setTab('search')
  }

  async function startEdit(id: number) {
    setBusy(true)
    try {
      const d: PlaceDetail = await fetchPlaceDetail(id)
      setEditId(d.id)
      setForm({
        canonical_name: d.name,
        names: d.names,
        lat: d.lat,
        lng: d.lng,
        admin1: d.admin1,
        admin2: d.admin2,
        precision: d.precision,
        name_zh: d.name_zh,
      })
      setAliasText(d.names.join(', '))
      setNote(`编辑 #${d.id} · ${d.source}`)
      onFocus(d.lng, d.lat)
    } catch {
      setNote('读取失败')
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    if (!canSave) return
    setBusy(true)
    try {
      const body: PlaceInput = {
        ...form,
        canonical_name: form.canonical_name.trim(),
        names: aliasText
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
      }
      const saved = editId == null ? await createPlace(body) : await updatePlace(editId, body)
      setEditId(saved.id)
      setNote(`已保存 #${saved.id} ${saved.name}${saved.name_zh ? ` · ${saved.name_zh}` : ''} — 记得点「重新匹配」回填历史报文`)
      await refreshUnknown()
      if (query.trim()) setPlaces(await fetchPlaces(query))
      onSaved()
    } catch (e) {
      setNote(`保存失败:${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  async function rematch() {
    setBusy(true)
    setNote('重新匹配中…')
    try {
      const r = await rematchAll()
      setNote(
        `重新匹配完成:补齐地名 ${r.unknown.unknown_resolved} 个 · 回填报文 ${r.unknown.messages_reprocessed} 条 · ` +
          `待处理候选重试 ${r.pending.retried}(解析 ${r.pending.resolved})· 自动确认 ${r.pending.auto_confirmed}`,
      )
      await refreshUnknown()
      onSaved()
    } catch (e) {
      setNote(`重新匹配失败:${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="side-head">
        <b>地名库维护</b>
        <span className="muted">待补 {unknown.length}</span>
        <button className="ghost" disabled={busy} onClick={rematch} title="补齐地名后回填历史报文">
          重新匹配
        </button>
      </div>
      <div className="tabs">
        <button className={tab === 'unknown' ? 'on' : ''} onClick={() => setTab('unknown')}>
          待补地名
        </button>
        <button className={tab === 'search' ? 'on' : ''} onClick={() => setTab('search')}>
          检索 / 编辑
        </button>
      </div>
      <div className="feed">
        {note && <div className="geodata-note">{note}</div>}

        {tab === 'unknown' && (
          <>
            {unknown.map((u) => (
              <div key={u.id} className="pitem">
                <div className="pitem-head">
                  <span className="place">{u.name}</span>
                  <span className="muted">{u.report_count} 次</span>
                </div>
                <p className="ptext">{u.context}</p>
                <div className="pitem-actions">
                  <button className="dismiss" disabled={busy} onClick={() => startCreate(u.name)}>
                    补录
                  </button>
                  <button
                    className="dismiss"
                    disabled={busy}
                    onClick={async () => {
                      await ignoreUnknownPlace(u.id)
                      refreshUnknown()
                    }}
                  >
                    忽略
                  </button>
                </div>
              </div>
            ))}
            {unknown.length === 0 && (
              <div className="empty">没有待补地名 — 提取到的专名都能在地名库中找到</div>
            )}
          </>
        )}

        {tab === 'search' && (
          <>
            <div className="resolve">
              <input
                placeholder="搜索:中文名 / 乌克兰语原名 / 别名 / 州"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <button className="wide" disabled={busy} onClick={() => startCreate('')}>
                + 新增地名(输入原名后点地图取坐标)
              </button>
            </div>

            <div className="geo-form">
              <label>
                乌克兰语原名
                <input
                  value={form.canonical_name}
                  onChange={(e) => setForm({ ...form, canonical_name: e.target.value })}
                />
              </label>
              <label>
                中文名
                <input
                  value={form.name_zh ?? ''}
                  onChange={(e) => setForm({ ...form, name_zh: e.target.value })}
                />
              </label>
              <label>
                别名(逗号分隔,用于匹配变格)
                <input value={aliasText} onChange={(e) => setAliasText(e.target.value)} />
              </label>
              <label>
                所属州
                <input
                  value={form.admin1 ?? ''}
                  onChange={(e) => setForm({ ...form, admin1: e.target.value })}
                />
              </label>
              <div className="geo-row">
                <label>
                  纬度
                  <input
                    value={Number.isFinite(form.lat) ? String(form.lat) : ''}
                    onChange={(e) => setForm({ ...form, lat: Number(e.target.value) })}
                  />
                </label>
                <label>
                  经度
                  <input
                    value={Number.isFinite(form.lng) ? String(form.lng) : ''}
                    onChange={(e) => setForm({ ...form, lng: Number(e.target.value) })}
                  />
                </label>
              </div>
              <div className="geo-row">
                <label>
                  级别
                  <select
                    value={form.precision}
                    onChange={(e) => setForm({ ...form, precision: e.target.value })}
                  >
                    {PRECISIONS.map(([v, zh]) => (
                      <option key={v} value={v}>
                        {zh}
                      </option>
                    ))}
                  </select>
                </label>
                <button className={picking ? 'pick on' : 'pick'} onClick={onTogglePick}>
                  {picking ? '点击地图取坐标…' : '地图拾取'}
                </button>
              </div>
              <div className="geo-actions">
                <button disabled={busy || !canSave} onClick={save}>
                  {editId == null ? '新增' : '保存修改'}
                </button>
                {editId != null && (
                  <button disabled={busy} onClick={() => startCreate('')}>
                    改为新增
                  </button>
                )}
              </div>
            </div>

            <div className="sec-title">检索结果({places.length})</div>
            {!query.trim() && <div className="empty">输入中文名 / 乌克兰语原名 / 别名开始检索</div>}
            {places.map((p) => (
              <div
                key={p.id}
                className={p.id === editId ? 'item on' : 'item'}
                onClick={() => startEdit(p.id)}
              >
                <span className="place">{p.name_zh ?? p.name}</span>
                <span className="meta">
                  {p.admin1 ?? '—'} · {p.precision}
                  {p.source === 'admin' ? ' · 手工' : ''}
                </span>
              </div>
            ))}
            {query.trim() && places.length === 0 && <div className="empty">没有匹配的地名 — 可点上方「新增地名」补录</div>}
          </>
        )}
      </div>
    </>
  )
}
