# 更新日志

## v1.2.0（2026-11-08）

### 新增

- **媒体库分类：配好一次 OpenList + Emby，建任务只选分类**
  - 系统设置新增「媒体库分类」页：一键读取 Emby 媒体库（`/emby/Library/VirtualFolders`）和 OpenList 存储（`/api/admin/storage/list`），按媒体库路径自动推算对应的网盘目录；rclone 挂载与 strm 两种方式都支持，识别不出的可从下拉框手选
  - 追剧任务抽屉新增「媒体库分类」下拉框：选分类即自动填保存路径；关联 TMDB 时按电视剧/动漫/综艺/电影（及出品国家）自动选；手动改保存路径会自动对应到所属分类
  - openlist / emby 插件参数折叠为「高级参数」，正常无需填写；任务执行时按分类（老任务按保存路径前缀）自动补齐，只补空字段，手填值优先
  - emby 插件新增任务级 `library_id`：新剧未匹配到时刷新所属分类的媒体库，而不是唯一的全局媒体库
  - openlist 插件不再强制要求全局 `storage_id`，只填地址和 Token 即可启用
  - 数据库：新增 `media_categories` 表（迁移 `20261108_media_categories`，启动时自动执行）
  - 接口：`/api/media-library/openlist/storages`、`/emby/libraries`、`/categories`（增删改查）、`/categories/suggest`

---

## v1.1.0（2026-08-23）

### 新增

- **任务「本周完成」一键休眠**：追剧任务页每行新增「本周完成」按钮
  - 点击后任务立即进入休眠：本周内**全局调度、自动搜索 cron、失效补链巡检**三处触发全部跳过，不再产生任何自动运行
  - 恢复时间按任务自身的运行星期计算：恢复到**最早运行日的下一次出现** 00:00（Asia/Shanghai）
    - 例：周六/日任务，周六或周日任意一天点击 → 都休眠到**下周六** 00:00
    - 只周日更新的任务，周日点击 → 休眠到下周日 00:00
    - 运行星期为「自动（TMDB）」的任务按 TMDB 播出星期计算；未配置运行星期则回退到下周一
  - 状态列显示「休眠至 MM-DD」标签；按钮变为「恢复」，点击可立即解除休眠
  - 休眠期间**手动执行不受限制**（仍可点「执行」强制运行）
  - 数据库：`tasks` 表新增 `paused_until` 字段（迁移 `20260823_tasks_add_paused_until`，启动时自动执行）
  - 接口：`POST /api/tasks/{id}/week-complete`、`POST /api/tasks/{id}/week-resume`（需 TASK_WRITE 权限，写审计日志）

### 修复

- 澄清与「禁用」的区别：禁用是无限期停止（需手动启用）；「本周完成」是定时自动恢复的周期休眠

---

## v1.0.9（上游基线）

- 基于 [cloud-auto-save-x v1.0.9](https://github.com/OzoO0/cloud-auto-save-x)，新增定时自动搜索与链接自动维护能力（详见 README）
