<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'

import { fetchDriveAccounts } from '@/api/extensions'
import {
  createMediaCategory,
  deleteMediaCategory,
  fetchEmbyLibraries,
  fetchMediaCategories,
  fetchOpenListStorages,
  suggestMediaCategories,
  updateMediaCategory,
} from '@/api/mediaLibrary'
import { browseDrive } from '@/api/tasks'
import { useIsMobile } from '@/composables/useIsMobile'
import type { DriveAccountItem } from '@/types/extensions'
import type { EmbyLibrary, MediaCategory, MediaCategoryFields, OpenListStorage } from '@/types/mediaLibrary'
import type { DriveBrowseItem } from '@/types/tasks'

const props = withDefaults(defineProps<{ canWrite?: boolean }>(), { canWrite: false })

type Row = MediaCategoryFields & {
  id: number | null
  uid: string
  note: string
  noteOk: boolean
  saving: boolean
  dirty: boolean
}

const KIND_OPTIONS = [
  { value: 'tv', label: '电视剧' },
  { value: 'anime', label: '动漫' },
  { value: 'variety', label: '综艺' },
  { value: 'movie', label: '电影' },
  { value: '', label: '不自动选择' },
]

const isMobile = useIsMobile()
const loading = ref(false)
const suggesting = ref(false)
const savingAll = ref(false)
const rows = ref<Row[]>([])
const storages = ref<OpenListStorage[]>([])
const libraries = ref<EmbyLibrary[]>([])
const accounts = ref<DriveAccountItem[]>([])
const conn = reactive({ openlistError: '', embyError: '', openlistLoaded: false, embyLoaded: false })

let uidSeq = 0
function toRow(item: Partial<MediaCategory> & MediaCategoryFields, extra?: Partial<Row>): Row {
  uidSeq += 1
  return {
    id: item.id ?? null,
    uid: `row-${uidSeq}`,
    name: item.name || '',
    kind: item.kind || '',
    drive_type: item.drive_type || null,
    cloud_root: item.cloud_root || '',
    openlist_storage_id: item.openlist_storage_id || null,
    openlist_mount_path: item.openlist_mount_path || null,
    openlist_root_dir: item.openlist_root_dir || null,
    emby_library_id: item.emby_library_id || null,
    emby_library_name: item.emby_library_name || null,
    sort_order: Number(item.sort_order || 0),
    note: '',
    noteOk: false,
    saving: false,
    dirty: false,
    ...extra,
  }
}

function errorMessage(e: any, fallback: string) {
  return String(e?.response?.data?.message || e?.message || fallback)
}

async function loadConnections() {
  const [ol, emby] = await Promise.allSettled([
    fetchOpenListStorages({ silentToast: true }),
    fetchEmbyLibraries({ silentToast: true }),
  ])
  if (ol.status === 'fulfilled') {
    storages.value = ol.value
    conn.openlistError = ''
  } else {
    storages.value = []
    conn.openlistError = errorMessage(ol.reason, 'OpenList 连接失败')
  }
  if (emby.status === 'fulfilled') {
    libraries.value = emby.value
    conn.embyError = ''
  } else {
    libraries.value = []
    conn.embyError = errorMessage(emby.reason, 'Emby 连接失败')
  }
  conn.openlistLoaded = true
  conn.embyLoaded = true
}

async function refresh() {
  loading.value = true
  try {
    const [cats] = await Promise.all([
      fetchMediaCategories(),
      loadConnections(),
      fetchDriveAccounts()
        .then((data) => (accounts.value = data || []))
        .catch(() => (accounts.value = [])),
    ])
    rows.value = (cats || []).map((c) => toRow(c))
  } finally {
    loading.value = false
  }
}

async function autoDetect() {
  suggesting.value = true
  try {
    const data = await suggestMediaCategories()
    storages.value = data.storages || []
    libraries.value = data.libraries || []
    conn.openlistError = ''
    conn.embyError = ''
    const known = new Set(rows.value.map((r) => String(r.emby_library_id || '')).filter(Boolean))
    const added = (data.suggestions || [])
      .filter((s) => !s.already_configured && !known.has(String(s.emby_library_id || '')))
      .map((s) => {
        const how = s.openlist_path ? `Emby 路径 ${s.locations[0] || ''} → OpenList ${s.openlist_path}` : `Emby 路径 ${s.locations.join('、') || '（无）'}`
        return toRow(s, { note: `${s.note}；${how}`, noteOk: s.matched, dirty: true })
      })
    rows.value = [...rows.value, ...added]
    if (added.length) {
      ElMessage.success(`识别到 ${added.length} 个新分类，确认无误后点「保存全部」`)
    } else {
      ElMessage.info('没有发现新的 Emby 媒体库，已有分类保持不变')
    }
  } catch {
    await loadConnections()
  } finally {
    suggesting.value = false
  }
}

function addRow() {
  rows.value = [...rows.value, toRow({ name: '', kind: 'tv', cloud_root: '', sort_order: rows.value.length + 1 }, { dirty: true })]
}

function markDirty(row: Row) {
  row.dirty = true
}

function storageLabel(s: OpenListStorage) {
  return `${s.mount_path}（${s.driver || '未知驱动'}${s.disabled ? '，已禁用' : ''}）`
}

function onStorageChange(row: Row, id: string | null) {
  const s = storages.value.find((x) => x.id === id)
  row.openlist_storage_id = s ? s.id : null
  row.openlist_mount_path = s ? s.mount_path : null
  if (s?.root_path) row.openlist_root_dir = s.root_path
  if (s?.drive_type) row.drive_type = s.drive_type
  markDirty(row)
}

function onLibraryChange(row: Row, id: string | null) {
  const lib = libraries.value.find((x) => x.id === id)
  row.emby_library_id = lib ? lib.id : null
  row.emby_library_name = lib ? lib.name : null
  if (lib && !row.name.trim()) row.name = lib.name
  markDirty(row)
}

function payloadOf(row: Row): Partial<MediaCategoryFields> {
  return {
    name: row.name.trim(),
    kind: row.kind || '',
    drive_type: row.drive_type || null,
    cloud_root: row.cloud_root.trim(),
    openlist_storage_id: row.openlist_storage_id || null,
    openlist_mount_path: row.openlist_mount_path || null,
    openlist_root_dir: row.openlist_root_dir || null,
    emby_library_id: row.emby_library_id || null,
    emby_library_name: row.emby_library_name || null,
    sort_order: Number(row.sort_order || 0),
  }
}

function missingOf(row: Row) {
  const missing: string[] = []
  if (!row.name.trim()) missing.push('名称')
  if (!row.cloud_root.trim()) missing.push('网盘保存目录')
  return missing
}

async function saveRow(row: Row, quiet = false) {
  const missing = missingOf(row)
  if (missing.length) {
    if (!quiet) ElMessage.warning(`「${row.name || '未命名'}」请先填写：${missing.join('、')}`)
    return false
  }
  row.saving = true
  try {
    const saved = row.id ? await updateMediaCategory(row.id, payloadOf(row)) : await createMediaCategory(payloadOf(row))
    Object.assign(row, toRow(saved, { uid: row.uid, note: '', dirty: false }))
    if (!quiet) ElMessage.success('已保存')
    return true
  } catch {
    return false
  } finally {
    row.saving = false
  }
}

async function saveAll() {
  const dirty = rows.value.filter((r) => r.dirty)
  if (!dirty.length) {
    ElMessage.info('没有需要保存的修改')
    return
  }
  savingAll.value = true
  try {
    let ok = 0
    const skipped: string[] = []
    for (const row of dirty) {
      if (missingOf(row).length) {
        skipped.push(row.name || '未命名')
        continue
      }
      if (await saveRow(row, true)) ok += 1
    }
    if (skipped.length) {
      ElMessage.warning(`已保存 ${ok} 个；${skipped.join('、')} 还缺网盘保存目录，请点「选择」补上`)
    } else {
      ElMessage.success(`已保存 ${ok} 个分类`)
    }
  } finally {
    savingAll.value = false
  }
}

async function removeRow(row: Row) {
  if (row.id) {
    await ElMessageBox.confirm(`删除分类「${row.name}」？已建好的任务不受影响。`, '删除确认', { type: 'warning' })
    await deleteMediaCategory(row.id)
  }
  rows.value = rows.value.filter((r) => r.uid !== row.uid)
}

const kindLabel = (k: string) => KIND_OPTIONS.find((x) => x.value === k)?.label || '不自动选择'

// ------------------------------------------------------------ 网盘目录选择

const picker = reactive({
  visible: false,
  loading: false,
  row: null as Row | null,
  account: '' as string,
  paths: [] as Array<{ fid: string; name: string }>,
  items: [] as DriveBrowseItem[],
})

const activeAccounts = computed(() => accounts.value.filter((a) => a.enabled && a.runtime_status === 'active'))
const pickerPathLabel = computed(() => `/${picker.paths.map((p) => p.name).join('/')}`.replace(/\/+/g, '/'))
const pickerDirs = computed(() => picker.items.filter((it) => it.is_dir))

async function pickerLoad(dirPath: string) {
  if (!picker.account) return
  picker.loading = true
  try {
    const data = await browseDrive({ dir_path: dirPath, account_name: picker.account, max_items: 500 })
    // fid 模式下后端不返回面包屑，只有按路径打开时才用后端的
    if (Array.isArray(data.paths) && data.paths.length) picker.paths = data.paths
    picker.items = data.exists ? data.items || [] : []
  } finally {
    picker.loading = false
  }
}

function openPicker(row: Row) {
  picker.row = row
  const sameType = activeAccounts.value.filter((a) => !row.drive_type || a.drive_type === row.drive_type)
  const preferred = sameType.find((a) => a.is_default) || sameType[0] || activeAccounts.value[0]
  picker.account = preferred?.name || ''
  picker.paths = []
  picker.items = []
  picker.visible = true
  if (!picker.account) {
    ElMessage.warning('没有可用的网盘账号，请先到「网盘账户」添加并启用')
    return
  }
  pickerLoad(row.cloud_root || '/')
}

function pickerEnter(item: DriveBrowseItem) {
  picker.paths = [...picker.paths, { fid: item.fid, name: String(item.file_name || item.name || '') }]
  pickerLoad(item.fid)
}

function pickerGoto(index: number) {
  if (index < 0) {
    picker.paths = []
    pickerLoad('/')
    return
  }
  picker.paths = picker.paths.slice(0, index + 1)
  pickerLoad(picker.paths[index].fid)
}

function pickerAccountChange() {
  picker.paths = []
  pickerLoad('/')
}

function pickerUse() {
  const row = picker.row
  if (!row) return
  row.cloud_root = pickerPathLabel.value
  const account = accounts.value.find((a) => a.name === picker.account)
  if (account && !row.drive_type) row.drive_type = account.drive_type
  if (!row.name.trim()) row.name = picker.paths.at(-1)?.name || ''
  markDirty(row)
  picker.visible = false
}

onMounted(refresh)
</script>

<template>
  <div v-loading="loading" class="ml">
    <div class="ml__hint">
      <div>配好一次，以后建追剧任务只需从下拉框选分类：保存路径、OpenList 刷新参数、Emby 媒体库都会自动填好。</div>
      <div>点「一键识别」会读取 Emby 的媒体库和 OpenList 的存储，自动推算每个媒体库对应的网盘目录（rclone 挂载和 strm 两种方式都支持），确认后保存即可。</div>
    </div>

    <el-card class="ml__card" shadow="never">
      <template #header>
        <div class="ml__header">
          <div>连接状态</div>
          <el-button text :loading="loading" @click="refresh">刷新</el-button>
        </div>
      </template>
      <div class="ml__status">
        <div class="ml__status-row">
          <el-tag :type="conn.openlistError ? 'danger' : 'success'" effect="plain">OpenList</el-tag>
          <span v-if="conn.openlistError" class="ml__err">{{ conn.openlistError }}</span>
          <span v-else-if="conn.openlistLoaded">已连接，读取到 {{ storages.length }} 个存储</span>
        </div>
        <div class="ml__status-row">
          <el-tag :type="conn.embyError ? 'danger' : 'success'" effect="plain">Emby</el-tag>
          <span v-if="conn.embyError" class="ml__err">{{ conn.embyError }}</span>
          <span v-else-if="conn.embyLoaded">已连接，读取到 {{ libraries.length }} 个媒体库</span>
        </div>
        <div v-if="conn.openlistError || conn.embyError" class="ml__sub">
          连接信息在 <router-link to="/extensions/plugins">插件管理</router-link> 的 openlist / emby 插件里填写（OpenList 需管理员 Token）。连不上也可以手动新增分类。
        </div>
      </div>
    </el-card>

    <el-card class="ml__card" shadow="never">
      <template #header>
        <div class="ml__header">
          <div>媒体库分类（{{ rows.length }}）</div>
          <div class="ml__actions">
            <el-button type="primary" :loading="suggesting" :disabled="!props.canWrite" @click="autoDetect">一键识别</el-button>
            <el-button :disabled="!props.canWrite" @click="addRow">手动新增</el-button>
            <el-button type="success" :loading="savingAll" :disabled="!props.canWrite" @click="saveAll">保存全部</el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="!rows.length" description="还没有分类，点「一键识别」从 Emby 自动生成" />

      <div v-for="row in rows" :key="row.uid" class="ml__row" :class="{ 'ml__row--dirty': row.dirty }">
        <div class="ml__row-head">
          <div class="ml__row-title">
            <span>{{ row.name || '未命名分类' }}</span>
            <el-tag size="small" effect="plain">{{ kindLabel(String(row.kind || '')) }}</el-tag>
            <el-tag v-if="!row.id" size="small" type="warning" effect="plain">未保存</el-tag>
            <el-tag v-else-if="row.dirty" size="small" type="warning" effect="plain">有修改</el-tag>
          </div>
          <div class="ml__actions">
            <el-button size="small" type="primary" :loading="row.saving" :disabled="!props.canWrite" @click="saveRow(row)">保存</el-button>
            <el-button size="small" type="danger" plain :disabled="!props.canWrite" @click="removeRow(row)">{{ row.id ? '删除' : '移除' }}</el-button>
          </div>
        </div>
        <div v-if="row.note" class="ml__note" :class="{ 'ml__note--ok': row.noteOk }">{{ row.note }}</div>

        <el-form label-position="top" class="ml__grid" :disabled="!props.canWrite">
          <el-form-item label="分类名称">
            <el-input v-model="row.name" placeholder="如：国产剧" @input="markDirty(row)" />
          </el-form-item>
          <el-form-item label="按 TMDB 自动选择">
            <el-select v-model="row.kind" style="width: 100%" @change="markDirty(row)">
              <el-option v-for="opt in KIND_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="Emby 媒体库">
            <el-select
              :model-value="row.emby_library_id || ''"
              filterable
              clearable
              style="width: 100%"
              :placeholder="libraries.length ? '选择媒体库' : (row.emby_library_name || '未连接 Emby')"
              @update:model-value="(v: any) => onLibraryChange(row, v || null)"
            >
              <el-option v-if="row.emby_library_id && !libraries.some((l) => l.id === row.emby_library_id)" :label="row.emby_library_name || row.emby_library_id" :value="row.emby_library_id" />
              <el-option v-for="lib in libraries" :key="lib.id" :label="lib.name" :value="lib.id">
                <span>{{ lib.name }}</span>
                <span class="ml__opt-sub">{{ lib.locations.join('、') }}</span>
              </el-option>
            </el-select>
          </el-form-item>
          <el-form-item label="OpenList 存储">
            <el-select
              :model-value="row.openlist_storage_id || ''"
              filterable
              clearable
              style="width: 100%"
              :placeholder="storages.length ? '选择存储' : (row.openlist_mount_path || '未连接 OpenList')"
              @update:model-value="(v: any) => onStorageChange(row, v || null)"
            >
              <el-option v-if="row.openlist_storage_id && !storages.some((s) => s.id === row.openlist_storage_id)" :label="row.openlist_mount_path || row.openlist_storage_id" :value="row.openlist_storage_id" />
              <el-option v-for="s in storages" :key="s.id" :label="storageLabel(s)" :value="s.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="网盘保存目录（任务会存到这里的子文件夹）">
            <el-input v-model="row.cloud_root" placeholder="/TV/国产剧" @input="markDirty(row)">
              <template #append>
                <el-button @click="openPicker(row)">选择</el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="OpenList 存储根目录对应的网盘目录">
            <el-input v-model="row.openlist_root_dir" placeholder="一般是 /" @input="markDirty(row)" />
            <div class="ml__sub">选了存储会自动填；只有存储的根目录不是网盘根目录时才需要改。</div>
          </el-form-item>
        </el-form>
      </div>
    </el-card>

    <el-dialog v-model="picker.visible" title="选择网盘保存目录" :width="isMobile ? '96vw' : '640px'">
      <div class="ml__picker-bar">
        <el-select v-model="picker.account" style="min-width: 200px" placeholder="选择网盘账号" @change="pickerAccountChange">
          <el-option v-for="a in activeAccounts" :key="a.id" :label="`${a.name}（${a.drive_type}）`" :value="a.name" />
        </el-select>
      </div>
      <el-breadcrumb separator="/" class="ml__crumbs">
        <el-breadcrumb-item><el-link @click="pickerGoto(-1)">根目录</el-link></el-breadcrumb-item>
        <el-breadcrumb-item v-for="(p, i) in picker.paths" :key="p.fid">
          <el-link @click="pickerGoto(i)">{{ p.name }}</el-link>
        </el-breadcrumb-item>
      </el-breadcrumb>
      <div v-loading="picker.loading" class="ml__picker-list">
        <el-empty v-if="!pickerDirs.length && !picker.loading" description="没有子文件夹" :image-size="60" />
        <div v-for="it in pickerDirs" :key="it.fid" class="ml__picker-item" @click="pickerEnter(it)">
          📁 {{ it.file_name || it.name }}
        </div>
      </div>
      <template #footer>
        <span class="ml__sub" style="margin-right: 12px">当前：{{ pickerPathLabel }}</span>
        <el-button @click="picker.visible = false">取消</el-button>
        <el-button type="primary" @click="pickerUse">使用当前目录</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ml__hint {
  margin: 10px 0 14px;
  color: var(--el-text-color-regular);
  font-size: 13px;
  line-height: 1.7;
}
.ml__card {
  margin-bottom: 14px;
}
.ml__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.ml__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.ml__status {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
}
.ml__status-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ml__err {
  color: var(--el-color-danger);
}
.ml__sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.ml__row {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.ml__row--dirty {
  border-color: var(--el-color-warning-light-5);
}
.ml__row-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.ml__row-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
.ml__note {
  font-size: 12px;
  color: var(--el-color-warning-dark-2);
  margin-bottom: 8px;
  word-break: break-all;
}
.ml__note--ok {
  color: var(--el-color-success);
}
.ml__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 16px;
}
.ml__grid :deep(.el-form-item) {
  margin-bottom: 12px;
}
.ml__opt-sub {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.ml__picker-bar {
  margin-bottom: 10px;
}
.ml__crumbs {
  margin-bottom: 10px;
}
.ml__picker-list {
  min-height: 200px;
  max-height: 50vh;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.ml__picker-item {
  padding: 8px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--el-border-color-extra-light);
}
.ml__picker-item:hover {
  background: var(--el-fill-color-light);
}
@media (max-width: 767px) {
  .ml__grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
