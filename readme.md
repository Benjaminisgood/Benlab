# ✅ macOS 环境下运行指令

🔹 1. 创建项目目录（例如放在桌面）

cd ~/Desktop
mkdir chem_lab_system
cd chem_lab_system

🔹 2. 建立虚拟环境并激活（推荐）

python3 -m venv venv
source venv/bin/activate

🔹 3. 创建并保存 requirements.txt 文件

nano requirements.txt

将上方内容粘贴进去，保存并退出（Ctrl + O → 回车，Ctrl + X）。

🔹 4. 安装依赖

pip install -r requirements.txt

🔹 5. 创建目录结构（如上传目录）

mkdir -p static/images templates

将我上面给出的 Python 文件（如 app.py）、HTML 模板（如 templates/base.html）放入对应路径。

🔹 6. 初始化数据库（首次运行自动）

python app.py

终端将输出：

 * Running on http://127.0.0.1:5000

浏览器访问：
👉 http://127.0.0.1:5000
初始默认账号为：

用户名：admin
密码：admin

🔹 7. 生产模式公网启动

flask run --host=0.0.0.0 --port=5000

gunicorn -w 3 -b 0.0.0.0:8000 app:app

gunicorn -w 2 -k gthread --threads 6 -b 0.0.0.0:8000 app:app \
  --timeout 120 --access-logfile - --error-logfile -

# ✅ 一键上传项目到 GitHub 的完整步骤

你已经在本地有了项目目录，比如：~/Desktop/Benlab
并且你已在该路径运行过 Flask 项目（例如包含 app.py、templates/ 等）。

🔹 1. 打开终端并进入项目目录

cd ~/Desktop/Benlab


⸻

🔹 2. 初始化 Git 并添加远程仓库

git init
git remote add origin https://github.com/Benjaminisgood/Benlab.git


⸻

🔹 3. 添加 .gitignore 文件（忽略虚拟环境等）

echo "venv/" > .gitignore
echo "__pycache__/" >> .gitignore
echo "*.pyc" >> .gitignore
echo "*.sqlite3" >> .gitignore

⸻

🔹 4. 添加项目所有文件并提交

git add .
git commit -m "🎉 Initial commit of chemical lab management system"

⸻

🔹 5. 推送到 GitHub 主分支 main

git branch -M main
git push -u origin main

⸻

✅ 之后更新项目的方法（命令回顾）

git add .
git commit -m "✨ 更新内容说明"
git push

⸻

# 🧪 Flask-Migrate 数据库迁移流程

✅ 步骤 1：安装依赖

pip install flask-migrate

确保虚拟环境已激活，否则就会装到全局环境里。

⸻

✅ 步骤 2：修改 app.py 支持 Flask-Migrate

在 app.py 里加入以下内容（你已写了 db = SQLAlchemy(app)）：

from flask_migrate import Migrate

在 db 初始化之后加  

migrate = Migrate(app, db)

⸻

✅ 步骤 3：初始化迁移目录

在项目根目录执行：

export FLASK_APP=app.py  # Mac/Linux
set FLASK_APP=app.py  # Windows

flask db init

会创建一个 migrations/ 文件夹用于跟踪迁移历史。

⸻

✅ 步骤 4：生成初始迁移脚本

flask db migrate -m "Initial migration"

这一步会根据模型变化自动生成迁移脚本（不会立即修改数据库）。

⸻

✅ 步骤 5：应用迁移到数据库

flask db upgrade

⸻

# 📌 注意事项与补充

flask db migrate -m "Added xxx"
flask db upgrade
flask db stamp head

cd /Users/benserver/Desktop/Benlab
gunicorn -w 4 -k gevent --worker-connections 1000 \
  -b 0.0.0.0:8000 "app:app" \
  --timeout 120 --access-logfile - --error-logfile -

gunicorn -w 2 -k gthread --threads 6 -b 0.0.0.0:8000 app:app \
  --timeout 120 --access-logfile - --error-logfile -

1) 安装
pip install "gunicorn>=21" "gevent>=24"

2) 安全变量
export FLASK_SECRET_KEY="$(python - <<'PY'
import secrets; print(secrets.token_urlsafe(32))
PY
)"

3) 启动（本机服务器）
cd /Users/benserver/Desktop/Benlab
gunicorn -w 4 -k gevent --worker-connections 1000 \
  -b 0.0.0.0:8000 "app:app" \
  --timeout 120 --access-logfile - --error-logfile -
