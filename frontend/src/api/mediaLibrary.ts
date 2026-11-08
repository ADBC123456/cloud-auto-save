import { http } from '@/api/http'
import type {
  EmbyLibrary,
  MediaCategory,
  MediaCategoryFields,
  MediaCategorySuggestResponse,
  OpenListStorage,
} from '@/types/mediaLibrary'

const silent = { headers: { 'X-Silent-Toast': '1' } }

export async function fetchMediaCategories(options?: { silentToast?: boolean }) {
  const { data } = await http.get<MediaCategory[]>('/media-library/categories', options?.silentToast ? silent : undefined)
  return data
}

export async function suggestMediaCategories() {
  const { data } = await http.post<MediaCategorySuggestResponse>('/media-library/categories/suggest', {})
  return data
}

export async function fetchOpenListStorages(options?: { silentToast?: boolean }) {
  const { data } = await http.get<OpenListStorage[]>('/media-library/openlist/storages', options?.silentToast ? silent : undefined)
  return data
}

export async function fetchEmbyLibraries(options?: { silentToast?: boolean }) {
  const { data } = await http.get<EmbyLibrary[]>('/media-library/emby/libraries', options?.silentToast ? silent : undefined)
  return data
}

export async function createMediaCategory(payload: Partial<MediaCategoryFields>) {
  const { data } = await http.post<MediaCategory>('/media-library/categories', payload)
  return data
}

export async function updateMediaCategory(id: number, payload: Partial<MediaCategoryFields>) {
  const { data } = await http.patch<MediaCategory>(`/media-library/categories/${id}`, payload)
  return data
}

export async function deleteMediaCategory(id: number) {
  const { data } = await http.delete<{ ok: boolean }>(`/media-library/categories/${id}`)
  return data
}
