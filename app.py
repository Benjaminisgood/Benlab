import os
from datetime import datetime
import re
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from collections import Counter
from sqlalchemy.orm import selectinload
from sqlalchemy import or_, func
from flask_migrate import Migrate


app = Flask(__name__)
# Behind reverse proxy (e.g., Nginx) fix: trust X-Forwarded-* headers
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# 优先从环境变量读取，生产环境请设置强随机串：export FLASK_SECRET_KEY='...'
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev-only-change-me')
# 注意：SQLite 在多进程/并发写入下会锁表；生产建议换 PostgreSQL。
# 这里为提高并发容忍度，加入超时参数；并在 engine options 中关闭线程检查。
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///lab.db?timeout=30'
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'connect_args': {'check_same_thread': False}
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 使用绝对路径，避免工作目录变化导致保存失败
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'static', 'images')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 限制上传16MB以内的文件

# ---- SQLite 并发优化（仅在使用 sqlite 时启用）----
# 启用 WAL、调整同步级别与 busy_timeout，提升多读单写体验
from sqlalchemy import event
from sqlalchemy.engine import Engine
import sqlite3
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        # 写放大较小、可多读；适合 10 人以内轻并发
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        # 写锁等待 30s，降低 'database is locked' 频率
        cursor.execute("PRAGMA busy_timeout=30000;")
        # 内存临时表与适度缓存
        cursor.execute("PRAGMA temp_store=MEMORY;")
        cursor.execute("PRAGMA cache_size=-20000;")  # 约 20MB
        cursor.close()

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# 物品 与 位置：多对多关联表
item_locations = db.Table(
    'item_locations',
    db.Column('item_id', db.Integer, db.ForeignKey('items.id'), primary_key=True),
    db.Column('location_id', db.Integer, db.ForeignKey('locations.id'), primary_key=True)
)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# 允许上传的图片扩展名
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 数据模型定义
class Member(UserMixin, db.Model):
    __tablename__ = 'members'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)            # 姓名
    username = db.Column(db.String(100), unique=True, nullable=False)  # 登录用户名
    password_hash = db.Column(db.String(200), nullable=False)    # 密码哈希
    contact = db.Column(db.String(100))                          # 联系方式（邮箱/电话）
    photo = db.Column(db.String(200))                            # 头像图片路径
    notes = db.Column(db.Text)                                   # 备注/个人展示板
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    # 关系：成员负责的物品，以及发送/收到的消息和日志
    items = db.relationship('Item', backref='responsible_member', lazy=True, foreign_keys='Item.responsible_id')
    # 负责的位置：多对多 responsible_locations
    responsible_locations = db.relationship(
        'Location',
        secondary='location_members',
        backref=db.backref('responsible_members', lazy='select'),
        lazy='select'
    )
    sent_messages = db.relationship('Message', backref='sender', lazy=True, foreign_keys='Message.sender_id')
    received_messages = db.relationship('Message', backref='receiver', lazy=True, foreign_keys='Message.receiver_id')
    logs = db.relationship('Log', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    def __repr__(self):
        return f'<Member {self.username}>'

class Item(db.Model):
    __tablename__ = 'items'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)    # 物品名称
    cas_no = db.Column(db.String(64), nullable=True)     # ✅ CAS 号（可为空）
    category = db.Column(db.String(50))                 # 类别/危险级别
    stock_status = db.Column(db.String(50))        # ✅ 新增字段：库存状态
    features = db.Column(db.String(200))           # ✅ 多选：用逗号分隔
    value = db.Column(db.Float)                    # ✅ 价值（数字）
    quantity = db.Column(db.Float)                 # ✅ 数量
    unit = db.Column(db.String(20))                # ✅ 单位（例如：瓶、包）
    purchase_date = db.Column(db.Date)             # ✅ 购入时间
    responsible_id = db.Column(db.Integer, db.ForeignKey('members.id'))  # 负责人（成员ID）
    image = db.Column(db.String(200))                   # 图片文件名
    notes = db.Column(db.Text)                          # 备注说明
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    purchase_link = db.Column(db.String(200))           # 购买链接
    # 多对多：一个物品可出现在多个位置
    locations = db.relationship(
        'Location',
        secondary='item_locations',
        backref=db.backref('items', lazy='select'),
        lazy='select'
    )
    logs = db.relationship('Log', backref='item', lazy=True)  # 操作日志

    def __repr__(self):
        return f'<Item {self.name}>'

class Location(db.Model):
    __tablename__ = 'locations'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)    # 位置名称
    parent_id = db.Column(db.Integer, db.ForeignKey('locations.id'))  # 父级位置
    clean_status = db.Column(db.String(20))
    children = db.relationship('Location',
                            backref=db.backref('parent', remote_side=[id]),
                            cascade='all, delete-orphan')
    # 多对多负责人
    # responsible_members 关系由 Member.responsible_locations 的 backref 提供
    image = db.Column(db.String(200))                   # 位置图片文件名
    notes = db.Column(db.Text)                          # 备注
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    logs = db.relationship('Log', backref='location', lazy=True)     # 操作日志
    detail_link = db.Column(db.String(200))

    def __repr__(self):
        return f'<Location {self.name}>'
# 多对多：位置-成员负责人表
location_members = db.Table(
    'location_members',
    db.Column('location_id', db.Integer, db.ForeignKey('locations.id'), primary_key=True),
    db.Column('member_id', db.Integer, db.ForeignKey('members.id'), primary_key=True)
)

class Log(db.Model):
    __tablename__ = 'logs'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('members.id'))     # 执行操作的用户ID
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=True)         # 涉及的物品ID（如果有）
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=True) # 涉及的位置ID（如果有）
    action_type = db.Column(db.String(50))       # 操作类型描述（如 新增物品/修改位置 等）
    details = db.Column(db.Text)                 # 详情备注
    def __repr__(self):
        return f'<Log {self.action_type} by {self.user_id}>'

class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('members.id'))    # 发送者用户ID
    receiver_id = db.Column(db.Integer, db.ForeignKey('members.id'))  # 接收者用户ID
    content = db.Column(db.Text, nullable=False)     # 留言内容
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)  # 留言时间
    def __repr__(self):
        return f'<Message from {self.sender_id} to {self.receiver_id}>'

# 初始化数据库并创建默认用户
with app.app_context():
    db.create_all()
    if Member.query.count() == 0:
        default_user = Member(name="Admin User", username="admin", contact="admin@example.com", notes="Default admin user")
        default_user.set_password("admin")
        db.session.add(default_user)
        db.session.commit()

@login_manager.user_loader
def load_user(user_id):
    return Member.query.get(int(user_id))

# 路由定义
@app.route('/')
def index():
    # 未登录则跳转到登录页，已登录则进入管理员主页（固定 member_id=1）
    if current_user.is_authenticated:
        return redirect(url_for('profile', member_id=1))
    else:
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = Member.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('用户名或密码不正确', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form.get('name')
        username = request.form.get('username')
        password = request.form.get('password')
        contact = request.form.get('contact')
        # 检查用户名是否已存在
        if Member.query.filter_by(username=username).first():
            flash('用户名已存在', 'warning')
        else:
            new_user = Member(name=name, username=username, contact=contact)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            flash('注册成功，请登录', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/items')
@login_required
def items():
    # 物品列表，支持按名称/备注搜索，按类别筛选
    search = request.args.get('search', '')
    category_filter = request.args.get('category', '')
    items_query = Item.query
    if search:
        s = search.strip()
        # 去除非数字字符，便于做“无连字符”的模糊匹配（输入 '6' 也能匹配）
        s_digits = re.sub(r'[^0-9]', '', s)
        cas_col = func.coalesce(Item.cas_no, '')
        cas_no_hyphenless = func.replace(cas_col, '-', '')
        like_s = f"%{s}%"
        items_conds = [
            Item.name.ilike(like_s),
            Item.notes.ilike(like_s),
            Item.cas_no.ilike(like_s),
        ]
        if s_digits:
            like_digits = f"%{s_digits}%"
            items_conds.append(cas_no_hyphenless.like(like_digits))
        items_query = items_query.filter(or_(*items_conds))
    if category_filter:
        items_query = items_query.filter_by(category=category_filter)
    items_list = items_query.order_by(Item.name).all()
    # 获取现有类别列表供筛选选项
    categories = [c for (c,) in db.session.query(Item.category).distinct() if c]
    return render_template('items.html', items=items_list, search=search, category=category_filter, categories=categories)

@app.route('/items/<int:item_id>')
@login_required
def item_detail(item_id):
    # 查看物品详情
    item = Item.query.get_or_404(item_id)
    return render_template('item_detail.html', item=item)

@app.route('/items/add', methods=['GET', 'POST'])
@login_required
def add_item():
    default_loc_id = request.args.get('loc_id', type=int)
    if request.method == 'POST':
        # 获取表单数据
        name = request.form.get('name')
        category = request.form.get('category')
        cas_no = request.form.get('cas_no')

        stock_status = request.form.get('stock_status')  # ✅ 单选字段
        features_str = request.form.get('features')  # 返回单个字符串

        value = request.form.get('value')
        value = float(value) if value else None          # ✅ 数字输入

        quantity = request.form.get('quantity')
        quantity = float(quantity) if quantity else None # ✅ 数量输入
        unit = request.form.get('unit')                  # ✅ 单位选择

        purchase_date_str = request.form.get('purchase_date')  # ✅ 日期处理
        purchase_date = datetime.strptime(purchase_date_str, '%Y-%m-%d').date() if purchase_date_str else None

        responsible_id = request.form.get('responsible_id')
        location_ids = request.form.getlist('location_ids')  # ✅ 支持多个位置
        notes = request.form.get('notes')
        purchase_link = request.form.get('purchase_link')

        # 图片处理
        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            image_filename = filename

        # ✅ 创建新的 Item 实例（已更新字段）
        new_item = Item(
            name=name,
            cas_no=cas_no or None,
            category=category,
            stock_status=stock_status,
            features=features_str,
            value=value,
            quantity=quantity,
            unit=unit,
            purchase_date=purchase_date,
            responsible_id=responsible_id if responsible_id else current_user.id,
            notes=notes,
            purchase_link=purchase_link,
            image=image_filename
        )
        # 绑定多个位置（若前端未选择则为空列表）
        loc_ids = [int(x) for x in location_ids] if location_ids else []
        if not loc_ids and default_loc_id:
            loc_ids = [default_loc_id]
        if loc_ids:
            new_item.locations = Location.query.filter(Location.id.in_(loc_ids)).all()
        db.session.add(new_item)
        db.session.commit()

        # ✅ 写入日志
        log = Log(
            user_id=current_user.id,
            item_id=new_item.id,
            action_type="新增物品",
            details=f"Added item {new_item.name}"
        )
        db.session.add(log)
        db.session.commit()

        flash('物品已添加', 'success')
        return redirect(url_for('items'))

    # GET 请求
    members = Member.query.all()
    locations = Location.query.all()
    return render_template('item_form.html', members=members, locations=locations, item=None, default_loc_id=default_loc_id)

@app.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_item(item_id):
    item = Item.query.get_or_404(item_id)
    if request.method == 'POST':
        # 更新物品信息
        item.name = request.form.get('name')
        item.category = request.form.get('category')
        item.cas_no = request.form.get('cas_no') or None
        
        item.stock_status = request.form.get('stock_status')  # 单选库存状态
        item.features = request.form.get('features')          # 单选物品特性
        
        item.value = request.form.get('value', type=float)    # 新增：价值（数值）
        item.quantity = request.form.get('quantity', type=float)  # 数量（数字）
        item.unit = request.form.get('unit')                      # 数量单位

        purchase_date_str = request.form.get('purchase_date')
        if purchase_date_str:
            item.purchase_date = datetime.strptime(purchase_date_str, '%Y-%m-%d').date()

        item.responsible_id = request.form.get('responsible_id')
        location_ids = request.form.getlist('location_ids')  # ✅ 多选位置
        # 同步多对多关系
        loc_ids = [int(x) for x in location_ids] if location_ids else []
        item.locations = Location.query.filter(Location.id.in_(loc_ids)).all() if loc_ids else []
        item.notes = request.form.get('notes')
        item.purchase_link = request.form.get('purchase_link')

        # 处理图片更新（如果有新上传）
        image_file = request.files.get('image')
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            item.image = filename  # 更新图片路径

        item.last_modified = datetime.utcnow()
        db.session.commit()

        # 记录日志
        log = Log(user_id=current_user.id, item_id=item.id, action_type="修改物品", details=f"Edited item {item.name}")
        db.session.add(log)
        db.session.commit()

        flash('物品信息已更新', 'success')
        return redirect(url_for('items'))

    members = Member.query.all()
    locations = Location.query.all()
    return render_template('item_form.html', members=members, locations=locations, item=item)

@app.route('/items/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    # 记录日志
    log = Log(user_id=current_user.id, item_id=item_id, action_type="删除物品", details=f"Deleted item {item.name}")
    db.session.add(log)
    db.session.commit()
    flash('物品已删除', 'info')
    return redirect(url_for('items'))

@app.route('/locations')
@login_required
def locations_list():
    locations = Location.query.options(
        # selectinload 用于加载多级 children 避免 N+1 查询问题
        db.selectinload(Location.children).selectinload(Location.children)
    ).order_by(Location.name).all()
    return render_template('locations.html', locations=locations)

@app.route('/locations/add', methods=['GET', 'POST'])
@login_required
def add_location():
    if request.method == 'POST':
        # 获取并保存新的位置记录
        name = request.form.get('name')
        clean_status = request.form.get('clean_status') or None
        parent_id = request.form.get('parent_id')
        responsible_ids = request.form.getlist('responsible_ids')
        notes = request.form.get('notes')
        detail_link = request.form.get('detail_link')
        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            image_filename = filename
        # 先创建 Location
        new_loc = Location(
            name=name,
            parent_id=parent_id if parent_id else None,
            notes=notes,
            image=image_filename,
            clean_status=clean_status,
            detail_link=detail_link
        )
        db.session.add(new_loc)
        db.session.commit()
        # 多负责人
        member_objs = []
        if responsible_ids:
            member_objs = Member.query.filter(Member.id.in_([int(mid) for mid in responsible_ids])).all()
        if not member_objs:
            member_objs = [current_user]
        new_loc.responsible_members = member_objs
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, location_id=new_loc.id, action_type="新增位置", details=f"Added location {new_loc.name}")
        db.session.add(log)
        db.session.commit()

        flash('实验室位置已添加', 'success')
        return redirect(url_for('locations_list'))
    
    members = Member.query.all()
    parents = Location.query.all()
    return render_template('location_form.html', members=members, location=None, parents=parents)

@app.route('/locations/<int:loc_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    if request.method == 'POST':
        # 更新位置信息
        location.name = request.form.get('name')
        responsible_ids = request.form.getlist('responsible_ids')
        notes = request.form.get('notes')
        detail_link = request.form.get('detail_link')
        image_file = request.files.get('image')
        location.clean_status = request.form.get('clean_status') or None
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            location.image = filename
        # 多负责人
        member_objs = []
        if responsible_ids:
            member_objs = Member.query.filter(Member.id.in_([int(mid) for mid in responsible_ids])).all()
        if not member_objs:
            member_objs = [current_user]
        location.responsible_members = member_objs
        location.notes = notes
        location.detail_link = detail_link
        location.last_modified = datetime.utcnow()
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, location_id=location.id, action_type="修改位置", details=f"Edited location {location.name}")
        db.session.add(log)
        db.session.commit()
        flash('位置信息已更新', 'success')
        return redirect(url_for('locations_list'))
    members = Member.query.all()
    return render_template('location_form.html', members=members, location=location)

@app.route('/locations/<int:loc_id>/delete', methods=['POST'])
@login_required
def delete_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    db.session.delete(location)
    db.session.commit()
    # 记录日志
    log = Log(user_id=current_user.id, location_id=loc_id, action_type="删除位置", details=f"Deleted location {location.name}")
    db.session.add(log)
    db.session.commit()
    flash('实验室位置已删除', 'info')
    return redirect(url_for('locations_list'))

@app.route('/locations/<int:loc_id>')
@login_required
def view_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    # 获取该位置包含的所有物品（多对多）
    items_at_location = list(location.items)
    # 分类统计状态标签（如：用完、少量、充足）
    status_counter = Counter()
    for item in items_at_location:
        if item.stock_status:
            statuses = item.stock_status.split(',')  # 支持多个状态
            for s in statuses:
                status_counter[s.strip()] += 1

    return render_template('location.html', location=location, 
                           items=items_at_location,
                           status_counter=status_counter)


@app.route('/members')
@login_required
def members_list():
    members = Member.query.order_by(Member.name).all()
    return render_template('members.html', members=members)

@app.route('/member/<int:member_id>')
@login_required
def profile(member_id):
    member = Member.query.get_or_404(member_id)

    # 负责的物品
    all_items = Item.query.filter_by(responsible_id=member.id).all()
    # 负责的位置（多对多）
    all_locations = list(member.responsible_locations)

    # 分开“告警”和“正常”
    critical_items = [it for it in all_items if it.stock_status and '用完' in it.stock_status]
    normal_items = [it for it in all_items if not (it.stock_status and '用完' in it.stock_status)]
    items_resp = critical_items + normal_items  # 用完的置顶

    critical_locs = [loc for loc in all_locations if loc.clean_status == '脏/报修']
    normal_locs = [loc for loc in all_locations if loc.clean_status != '脏/报修']
    locations_resp = critical_locs + normal_locs  # 脏/报修的置顶

    any_item_empty = any(it.stock_status and '用完' in it.stock_status for it in items_resp)
    any_location_dirty = any(loc.clean_status == '脏/报修' for loc in locations_resp)

    # 通知列表：他人对该成员负责的物品/位置的最近更新
    notifications = []
    if member.id == current_user.id:
        from sqlalchemy import or_
        # 只查找该成员负责的物品和位置的日志（物品表的responsible_id，位置表通过多对多）
        location_ids = [loc.id for loc in member.responsible_locations]
        notifications = Log.query.join(Item, Log.item_id == Item.id, isouter=True) \
                        .join(Location, Log.location_id == Location.id, isouter=True) \
                        .filter(
                            or_(
                                Item.responsible_id == member.id,
                                Location.id.in_(location_ids) if location_ids else False
                            ),
                            Log.user_id != member.id
                        ) \
                        .order_by(Log.timestamp.desc()).limit(5).all()
    # 留言板消息列表（发送给该成员的留言）
    messages = Message.query.filter_by(receiver_id=member.id).order_by(Message.timestamp.desc()).limit(10).all()
    # 当前用户自己的操作记录（仅查看自己的主页时显示）
    user_logs = []
    if member.id == current_user.id:
        user_logs = Log.query.filter_by(user_id=member.id).order_by(Log.timestamp.desc()).limit(5).all()
    return render_template('profile.html', 
                           profile_user=member, 
                           items_resp=items_resp, 
                           locations_resp=locations_resp, 
                           notifications=notifications, 
                           messages=messages, 
                           user_logs=user_logs,
                           any_item_empty=any_item_empty,
                           any_location_dirty=any_location_dirty)

@app.route('/member/<int:member_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_profile(member_id):
    if current_user.id != member_id:
        flash('无权编辑他人信息', 'danger')
        return redirect(url_for('profile', member_id=member_id))
    member = Member.query.get_or_404(member_id)
    if request.method == 'POST':
        # 更新个人信息
        member.name = request.form.get('name')
        member.contact = request.form.get('contact')
        member.notes = request.form.get('notes')
        # 如填写了新密码则更新密码
        new_password = request.form.get('password')
        if new_password and new_password.strip() != '':
            member.set_password(new_password)
        # 更新头像
        image_file = request.files.get('photo')
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            member.photo = filename
        member.last_modified = datetime.utcnow()
        db.session.commit()
        flash('个人信息已更新', 'success')
        return redirect(url_for('profile', member_id=member_id))
    return render_template('edit_profile.html', member=member)

@app.route('/message/<int:member_id>', methods=['POST'])
@login_required
def post_message(member_id):
    # 提交留言
    receiver = Member.query.get_or_404(member_id)
    content = request.form.get('content')
    if content and content.strip() != '':
        msg = Message(sender_id=current_user.id, receiver_id=receiver.id, content=content.strip())
        db.session.add(msg)
        db.session.commit()
        flash('留言已发布', 'success')
    return redirect(url_for('profile', member_id=member_id))

@app.route('/export/<string:datatype>')
@login_required
def export_data(datatype):
    # 导出数据为 CSV 文件
    import pandas as pd
    filename = f"export_{datatype}.csv"
    if datatype == 'items':
        df = pd.read_sql(Item.query.statement, db.session.bind)
    elif datatype == 'members':
        df = pd.read_sql(Member.query.statement, db.session.bind)
    elif datatype == 'locations':
        df = pd.read_sql(Location.query.statement, db.session.bind)
    elif datatype == 'logs':
        df = pd.read_sql(Log.query.statement, db.session.bind)
    elif datatype == 'messages':
        df = pd.read_sql(Message.query.statement, db.session.bind)
    else:
        flash('未知数据类型', 'warning')
        return redirect(url_for('index'))
    df.to_csv(filename, index=False)
    return send_file(filename, as_attachment=True, mimetype='text/csv', download_name=filename)

if __name__ == '__main__':
    # 本地开发便捷启动；生产由 gunicorn/uvicorn 托管
    debug = os.getenv('FLASK_DEBUG', '1') == '1'
    app.run(host='127.0.0.1', port=8000, debug=debug)
    print("Server is running on the http://127.0.0.1:8000")