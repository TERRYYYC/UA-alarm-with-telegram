/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BASEMAP?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
