# Benlab 实验室资产管理系统
> 面向小型实验室与研究团队的轻量级资产全流程协作平台。支持物品、位置、成员、事项、消息的统一管理，覆盖盘点、维护、协作与审计等完整流程。

## 目录
- [项目简介](#项目简介)
- [适用场景](#适用场景)
- [功能亮点](#功能亮点)
- [架构与技术栈](#架构与技术栈)
- [快速开始](#快速开始)
- [一键启动（benlab.sh 脚本）](#一键启动benlabsh-脚本)
- [配置项](#配置项)
- [目录结构](#目录结构)
- [核心模块详解](#核心模块详解)
- [典型使用流程](#典型使用流程)
- [权限与角色建议](#权限与角色建议)
- [数据模型概览](#数据模型概览)
- [图片与存储策略](#图片与存储策略)
- [日常运维与数据管理](#日常运维与数据管理)
- [开发调试指南](#开发调试指南)
- [部署建议](#部署建议)
- [常见问题](#常见问题)
- [路线图（Roadmap）](#路线图roadmap)
- [贡献与反馈](#贡献与反馈)

## 项目简介
Benlab 基于 Flask 打造，帮助实验室管理物品、位置、成员、事项与消息等核心资产信息。系统强调易用性与可视化，提供二维码、批量图片、CSV 导出等配套能力，降低日常管理与审计成本。

**系统特色**
- 低门槛：开箱即用，默认 SQLite 即可启动。
- 可追溯：物品/位置的增删改自动写入日志。
- 可协作：事项与消息协作结合，形成团队资产记忆。

## 适用场景
- 高校/企业实验室：试剂、耗材、设备与实验区域管理。
- 研究小组：共享设备预约、事项协作与人员分工。
- 创客/工作室：物料清单、工具位置、使用记录。
- 需要追踪资产位置、负责人、历史变更的任何轻量资产场景。

## 功能亮点
- **资产全景**：集中管理物品库存、存放位置、负责人、采购信息与生命周期记录。
- **位置分层**：支持父子位置、清洁状态和负责人标签，页面自动生成二维码便于现场扫码查询。
- **协作成员**：成员主页整合负责资产、事项提醒、日志与留言板，降低信息孤岛。
- **事项安排**：带有权限控制的日程/事项模块，可关联物品与位置，识别缺失资源并自动提醒。
- **多媒体支持**：多图上传、轮播浏览、统一 `/uploads/<filename>` 下载，为移动端拍照盘点提供便利。
- **审计追踪**：自动生成资产、位置的增删改日志，必要时可溯源责任人。
- **数据出口**：一键导出物品、成员、位置、日志、消息等 CSV 文件，方便离线分析或备份。
- **部署友好**：支持一键脚本启动、Gunicorn 与反向代理部署。

## 架构与技术栈
- **后端框架**：Flask + Flask-Login + Flask-SQLAlchemy + Flask-Migrate
- **数据库**：默认 SQLite，支持切换 PostgreSQL/MySQL 等生产级数据库
- **前端**：Bootstrap 5、自适应布局
- **部署参考**：Gunicorn + gevent，Nginx/Caddy 反向代理
- **其他依赖**：Werkzeug、pandas（用于导出与处理数据）

系统主体由 `app.py` 承载，整合模型、路由、文件上传、导出以及权限控制；模板位于 `templates/`，负责渲染管理后台界面。

## 快速开始
### 1. 克隆仓库并创建虚拟环境
```bash
git clone <your-repo-url>
cd Benlab
python3 -m venv venv
source venv/bin/activate  # Windows PowerShell: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 配置基础环境变量（首次建议写入 `.env` 或 shell）
```bash
export FLASK_APP=app.py
export FLASK_SECRET_KEY='请替换为随机字符串'
# 如需调整监听地址或端口，可添加：
# export HOST=0.0.0.0
# export PORT=5001
```

### 3. 启动服务
```bash
python app.py
# 或使用 Flask CLI：
# flask run --host="${HOST:-0.0.0.0}" --port="${PORT:-5001}"
```

首次启动会自动创建管理员账号 `admin/admin`，请第一时间登录后台修改密码并补充个人资料。

## 一键启动（benlab.sh 脚本）
项目根目录自带 `benlab.sh`，封装了虚拟环境、依赖检测、目录初始化、Gunicorn 启动等步骤。

1) 赋予执行权限（仅首次）：
```bash
chmod +x benlab.sh
```

2) 可在 `.env` 写好 `FLASK_SECRET_KEY`、`PORT` 等变量，脚本会自动加载。

3) 一键启动：
```bash
./benlab.sh start
```
- 会自动创建/复用 `venv`、安装 `requirements.txt`、确保 `attachments/` 和 `instance/` 目录存在、计算合理的 Gunicorn workers，并默认监听 `0.0.0.0:5001`。

4) 其他常用命令：
```bash
./benlab.sh stop    # 停止服务
./benlab.sh status  # 查看运行状态
./benlab.sh logs    # 持续输出访问/错误日志
./benlab.sh ip      # 快速查看访问入口
```

## 配置项
| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLASK_SECRET_KEY` | `dev-only-change-me` | Flask 会话密钥，生产环境必须替换为强随机值 |
| `HOST` / `FLASK_RUN_HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` / `FLASK_RUN_PORT` / `BENSCI_PORT` | `5001` | 多级端口回退，优先级 `PORT` → `FLASK_RUN_PORT` → `BENSCI_PORT` |
| `SQLALCHEMY_DATABASE_URI` | `sqlite:///lab.db?timeout=30` | 支持改为 PostgreSQL/MySQL 等，例如 `postgresql+psycopg://user:pass@host/db` |
| `ATTACHMENTS_FOLDER` | `./attachments` | 上传文件保存目录，默认位于项目根目录 |
| `PUBLIC_BASE_URL` | `''` | 事项分享/二维码使用的外部访问域名（不影响 OSS） |
| `ALIYUN_OSS_PUBLIC_BASE_URL` | `''` | OSS 绑定域名/CNAME（可选） |
| `ALIYUN_OSS_ASSUME_PUBLIC` | `false` | 是否假定 OSS Bucket 公共可读；为 `false` 时生成签名 URL 并使用默认域名 |
| `MAX_CONTENT_LENGTH` | `2500 * 1024 * 1024` | 上传文件体积上限（2500MB） |

> 若使用 `.env` / `.flaskenv` 管理变量，可借助 `python-dotenv` 自动加载。

> 使用提供的 `benlab.sh start` 管理脚本时，会自动在项目根目录加载 `.env` 并导出环境变量，无需手动 `export`。

## 目录结构
```text
Benlab/
├── app.py                # Flask 主应用，包含模型、路由、上传逻辑
├── attachments/          # 附件上传目录（运行时生成，可挂载持久化存储）
├── instance/             # Flask 实例目录（可放置生产配置）
├── migrations/           # Flask-Migrate 管理的数据库迁移版本
├── requirements.txt      # Python 依赖列表
├── templates/            # Jinja2 模板（界面与表单）
│   ├── base.html
│   ├── edit_profile.html
│   ├── event_detail.html
│   ├── event_form.html
│   ├── item_detail.html
│   ├── item_form.html
│   ├── items.html
│   ├── location.html
│   ├── location_form.html
│   ├── locations.html
│   ├── login.html
│   ├── members.html
│   ├── profile.html
│   └── register.html
└── readme.md             # 项目说明文档
```

## 核心模块详解
### 物品管理
- 支持按名称、备注、CAS 号搜索与分类筛选。
- 维护库存状态（充足 / 少量 / 用完 / 舍弃）、特性标签、购入日期、数量单位与采购链接。
- 可指定负责人并关联多个存放位置；详情页提供上一张/下一张图片轮播及二维码跳转。

### 位置管理
- 多级父子结构，快速浏览子区域并追踪清洁状态。
- 支持多负责人、备注、详情链接与多图上传；二维码可贴在物理位置供扫码查看。

### 成员中心
- 成员主页展示负责的物品和位置，并突出库存告警或卫生问题。
- 内置站内信、协作通知、操作日志；支持修改头像与个人资料。

### 事项与协作
- 事项（Event）模块支持个人 / 内部 / 公开三种可见性。
- 可关联物品、位置与参与成员，系统会提示描述中提及但未关联的资源。
- 上传事项配图、识别缺失资源、支持参与者申请与确认。

### 日志与消息
- 资产、位置的新增/修改/删除自动写入日志，便于追溯。
- 成员间可通过留言板沟通，保留时间戳记录。

### 数据导出
- 导出覆盖 `items`、`members`、`locations`、`logs`、`messages` 等表。
- 借助 pandas 生成 CSV，可直接导入 Excel/数据分析工具。

## 典型使用流程
1. **初始化基础数据**：创建楼层/房间/货架等位置层级，并补全负责人信息。
2. **导入或录入物品**：录入关键属性，上传实物图片，指定存放位置与负责人。
3. **生成二维码并贴附**：在位置与物品详情页下载二维码，贴在柜门/箱体等显眼位置。
4. **日常维护**：通过扫码快速查看详情、更新库存状态与清洁状态。
5. **安排事项与提醒**：创建实验、维护、采购等事项，关联物品与位置并通知成员。
6. **导出盘点**：按周期导出 CSV 备份，或接入数据分析工具做盘点报告。

## 权限与角色建议
目前系统以管理员与普通成员两类权限为主，可结合组织结构制定轻量规则：
- **管理员**：全量管理权限，负责成员管理、位置结构调整、权限审核。
- **成员**：管理被分配的物品与位置，可参与事项协作、发表评论。
- **外部协作者（建议）**：在反向代理层做访问控制，仅放开查看/扫码入口。

如需更精细化角色（如仓管/审计/访客），可在 `app.py` 的权限判断逻辑基础上扩展。

## 数据模型概览
> 便于了解关系，便于后续二次开发或 BI 对接。

- **Item（物品）**：核心资产对象，关联 `Location`、`Member` 与多张图片。
- **Location（位置）**：支持父子层级关系，可关联多个 `Item` 与负责人。
- **Member（成员）**：系统用户，关联负责的物品、位置与参与事项。
- **Event（事项）**：协作任务与日程，支持参与者、关联资源与状态变更。
- **Log / Message**：系统审计与成员互动记录。

如需查看或扩展字段，请查阅 `app.py` 中模型定义区域。

## 附件与存储策略
- 默认使用 OSS 存储；`attachments/` 用于本地落盘或同步缓存。
- `/attachments/<filename>` 动态路由可访问本地附件，方便迁移或排查。
- 编辑表单允许批量删除旧附件，系统会自动清理冗余文件。
- **OSS 直传**：启用 OSS 时默认使用前端直传，无需额外开关。
  - `ALIYUN_OSS_PUBLIC_BASE_URL` 可配置绑定域名/CNAME；当 `ALIYUN_OSS_ASSUME_PUBLIC=1` 时会用作对外访问域名。
  - `ALIYUN_OSS_ASSUME_PUBLIC=0`（默认）会使用默认 bucket 域名并生成签名 URL，不依赖公共域名。
  - 若需要 HTTP，可显式写成 `http://...`。

**OSS 配置示例**
```bash
export ALIYUN_OSS_BUCKET=example-bucket
export ALIYUN_OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
export ALIYUN_OSS_ACCESS_KEY_ID=xxx
export ALIYUN_OSS_ACCESS_KEY_SECRET=yyy
export ALIYUN_OSS_PUBLIC_BASE_URL=https://oss.example.com
export ALIYUN_OSS_ASSUME_PUBLIC=0
```

## 日常运维与数据管理
- **备份**：定期复制 `lab.db`（或外部数据库备份）及 `attachments/` 目录。
- **数据清理**：测试环境可删除 `lab.db`、迁移目录后重新执行迁移；生产环境请使用 `flask db downgrade` / `upgrade` 维护版本。
- **库存巡检**：结合二维码巡检，成员扫码即可看到责任人、库存状态与历史记录。
- **导出审计**：导出 CSV 并导入数据仓库或 BI 工具开展年度资产盘点。
- **日志留存**：建议保留日志与消息记录以满足审计需求。

## 开发调试指南
- 建议在虚拟环境中运行 `flask shell` 创建演示数据或执行 SQL。
- 语法检查：`python -m compileall app.py`。
- 迁移命令：
  ```bash
  flask db init        # 首次初始化迁移仓库
  flask db migrate -m "描述"
  flask db upgrade
  ```
- 上传调试：确认 `ATTACHMENTS_FOLDER` 可写；必要时在 macOS/Linux 上执行 `chmod 755 attachments`。
- 若需热加载，可使用 `FLASK_ENV=development flask run`。

## 部署建议
- 使用 Gunicorn + gevent 或 uwsgi 等 WSGI 服务器：
  ```bash
  gunicorn -k gevent -w 4 "app:app"
  ```
- 置于 Nginx/Caddy 反向代理之后，并启用 HTTPS；Flask 已通过 ProxyFix 支持 `X-Forwarded-*` 头。
- 将 `FLASK_SECRET_KEY`、数据库凭据写入安全的环境变量或密钥管理服务。
- 将数据库与 `attachments/` 目录放置在持久化存储，必要时配置对象存储 / CDN 加速。
- 若部署在容器环境，记得挂载卷保存 `attachments/` 与数据库数据。

**生产环境检查清单**
- [ ] 修改默认管理员账号密码。
- [ ] 设置强随机 `FLASK_SECRET_KEY`。
- [ ] 配置反向代理与 HTTPS。
- [ ] 开启数据库备份与日志留存策略。
- [ ] 如使用 OSS，确保 Bucket 权限与签名配置正确（私有桶需签名访问）。

## 常见问题
- **附件无法显示？** 确认 `attachments/` 目录写权限，以及反向代理是否放行 `/attachments/<filename>` 路由。
- **SQLite 锁冲突？** 已默认启用 WAL、busy_timeout；若并发写入仍频繁，可切换到 PostgreSQL。
- **默认账号安全性？** 部署后务必修改管理员密码，并视情况关闭注册入口。
- **大文件上传失败？** 检查 `MAX_CONTENT_LENGTH` 与反向代理的上传限制（如 Nginx 的 `client_max_body_size`）。
- **二维码无法访问？** 确认服务域名/端口正确，反向代理未拦截静态与 `/attachments/` 路由。

## 路线图（Roadmap）
- [ ] 支持批量导入（CSV/Excel）与模板校验
- [ ] 更细粒度权限（仓管/审计/访客）
- [ ] 事项提醒接入企业微信/邮件通知
- [ ] 资产报废与维修流程闭环
- [ ] 多语言 UI 与移动端友好布局优化

## 以上是这个网站的初衷，但接下来，才是精彩
- 物品：作为人生的物品记录本，所有用过的，推荐的，想要的，想卖的，一切物品，把这里当成你的专属物品宇宙，从生活小物到珍藏收藏、想买清单到二手转让，每件东西都能被分类记录、打标签、设置提醒，随时搜索回顾。
- 地点：管理物品？大胆点，还可以管理自己，每去过一个地方，可以在这里留下你的足迹，记录下坐标，存储下美景。不仅是足迹地图，更能记录旅行攻略、日常散步路线、朋友推荐的私藏角落，搭配照片、音频、甚至实时导航，让回忆与灵感随时被唤起。
- 事项：社区活动？表白墙？也是你的人生大事留恋册，记录下成长过程达到的每一个成就，每一次宝贵的经历，绘制你的专属宇宙。打造个人仪式感与社区活动中心，日程规划、目标打卡、学习成长轨迹、纪念日提醒，甚至是圈子联动的主题挑战，都可以在这里发生并留档。
- 成员：当然，可以作为自己的“老友记”让你的朋友在你的世界里记录、发光、留言，人与人会路过，但痕迹可以永存！1+1>2！邀请伙伴协作，共建“老友记”空间；家人共管的家庭记账簿、团队的项目进度板、兴趣小组的灵感墙，都能通过权限设置、互动留言、实时通知保持连接，让每段关系不被时间冲淡。

## 贡献与反馈
欢迎根据实验室业务扩展模块，或与扫码枪、智能柜等硬件集成实现自动化管理。如发现问题或完成改进，欢迎提交 Issue / PR，或直接联系维护者交流最佳实践。
