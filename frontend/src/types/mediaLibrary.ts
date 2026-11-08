export type MediaCategoryKind = '' | 'tv' | 'anime' | 'variety' | 'movie'

export type OpenListStorage = {
  id: string
  mount_path: string
  driver: string
  drive_type?: string | null
  root_path?: string | null
  status: string
  disabled: boolean
  remark: string
}

export type EmbyLibrary = {
  id: string
  name: string
  collection_type: string
  locations: string[]
}

export type MediaCategoryFields = {
  name: string
  kind: MediaCategoryKind | string
  drive_type?: string | null
  cloud_root: string
  openlist_storage_id?: string | null
  openlist_mount_path?: string | null
  openlist_root_dir?: string | null
  emby_library_id?: string | null
  emby_library_name?: string | null
  sort_order: number
}

export type MediaCategory = MediaCategoryFields & { id: number }

export type MediaCategorySuggestion = MediaCategoryFields & {
  locations: string[]
  openlist_path?: string | null
  matched: boolean
  already_configured: boolean
  note: string
}

export type MediaCategorySuggestResponse = {
  storages: OpenListStorage[]
  libraries: EmbyLibrary[]
  suggestions: MediaCategorySuggestion[]
}
