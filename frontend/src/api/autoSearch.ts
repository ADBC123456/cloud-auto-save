import { http } from '@/api/http'

export type AutoSearchRunResult = {
  ok: boolean
  total: number
  found: number
  items: Array<{
    task_id: number
    taskname: string
    result: string
    message?: string
    shareurl?: string
    datetime?: string
  }>
}

export async function runAutoSearch(taskId: number | null, force = true) {
  const payload: { task_id: number | null; force: boolean } = { task_id: taskId, force }
  const { data } = await http.post<AutoSearchRunResult>('/auto-search/run', payload, { timeout: 300000 })
  return data
}
