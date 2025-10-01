# Benlab 实验室资产管理系统

## 项目简介
Benlab 是一套面向小型实验室和研究小组的资产管理系统。它基于 Flask 构建，提供物品、位置、成员三大模块的可视化管理，并内置库存状态提醒、二维码快速访问、多图上传等能力，帮助团队掌握实验资源、保持空间井然有序。

## 核心功能
- 登录认证：支持注册、登录、退出，默认提供管理员账号，配合 Flask-Login 管理会话。
- 物品管理：记录 CAS 号、库存状态、数量单位、购买信息，可分配负责人、关联多个存放位置，并支持多张图片上传与轮播浏览。
- 位置管理：支持上下级结构、卫生状态、备注及多位负责人；页面自动生成二维码，方便手机扫码查看。
- 成员与消息：成员主页可查看负责的物品与位置、近期操作日志、协作通知，并支持站内留言板留言。
- 数据导出：一键导出物品、成员、位置、日志、消息等数据为 CSV 文件，方便离线分析或备份。
- 图片处理：所有上传图片保存在 `images/` 目录，通过 `/uploads/<filename>` 动态访问，端口或域名变更后依旧有效。
- 日志追踪：物品和位置的新增、修改、删除会自动记录操作日志，以便审计和对账。

## 技术栈
- Python 3
- Flask、Flask-Login、Flask-SQLAlchemy、Flask-Migrate
- SQLite（默认，可替换为 PostgreSQL 等生产级数据库）
- Bootstrap 5 前端样式

## 快速上手
### 环境准备
- Python 3.10 及以上
- Git（可选）

### 安装依赖
```bash
git clone <your-repo-url>
cd Benlab
python3 -m venv venv
source venv/bin/activate  # Windows 使用: venv\Scripts\activate
pip install -r requirements.txt
```

### 首次启动
```bash
export FLASK_APP=app.py
export FLASK_SECRET_KEY='请替换为随机字符串'
python app.py
```

默认监听 `0.0.0.0:5001`，可通过下表中的环境变量进行调整。

#### 关键环境变量
| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLASK_SECRET_KEY` | `dev-only-change-me` | Flask 会话密钥，生产环境务必改为强随机值 |
| `HOST` | `0.0.0.0` | 启动地址；同时支持 `FLASK_RUN_HOST` 等常见变量 |
| `PORT` | `5001` | 端口优先级：`PORT` → `FLASK_RUN_PORT` → `BENSCI_PORT` → `5001` |
| `SQLALCHEMY_DATABASE_URI` | `sqlite:///lab.db?timeout=30` | 默认 SQLite，生产可改为 PostgreSQL |

### 默认账号
首次运行会自动创建管理员账号：`admin` / `admin`。请尽快登录后修改密码并补充资料。

## 目录结构
```text
Benlab/
├── app.py                # Flask 主应用，包含模型、路由、上传逻辑
├── images/               # 图片上传目录（运行时生成）
├── instance/             # Flask 默认实例目录（可放配置）
├── requirements.txt      # Python 依赖
├── templates/            # Jinja2 模板
│   ├── base.html
│   ├── edit_profile.html
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

## 功能详解
### 物品管理
- 支持按名称、备注、CAS 号搜索与分类筛选。
- 可设置库存状态（充足 / 少量 / 用完 / 舍弃）和特性标签。
- 关联多个位置、负责人，记录数量、单位、价值、购入日期和购买链接。
- 多图上传：表单支持一次选择多张图片；详情页提供上一张 / 下一张浏览按钮，二维码与共享链接可快速分享。

### 位置管理
- 支持父子层级关系，快速浏览子区域。
- 为位置指定清洁状态、备注、详情链接和多位负责人。
- 支持多图上传与轮播展示，页面自动生成二维码方便现场扫码查看。

### 成员中心
- 成员主页展示所负责的物品与位置，自动突出库存告警和卫生问题。
- 内置操作日志、协作通知和留言板，方便团队沟通。
- 用户可自行修改个人资料、头像（头像同样存储于 `images/` 目录）。

### 数据导出
通过「导出」功能可生成 CSV 文件，覆盖以下实体：`items`、`members`、`locations`、`logs`、`messages`，便于统计或归档。

## 图片与存储策略
- 上传文件统一保存至 `images/` 目录，可通过环境变量或部署策略映射到持久化存储。
- 新增的 `/uploads/<filename>` 路由在运行时动态生成正确的访问链接，即使服务端端口或域名变化也能保持可访问。
- 支持在编辑物品/位置时批量删除旧图片，系统会自动清理磁盘上的冗余文件。

## 数据库迁移
项目默认使用 SQLite 并在启动时 `db.create_all()`，也内置 Flask-Migrate，方便后期维护：
```bash
flask db init        # 首次初始化迁移仓库
flask db migrate -m "描述"
flask db upgrade
```
生产环境建议切换到 PostgreSQL 或 MySQL，并在部署时运行迁移命令保持结构同步。

## 部署建议
- 配置反向代理（Nginx / Caddy 等）并启用 HTTPS，Flask 内置的 ProxyFix 已启用 X-Forwarded-* 头支持。
- 替换 `FLASK_SECRET_KEY`，并将数据库、图片目录放在持久化存储上。
- 使用 Gunicorn + gevent 或其他 WSGI 服务器运行：
  ```bash
  gunicorn -k gevent -w 4 "app:app"
  ```
- 将静态文件与图片目录接入对象存储或 CDN，可进一步提升下载速度。

## 开发与调试
- 快速语法检查：`python -m compileall app.py`
- 推荐在虚拟环境中运行 `flask shell` 进行数据调试。
- 若需要清理测试数据，删除 `lab.db`（或目标数据库）及相关迁移记录后重新执行迁移。

## 常见问题
- **图片无法显示？** 请确认 `images/` 目录具有写权限，并检查服务器是否允许访问 `/uploads/<filename>`。
- **数据库锁冲突？** SQLite 在并发写入时可能锁表，默认配置已启用 WAL、busy_timeout；若并发场景复杂，建议更换为 PostgreSQL。
- **默认账号安全性？** 部署后务必修改管理员密码，并考虑限制注册入口。

欢迎根据实验室实际需求继续扩展模块，或结合硬件设备（扫码枪、智能柜等）打造自动化管理流程。

