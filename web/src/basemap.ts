import type { StyleSpecification } from 'maplibre-gl'

// 底图配置:默认使用无需 API Key 的深色底图(CARTO 公共瓦片自 2026 年起带水印)。
// 可用 ?basemap=carto-dark 覆盖,或用 VITE_BASEMAP 环境变量固定部署默认值。
export interface BasemapPreset {
  id: string
  label: string
  kind: 'raster' | 'vector' | 'none'
  tiles?: string[]
  tileSize?: number
  styleUrl?: string
  attribution: string
  backgroundColor: string
}

export const BASEMAPS: Record<string, BasemapPreset> = {
  // Esri World Dark Gray Canvas:无需 Key、深色、街道级到国界级都有
  'esri-dark': {
    id: 'esri-dark',
    label: 'Esri 深灰',
    kind: 'raster',
    tiles: [
      'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    ],
    tileSize: 256,
    attribution:
      'Tiles © <a href="https://www.esri.com/">Esri</a> — Esri, DeLorme, HERE, © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    backgroundColor: '#0d1014',
  },
  // CARTO dark:视觉最贴合,但公共端点会打 "API KEY REQUIRED" 水印
  'carto-dark': {
    id: 'carto-dark',
    label: 'CARTO 深色(需 Key)',
    kind: 'raster',
    tiles: ['https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png'],
    tileSize: 256,
    attribution:
      '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions">CARTO</a>',
    backgroundColor: '#0d1014',
  },
  // 离线/内网部署:纯深色背景,只画事件
  none: {
    id: 'none',
    label: '无底图',
    kind: 'none',
    attribution: '',
    backgroundColor: '#0d1014',
  },
}

export function resolveBasemap(): BasemapPreset {
  const fromUrl = new URLSearchParams(window.location.search).get('basemap')
  const fromEnv = import.meta.env.VITE_BASEMAP as string | undefined
  const id = fromUrl || fromEnv || 'esri-dark'
  return BASEMAPS[id] ?? BASEMAPS['esri-dark']
}

export function styleFor(preset: BasemapPreset): StyleSpecification {
  const base: StyleSpecification = {
    version: 8,
    sources: {},
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': preset.backgroundColor } },
    ],
  }
  if (preset.kind === 'raster' && preset.tiles) {
    base.sources.carto = {
      type: 'raster',
      tiles: preset.tiles,
      tileSize: preset.tileSize ?? 256,
      attribution: preset.attribution,
    }
    base.layers.push({ id: 'carto', type: 'raster', source: 'carto' })
  }
  return base
}
