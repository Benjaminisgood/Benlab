# AGENTS.md - Benlab Agent 操作手册

## 1. 目标与定位

本文件给自动化 Agent（如 OpenClaw、Codex、Playwright agent）使用，帮助 Agent 在不依赖口头补充的前提下，快速理解并操作 Benlab。

Benlab 当前不是“实验室限定系统”，它已经扩展为“生活管理中枢”：

- `Item`：你的物品宇宙（拥有、想买、借出、收藏、转让）。
- `Location`：你的足迹与空间地图（学习、休闲、旅行、居住等）。
- `Event`：你的事项与成长轨迹（活动、目标、纪念、计划）。
- `Member`：你的人际关系与协作圈（关注、留言、共同参与）。

## 2. 项目真实结构（以代码为准）

- 后端框架：Flask（单体应用）
- 核心文件：`/Users/ben/Desktop/myapp/Benlab/app.py`（模型 + 路由 + 上传 + 启动迁移）
- 模板目录：`/Users/ben/Desktop/myapp/Benlab/templates`
- 静态目录：`/Users/ben/Desktop/myapp/Benlab/static`
- 启动脚本：`/Users/ben/Desktop/myapp/Benlab/benlab.sh`
- 默认数据库：SQLite（`instance/lab.db`）
- 附件目录：`attachments/`（local 模式）

重要事实（请按当前代码理解）：

- 数据库连接在 `app.py` 里是硬编码：`sqlite:///lab.db?timeout=30`。
- 应用启动时会自动做 schema 纠偏、历史字段迁移、默认管理员初始化（仅空库）。
- 无独立 REST CRUD API；主要通过 HTML 表单路由完成增删改查。

## 3. 快速启动（Agent 必备）

### 3.1 Python 直接启动（开发）

```bash
cd /Users/ben/Desktop/myapp/Benlab
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

默认监听：`0.0.0.0:5000`（可用 `HOST`、`PORT` 覆盖）。

### 3.2 脚本启动（推荐运维）

```bash
cd /Users/ben/Desktop/myapp/Benlab
chmod +x benlab.sh
./benlab.sh start
```

常用命令：

```bash
./benlab.sh stop
./benlab.sh status
./benlab.sh restart
./benlab.sh logs
./benlab.sh backup
./benlab.sh ip
```

## 4. 核心数据模型与枚举约束

### 4.1 核心实体

- `Member`：用户、关注关系、留言、负责资源。
- `Item`：物品主表（多负责人、多位置、多附件）。
- `Location`：空间位置（父子层级、多负责人、多附件、经纬度）。
- `Event`：事项（owner、participants、关联物品/位置、多附件）。
- `Attachment`：统一附件表（`item_id/location_id/event_id` 三选一）。
- `Log`：操作审计日志。
- `Message`：成员留言记录（当前以 feedback 流为主在页面使用）。

### 4.2 关键枚举（写入时必须合法）

- `Item.status`：`正常 / 少量 / 用完 / 借出 / 舍弃`
- `Item.features`：`公共 / 私人`
- `Location.status`：`正常 / 脏 / 报修 / 危险 / 禁止`
- `Event.visibility`：`personal / internal / public`
- 位置用途 tag：`study / leisure / event / public / rental / storage / travel / residence / other`

## 5. 路由地图（高频）

### 5.1 页面路由

- `/login`、`/register`、`/logout`
- `/items`、`/items/add`、`/items/<id>`、`/items/<id>/edit`
- `/locations`、`/locations/add`、`/locations/<id>`、`/locations/<id>/edit`
- `/events`、`/events/add`、`/events/<id>`、`/events/<id>/edit`
- `/members`、`/member/<id>`、`/member/<id>/edit`
- `/export/<datatype>`（`items/members/locations/logs/messages`）

### 5.2 API 路由（少量）

- `/api/items/search`
- `/api/locations/search`
- `/api/forms/ai-autofill`（图片驱动自动填表，支持 item/location）
- `/api/uploads/oss/presign`、`/api/uploads/oss/verify`（OSS 直传）

## 6. 生活管理工作流建议（Agent 执行模板）

### 6.1 每日（轻量）

- 收集今天新增物品并录入 `Item`（状态、归属、数量描述、责任人）。
- 记录去过的新地点到 `Location`（经纬度 + 用途标签）。
- 创建/更新当天 `Event`（活动、待办、纪念、复盘）。

### 6.2 每周（整理）

- 扫描 `Item.status` 的告警项：`用完/舍弃/少量/借出`。
- 扫描 `Location.status` 的告警项：`危险/报修/脏`。
- 生成导出 CSV（`/export/items`、`/export/members`、`/export/locations`、`/export/logs`、`/export/messages`）做归档。

### 6.3 社交协作

- 通过 `Member` 主页沉淀关系、留言和关注。
- 将共同事项配置为 `internal/public`，并维护参与者名单。

## 7. AI 自动填写（可选能力）

接口：`POST /api/forms/ai-autofill`（需登录态）。

输入重点：

- `form_type`: `item` 或 `location`
- 至少一张图片（`attachments`）
- 可选上下文：`context_json`
- 可选已上传引用：`uploaded_refs`

返回：

- `ok`
- `form_type`
- `model`
- `suggestion`（已做字段归一化，可直接回填表单）

模型供应商切换环境变量：

- `AI_AUTOFILL_PROVIDER=chatanywhere|deepseek|aliyun`
- 具体 Key/Base URL 在 `app.py` 的 `_chatanywhere_runtime_config` / `_deepseek_runtime_config` / `_aliyun_runtime_config`

## 8. 存储与附件规则（高风险）

Benlab 支持 `local` / `oss` / `auto` 存储模式。

- local：附件保存在 `attachments/`
- oss：附件对象存储在阿里云 OSS
- auto：检测配置后自动选择

注意事项：

- 不要直接删除 `attachments/` 或 OSS 对象，除非已确认数据库引用关系。
- 系统有“孤儿附件清理线程”，会按引用扫描删除无主文件。
- 若启用 OSS，确保 CORS 允许 `PUT`，否则浏览器直传会降级。

## 9. 安全与运维约束

- 首次空库会创建默认账号 `admin/admin`，上线后必须改密。
- `.env` 可能包含密钥，Agent 不得在日志/回复中输出真实值。
- 任何结构性改动前先备份：`instance/lab.db` + `attachments/`。
- `benlab.sh update` 在强制模式下会执行 `git reset --hard`，谨慎使用。

## 10. 代码修改优先级（给开发型 Agent）

1. 优先定位并修改 `app.py` 对应路由函数（单文件集中实现）。
2. 同步检查关联模板（`templates/*.html`）字段名是否匹配。
3. 涉及上传逻辑时，同步检查：
   - `save_uploaded_media`
   - `remove_uploaded_file`
   - `inject_attachment_helpers`
4. 涉及事项权限时，同步检查：
   - `Event.can_view`
   - `Event.can_edit`
   - `grant_internal_event_access`

最小验证命令：

```bash
python -m compileall app.py
```

## 11. Agent 执行策略（推荐）

- 先读现有数据，再写入（避免重复创建 item/location/event）。
- 优先做“增量更新”，避免覆盖历史内容。
- 写入时遵循枚举与关系约束（尤其多对多负责人、参与者）。
- 对用户可见的变更应附带日志语义（对应 `Log.action_type` 习惯）。

## 12. 给 OpenClaw/自动化工具的建议

- 若目标是“帮用户管理生活”，建议将 Benlab 当作以下四类知识库：
  - 物品账本（Item）
  - 足迹地图（Location）
  - 事项时间线（Event）
  - 社交网络（Member）
- 优先通过页面流程操作；当前项目并未提供完整 JSON CRUD 接口。
- 如果要做长期自动化，可基于“每日录入 + 每周巡检 + 每月导出”三层节奏执行。
