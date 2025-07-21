import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkey'  # 为安全起见，部署时请使用更复杂的随机值
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///lab.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'images')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 限制上传16MB以内的文件

db = SQLAlchemy(app)
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
    # 关系：成员负责的物品和位置，以及发送/收到的消息和日志
    items = db.relationship('Item', backref='responsible_member', lazy=True, foreign_keys='Item.responsible_id')
    locations = db.relationship('Location', backref='responsible_member', lazy=True, foreign_keys='Location.responsible_id')
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
    category = db.Column(db.String(50))                 # 类别/危险级别
    status = db.Column(db.String(50))                   # 当前状态（库存/危险等）
    responsible_id = db.Column(db.Integer, db.ForeignKey('members.id'))  # 负责人（成员ID）
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))   # 存放位置（位置ID）
    image = db.Column(db.String(200))                   # 图片文件名
    notes = db.Column(db.Text)                          # 备注说明
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    purchase_link = db.Column(db.String(200))           # 购买链接
    pos_x = db.Column(db.Float)                         # 在位置图片上的标记X坐标（百分比）
    pos_y = db.Column(db.Float)                         # 在位置图片上的标记Y坐标（百分比）
    logs = db.relationship('Log', backref='item', lazy=True)  # 操作日志

    def __repr__(self):
        return f'<Item {self.name}>'

class Location(db.Model):
    __tablename__ = 'locations'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)    # 位置名称
    responsible_id = db.Column(db.Integer, db.ForeignKey('members.id'))  # 负责人（成员ID）
    image = db.Column(db.String(200))                   # 位置图片文件名
    notes = db.Column(db.Text)                          # 备注
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    items = db.relationship('Item', backref='location', lazy=True, foreign_keys='Item.location_id')
    logs = db.relationship('Log', backref='location', lazy=True)     # 操作日志

    def __repr__(self):
        return f'<Location {self.name}>'

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
    # 未登录则跳转到登录页，已登录则进入个人主页
    if current_user.is_authenticated:
        return redirect(url_for('profile', member_id=current_user.id))
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
        items_query = items_query.filter(Item.name.contains(search) | Item.notes.contains(search))
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
    if request.method == 'POST':
        # 获取表单数据并创建新物品
        name = request.form.get('name')
        category = request.form.get('category')
        status = request.form.get('status')
        responsible_id = request.form.get('responsible_id')
        location_id = request.form.get('location_id')
        notes = request.form.get('notes')
        purchase_link = request.form.get('purchase_link')
        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            # 保存上传的物品图片文件
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            image_filename = filename
        new_item = Item(
            name=name, category=category, status=status,
            responsible_id=responsible_id if responsible_id else None,
            location_id=location_id if location_id else None,
            notes=notes, purchase_link=purchase_link, image=image_filename
        )
        db.session.add(new_item)
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, item_id=new_item.id, action_type="新增物品", details=f"Added item {new_item.name}")
        db.session.add(log)
        db.session.commit()
        flash('物品已添加', 'success')
        return redirect(url_for('items'))
    # GET 请求时，返回物品添加表单
    members = Member.query.all()
    locations = Location.query.all()
    return render_template('item_form.html', members=members, locations=locations, item=None)

@app.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_item(item_id):
    item = Item.query.get_or_404(item_id)
    if request.method == 'POST':
        # 更新物品信息
        item.name = request.form.get('name')
        item.category = request.form.get('category')
        item.status = request.form.get('status')
        item.responsible_id = request.form.get('responsible_id')
        item.location_id = request.form.get('location_id')
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
            # 可选：删除旧图片文件以节省空间（此处暂不删除）
            item.image = filename
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
    locations = Location.query.order_by(Location.name).all()
    return render_template('locations.html', locations=locations)

@app.route('/locations/add', methods=['GET', 'POST'])
@login_required
def add_location():
    if request.method == 'POST':
        # 获取并保存新的位置记录
        name = request.form.get('name')
        responsible_id = request.form.get('responsible_id')
        notes = request.form.get('notes')
        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            image_filename = filename
        new_loc = Location(name=name, responsible_id=responsible_id if responsible_id else None, notes=notes, image=image_filename)
        db.session.add(new_loc)
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, location_id=new_loc.id, action_type="新增位置", details=f"Added location {new_loc.name}")
        db.session.add(log)
        db.session.commit()
        flash('实验室位置已添加', 'success')
        return redirect(url_for('locations_list'))
    members = Member.query.all()
    return render_template('location_form.html', members=members, location=None)

@app.route('/locations/<int:loc_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    if request.method == 'POST':
        # 更新位置信息
        location.name = request.form.get('name')
        location.responsible_id = request.form.get('responsible_id')
        location.notes = request.form.get('notes')
        image_file = request.files.get('image')
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            location.image = filename
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
    # 获取该位置包含的所有物品及其坐标
    items_at_location = Item.query.filter_by(location_id=loc_id).all()
    return render_template('location.html', location=location, items=items_at_location)

@app.route('/locations/<int:loc_id>/set_item_position', methods=['POST'])
@login_required
def set_item_position(loc_id):
    # AJAX/表单提交物品坐标标记
    item_id = request.form.get('item_id')
    pos_x = request.form.get('pos_x')
    pos_y = request.form.get('pos_y')
    item = Item.query.get_or_404(item_id)
    if item.location_id == loc_id:
        try:
            item.pos_x = float(pos_x)
            item.pos_y = float(pos_y)
        except:
            item.pos_x = None
            item.pos_y = None
        db.session.commit()
        log = Log(user_id=current_user.id, item_id=item.id, action_type="标记位置", details=f"Set position for item {item.name} in location {loc_id}")
        db.session.add(log)
        db.session.commit()
        flash(f"物品 {item.name} 的位置已更新", "success")
    else:
        flash("无效的物品或位置", "danger")
    return redirect(url_for('view_location', loc_id=loc_id))

@app.route('/members')
@login_required
def members_list():
    members = Member.query.order_by(Member.name).all()
    return render_template('members.html', members=members)

@app.route('/member/<int:member_id>')
@login_required
def profile(member_id):
    member = Member.query.get_or_404(member_id)
    # 该成员负责的物品和位置列表
    items_resp = Item.query.filter_by(responsible_id=member.id).all()
    locations_resp = Location.query.filter_by(responsible_id=member.id).all()
    # 通知列表：他人对该成员负责的物品/位置的最近更新
    notifications = []
    if member.id == current_user.id:
        from sqlalchemy import or_
        notifications = Log.query.join(Item, Log.item_id == Item.id, isouter=True) \
                        .join(Location, Log.location_id == Location.id, isouter=True) \
                        .filter(or_(Item.responsible_id == member.id, Location.responsible_id == member.id), Log.user_id != member.id) \
                        .order_by(Log.timestamp.desc()).limit(5).all()
    # 留言板消息列表（发送给该成员的留言）
    messages = Message.query.filter_by(receiver_id=member.id).order_by(Message.timestamp.desc()).all()
    # 当前用户自己的操作记录（仅查看自己的主页时显示）
    user_logs = []
    if member.id == current_user.id:
        user_logs = Log.query.filter_by(user_id=member.id).order_by(Log.timestamp.desc()).limit(5).all()
    return render_template('profile.html', profile_user=member, items_resp=items_resp, locations_resp=locations_resp, notifications=notifications, messages=messages, user_logs=user_logs)

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
    app.run(debug=False)