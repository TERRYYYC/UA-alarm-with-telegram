import { useEffect, useMemo, useRef } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { MapEvent } from '../types'
import { tierOf } from '../util'
import { resolveBasemap, styleFor } from '../basemap'

const TYPE_COLOR_MATCH: unknown[] = [
  'match',
  ['get', 'type'],
  'explosion', '#c96a50',
  'shelling', '#c89340',
  'drone', '#8b6fbc',
  'missile', '#bd576d',
  'air_alert', '#d4c14f',
  '#909ba8',
]

const TIER_OPACITY_MATCH: unknown[] = [
  'match',
  ['get', 'tier'],
  'fresh', 1,
  'recent', 0.78,
  0.44,
]

const TIER_STROKE_MATCH: unknown[] = [
  'match',
  ['get', 'tier'],
  'fresh', '#e5e9ee',
  'rgba(0,0,0,0)',
]

function toFeatureCollection(events: MapEvent[], now: number) {
  return {
    type: 'FeatureCollection' as const,
    features: events
      .filter((e) => e.place && e.geometry && e.geometry.coordinates && e.geometry.coordinates.length >= 2)
      .map((e) => ({
        type: 'Feature' as const,
        geometry: e.geometry,
        properties: {
          id: e.id,
          type: e.event_type,
          place: e.place!.name,
          tier: tierOf(e.occurred_at, now),
          precision: e.place?.precision ?? 'settlement',
          unverified: e.verification === 'unverified',
        },
      })),
  }
}

interface Props {
  events: MapEvent[]
  now: number
  selectedId: number | null
  onSelect: (id: number) => void
  // 地名维护:拾取模式(点击地图取经纬度)
  picking?: boolean
  onPick?: (lng: number, lat: number) => void
  focus?: { lng: number; lat: number; zoom?: number } | null
}

export default function MapView({ events, now, selectedId, onSelect, picking, onPick, focus }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const eventsRef = useRef<MapEvent[]>(events)
  eventsRef.current = events
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect
  const pickRef = useRef<{ picking: boolean; onPick?: (lng: number, lat: number) => void }>({
    picking: false,
  })
  pickRef.current = { picking: Boolean(picking), onPick }
  // DOM 标签标记:id → Marker(支持中文字体)
  const labelsRef = useRef<Map<number, maplibregl.Marker>>(new Map())

  // 最新数据始终放进 ref:解决"事件先于 style load 到达、source 尚未创建"的竞态
  const fc = useMemo(() => toFeatureCollection(events, now), [events, now])
  const fcRef = useRef(fc)
  fcRef.current = fc

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: styleFor(resolveBasemap()),
      center: [31.6, 48.7],
      zoom: 4.9,
      minZoom: 4,
      maxZoom: 11,
      attributionControl: { compact: true },
    })
    mapRef.current = map

    map.on('load', () => {
      map.addSource('events', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })
      map.addLayer({
        id: 'events',
        type: 'circle',
        source: 'events',
        paint: {
          'circle-radius': ['match', ['get', 'precision'], 'region', 14, 'district', 10, 6],
          'circle-color': TYPE_COLOR_MATCH as never,
          'circle-opacity': TIER_OPACITY_MATCH as never,
          'circle-stroke-color': TIER_STROKE_MATCH as never,
          'circle-stroke-width': 1.2,
        },
      })
      // 标签改用 DOM 标记:符号层需要 glyph 服务,不含中文字形
      map.on('click', 'events', (ev) => {
        const f = ev.features?.[0]
        if (f && typeof f.properties?.id === 'number') onSelectRef.current(f.properties.id)
      })
      map.on('click', (ev) => {
        if (pickRef.current.picking && pickRef.current.onPick) {
          pickRef.current.onPick(ev.lngLat.lng, ev.lngLat.lat)
        }
      })
      map.on('mouseenter', 'events', () => {
        if (!pickRef.current.picking) map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', 'events', () => {
        map.getCanvas().style.cursor = ''
      })
      // load 时刻立即补一次数据:防止事件已到达而 load 晚于 effect 的竞态
      const srcNow = map.getSource('events') as maplibregl.GeoJSONSource | undefined
      if (srcNow) srcNow.setData(fcRef.current as never)
    })

    return () => {
      map.remove()
      mapRef.current = null
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const src = map.getSource('events') as maplibregl.GeoJSONSource | undefined
    if (src) src.setData(fc as never)

    // 同步 DOM 标签:重要级别(州/区/城市/镇)始终标注;村镇仅新鲜事件;
    // 总量设上限避免拥挤;若标签过少(数据较旧),补足最新若干条
    const labels = labelsRef.current
    const usable = events.filter(
      (e) => e.place && e.geometry?.coordinates && e.geometry.coordinates.length >= 2,
    )
    const importantTiers = ['region', 'district', 'city', 'town']
    const important = usable.filter((e) => importantTiers.includes(e.place!.precision))
    const freshRest = usable.filter(
      (e) => !importantTiers.includes(e.place!.precision) && tierOf(e.occurred_at, now) !== 'old',
    )
    const chosen = [...important, ...freshRest]
    if (chosen.length < 20) {
      const extra = usable.filter((e) => !chosen.includes(e))
      chosen.push(...extra.slice(0, 20 - chosen.length))
    }
    const labelEvents = chosen.slice(0, 60)

    const keep = new Set<number>()
    for (const e of labelEvents) {
      const tier = tierOf(e.occurred_at, now)
      keep.add(e.id)
      let marker = labels.get(e.id)
      if (!marker) {
        const el = document.createElement('div')
        el.className = 'map-label'
        marker = new maplibregl.Marker({ element: el, anchor: 'top', offset: [0, 10] })
          .setLngLat([e.geometry.coordinates[0], e.geometry.coordinates[1]])
          .addTo(map)
        labels.set(e.id, marker)
      }
      const el = marker.getElement()
      el.textContent = e.place!.name
      el.dataset.tier = tier
      el.dataset.type = e.event_type
      marker.setLngLat([e.geometry.coordinates[0], e.geometry.coordinates[1]])
    }
    for (const [id, marker] of labels) {
      if (!keep.has(id)) {
        marker.remove()
        labels.delete(id)
      }
    }
  }, [events, now, fc])

  useEffect(() => {
    return () => {
      for (const marker of labelsRef.current.values()) marker.remove()
      labelsRef.current.clear()
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (!map || selectedId == null) return
    const ev = eventsRef.current.find((e) => e.id === selectedId)
    if (!ev?.geometry?.coordinates || ev.geometry.coordinates.length < 2) return
    map.flyTo({
      center: [ev.geometry.coordinates[0], ev.geometry.coordinates[1]],
      zoom: Math.max(map.getZoom(), 6.2),
      duration: 600,
    })
  }, [selectedId])

  // 地名维护:拾取模式十字光标 + 拾取点预览标记
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    map.getCanvas().style.cursor = picking ? 'crosshair' : ''
  }, [picking])

  const pickMarkerRef = useRef<maplibregl.Marker | null>(null)
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    if (!focus) return
    if (!pickMarkerRef.current) {
      const el = document.createElement('div')
      el.className = 'pick-marker'
      pickMarkerRef.current = new maplibregl.Marker({ element: el })
    }
    pickMarkerRef.current.setLngLat([focus.lng, focus.lat]).addTo(map)
    map.flyTo({ center: [focus.lng, focus.lat], zoom: Math.max(map.getZoom(), 8), duration: 600 })
  }, [focus])

  return <div ref={containerRef} className="map" />
}
