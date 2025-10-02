# Benlab 实验室资产管理系统
> 面向小型实验室与研究团队的轻量级资产全流程协作平台。

## 目录
- [项目简介](#项目简介)
- [功能亮点](#功能亮点)
- [架构与技术栈](#架构与技术栈)
- [快速开始](#快速开始)
- [配置项](#配置项)
- [目录结构](#目录结构)
- [核心模块详解](#核心模块详解)
- [日常运维与数据管理](#日常运维与数据管理)
- [开发调试指南](#开发调试指南)
- [部署建议](#部署建议)
- [常见问题](#常见问题)
- [贡献与反馈](#贡献与反馈)

## 项目简介
Benlab 基于 Flask 打造，帮助实验室管理物品、位置、成员、事项与消息等核心资产信息。系统强调易用性与可视化，提供二维码、批量图片、CSV 导出等配套能力，降低日常管理与审计成本。

## 功能亮点
- 资产全景：集中管理物品库存、存放位置、负责人、采购信息与生命周期记录。
- 位置分层：支持父子位置、清洁状态和负责人标签，页面自动生成二维码便于现场扫码查询。
- 协作成员：成员主页整合负责资产、事项提醒、日志与留言板，降低信息孤岛。
- 事项安排：带有权限控制的日程/事项模块，可关联物品与位置，识别缺失资源并自动提醒。
- 多媒体支持：多图上传、轮播浏览、统一 `/uploads/<filename>` 下载，为移动端拍照盘点提供便利。
- 审计追踪：自动生成资产、位置的增删改日志，必要时可溯源责任人。
- 数据出口：一键导出物品、成员、位置、日志、消息等 CSV 文件，方便离线分析或备份。

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

## 配置项
| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLASK_SECRET_KEY` | `dev-only-change-me` | Flask 会话密钥，生产环境必须替换为强随机值 |
| `HOST` / `FLASK_RUN_HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` / `FLASK_RUN_PORT` / `BENSCI_PORT` | `5001` | 多级端口回退，优先级 `PORT` → `FLASK_RUN_PORT` → `BENSCI_PORT` |
| `SQLALCHEMY_DATABASE_URI` | `sqlite:///lab.db?timeout=30` | 支持改为 PostgreSQL/MySQL 等，例如 `postgresql+psycopg://user:pass@host/db` |
| `UPLOAD_FOLDER` | `./images` | 上传文件保存目录，默认位于项目根目录 |
| `MAX_CONTENT_LENGTH` | `16 * 1024 * 1024` | 上传文件体积上限（16MB） |

> 若使用 `.env` / `.flaskenv` 管理变量，可借助 `python-dotenv` 自动加载。

## 目录结构
```text
Benlab/
├── app.py                # Flask 主应用，包含模型、路由、上传逻辑
├── images/               # 图片上传目录（运行时生成，可挂载持久化存储）
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

### 图片与存储策略
- 上传文件统一保存至 `images/`，可映射至对象存储或专用磁盘。
- `/uploads/<filename>` 动态路由确保端口/域名变更后链接仍可用。
- 编辑表单允许批量删除旧图片，系统会自动清理冗余文件。

## 日常运维与数据管理
- **备份**：定期复制 `lab.db`（或外部数据库备份）及 `images/` 目录。
- **数据清理**：测试环境可删除 `lab.db`、迁移目录后重新执行迁移；生产环境请使用 `flask db downgrade` / `upgrade` 维护版本。
- **库存巡检**：结合二维码巡检，成员扫码即可看到责任人、库存状态与历史记录。
- **导出审计**：导出 CSV 并导入数据仓库或 BI 工具开展年度资产盘点。

## 开发调试指南
- 建议在虚拟环境中运行 `flask shell` 创建演示数据或执行 SQL。
- 语法检查：`python -m compileall app.py`。
- 迁移命令：
  ```bash
  flask db init        # 首次初始化迁移仓库
  flask db migrate -m "描述"
  flask db upgrade
  ```
- 上传调试：确认 `UPLOAD_FOLDER` 可写；必要时在 macOS/Linux 上执行 `chmod 755 images`。
- 若需热加载，可使用 `FLASK_ENV=development flask run`。

## 部署建议
- 使用 Gunicorn + gevent 或 uwsgi 等 WSGI 服务器：
  ```bash
  gunicorn -k gevent -w 4 "app:app"
  ```
- 置于 Nginx/Caddy 反向代理之后，并启用 HTTPS；Flask 已通过 ProxyFix 支持 `X-Forwarded-*` 头。
- 将 `FLASK_SECRET_KEY`、数据库凭据写入安全的环境变量或密钥管理服务。
- 将数据库与 `images/` 目录放置在持久化存储，必要时配置对象存储 / CDN 加速。
- 若部署在容器环境，记得挂载卷保存 `images/` 与数据库数据。

## 常见问题
- **图片无法显示？** 确认 `images/` 目录写权限，以及反向代理是否放行 `/uploads/<filename>` 路由。
- **SQLite 锁冲突？** 已默认启用 WAL、busy_timeout；若并发写入仍频繁，可切换到 PostgreSQL。
- **默认账号安全性？** 部署后务必修改管理员密码，并视情况关闭注册入口。
- **大文件上传失败？** 检查 `MAX_CONTENT_LENGTH` 与反向代理的上传限制（如 Nginx 的 `client_max_body_size`）。

## 贡献与反馈
欢迎根据实验室业务扩展模块，或与扫码枪、智能柜等硬件集成实现自动化管理。如发现问题或完成改进，欢迎提交 Issue / PR，或直接联系维护者交流最佳实践。
