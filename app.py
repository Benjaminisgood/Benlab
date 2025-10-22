import os
from datetime import datetime
import re
import json
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, send_from_directory, abort, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from collections import Counter
from sqlalchemy.orm import selectinload, load_only
from sqlalchemy import or_, func, text, inspect
from flask_migrate import Migrate
from markupsafe import Markup, escape


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
# app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'static', 'images')
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'images')
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

# 事项与物品/位置的关联表
event_items = db.Table(
    'event_items',
    db.Column('event_id', db.Integer, db.ForeignKey('events.id'), primary_key=True),
    db.Column('item_id', db.Integer, db.ForeignKey('items.id'), primary_key=True)
)

event_locations = db.Table(
    'event_locations',
    db.Column('event_id', db.Integer, db.ForeignKey('events.id'), primary_key=True),
    db.Column('location_id', db.Integer, db.ForeignKey('locations.id'), primary_key=True)
)

member_follows = db.Table(
    'member_follows',
    db.Column('follower_id', db.Integer, db.ForeignKey('members.id'), primary_key=True),
    db.Column('followed_id', db.Integer, db.ForeignKey('members.id'), primary_key=True),
    db.CheckConstraint('follower_id != followed_id', name='ck_member_follows_no_self')
)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# 允许上传的图片扩展名
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _parse_coordinate(raw_value):
    """Return a normalized float for latitude/longitude or None on failure."""
    if raw_value in (None, '', 'undefined'):
        return None
    try:
        return round(float(raw_value), 8)
    except (TypeError, ValueError):
        return None


_EXTERNAL_IMAGE_PREFIXES = ('http://', 'https://', '//')


def _is_external_image(ref):
    return isinstance(ref, str) and ref.startswith(_EXTERNAL_IMAGE_PREFIXES)


def _extract_external_urls(raw_value):
    """Parse newline/comma separated URLs, keeping only web-accessible links."""
    if not raw_value:
        return []
    cleaned = raw_value.replace('\r', '\n')
    chunks = []
    for line in cleaned.split('\n'):
        parts = [segment.strip() for segment in line.split(',') if segment.strip()]
        chunks.extend(parts)
    urls = []
    seen = set()
    for candidate in chunks:
        if candidate.startswith('//') or re.match(r'^https?://', candidate, flags=re.IGNORECASE):
            if candidate not in seen:
                urls.append(candidate)
                seen.add(candidate)
    return urls

_LINK_PATTERN = re.compile(r'(?P<url>https?://[^\s<>"\'`]+)', re.IGNORECASE)
_HASHTAG_PATTERN = re.compile(r'(?<![\w#])#(?P<tag>[\w\u4e00-\u9fa5-]+)')
_MENTION_PATTERN = re.compile(r'(?<![\w@])@(?P<handle>[\w\u4e00-\u9fa5-]+)')
_SENTIMENT_GOOD_TOKEN = '__SENT_POS__'
_SENTIMENT_DOUBT_TOKEN = '__SENT_QUEST__'


def render_rich_text(raw_text, mention_lookup=None):
    """Convert plain text into HTML with clickable links, tags, and mentions."""
    if not raw_text:
        return Markup('')
    safe_text = escape(raw_text)
    html = str(safe_text)
    html = html.replace('!!', _SENTIMENT_GOOD_TOKEN).replace('??', _SENTIMENT_DOUBT_TOKEN)

    def link_repl(match):
        url = match.group('url')
        return f'<a href="{escape(url)}" class="link-chip" target="_blank" rel="noopener">{escape(url)}</a>'

    html = _LINK_PATTERN.sub(link_repl, html)

    def hashtag_repl(match):
        tag = match.group('tag')
        return f'<span class="tag-chip">#{escape(tag)}</span>'

    html = _HASHTAG_PATTERN.sub(hashtag_repl, html)

    def mention_repl(match):
        handle = match.group('handle')
        key = handle.lower()
        member_id = None
        if mention_lookup:
            member_id = mention_lookup.get(key)
        label = escape(handle)
        if member_id:
            href = url_for('profile', member_id=member_id)
            return f'<a href="{escape(href)}" class="mention-chip">@{label}</a>'
        return f'<span class="mention-chip">@{label}</span>'

    html = _MENTION_PATTERN.sub(mention_repl, html)
    html = html.replace(_SENTIMENT_GOOD_TOKEN, '<span class="sentiment-chip sentiment-good">!!</span>')
    html = html.replace(_SENTIMENT_DOUBT_TOKEN, '<span class="sentiment-chip sentiment-doubt">??</span>')
    return Markup(html.replace('\n', '<br>'))


def load_feedback_stream(raw_text):
    """Load serialized feedback entries (JSON lines) into Python dicts."""
    entries = []
    if not raw_text:
        return entries
    for line in raw_text.splitlines():
        data = line.strip()
        if not data:
            continue
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                entries.append(parsed)
        except json.JSONDecodeError:
            entries.append({'content': data})
    return entries


def append_feedback_entry(target, sender, content, limit=200):
    """Append a feedback entry to a model that has a feedback_log column."""
    if not content or not content.strip():
        return None
    current = load_feedback_stream(getattr(target, 'feedback_log', '') or '')
    now = datetime.utcnow()
    entry = {
        'ts': now.isoformat(),
        'sid': sender.id if sender else None,
        'sn': (sender.name or sender.username) if sender else None,
        'content': content.strip()
    }
    current.append(entry)
    serialized = '\n'.join(json.dumps(item, ensure_ascii=False) for item in current[-limit:])
    target.feedback_log = serialized
    return entry


def _parse_iso_timestamp(raw):
    if not raw:
        return None
    try:
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def prepare_feedback_entries(raw_text, member_index, mention_lookup):
    """Convert stored feedback text into rich entries for templates."""
    entries = []
    for data in load_feedback_stream(raw_text):
        content = (data.get('content') or '').strip()
        if not content:
            continue
        ts = _parse_iso_timestamp(data.get('ts'))
        sender_id = data.get('sid')
        sender_name = data.get('sn')
        member = member_index.get(sender_id) if sender_id else None
        display_name = None
        sender_url = None
        if member:
            display_name = member.name or member.username
            sender_url = url_for('profile', member_id=member.id)
        else:
            display_name = sender_name or '匿名'
        sentiment = None
        if '!!' in content:
            sentiment = 'positive'
        elif '??' in content:
            sentiment = 'doubt'
        entries.append({
            'html': render_rich_text(content, mention_lookup),
            'timestamp': ts,
            'timestamp_display': ts.strftime('%Y-%m-%d %H:%M') if ts else '未知时间',
            'sender_id': sender_id,
            'sender_name': display_name,
            'sender_url': sender_url,
            'sentiment': sentiment
        })
    entries.sort(key=lambda item: item['timestamp'] or datetime.min, reverse=True)
    return entries


def build_member_lookup():
    """Return dictionaries for quick member lookup and mention resolution."""
    members = Member.query.options(load_only(Member.id, Member.name, Member.username)).all()
    member_index = {m.id: m for m in members}
    mention_lookup = {}
    for m in members:
        if m.name:
            mention_lookup[m.name.lower()] = m.id
        if m.username:
            mention_lookup[m.username.lower()] = m.id
    return member_index, mention_lookup


def save_uploaded_image(file_storage):
    """Persist an uploaded image and return the stored filename."""
    if not file_storage or file_storage.filename == '':
        return None
    if not allowed_file(file_storage.filename):
        return None
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    filename = secure_filename(file_storage.filename)
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    stored_name = f"{timestamp}_{filename}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], stored_name)
    file_storage.save(filepath)
    return stored_name


def remove_uploaded_file(filename):
    """Delete a previously saved image file if it still exists."""
    if not filename or _is_external_image(filename):
        return
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

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
    feedback_log = db.Column(db.Text, default='')                # 他人评价/留言流（JSON lines）
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
    event_participations = db.relationship('EventParticipant', back_populates='member', cascade='all, delete-orphan', lazy='select')
    following = db.relationship(
        'Member',
        secondary=member_follows,
        primaryjoin=(id == member_follows.c.follower_id),
        secondaryjoin=(id == member_follows.c.followed_id),
        lazy='select',
        backref=db.backref('followers', lazy='select')
    )

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

    images = db.relationship(
        'ItemImage',
        backref='item',
        lazy='select',
        cascade='all, delete-orphan',
        order_by='ItemImage.created_at'
    )

    @property
    def image_filenames(self):
        filenames = []
        seen = set()
        for img in self.images:
            if img.filename and img.filename not in seen:
                filenames.append(img.filename)
                seen.add(img.filename)
        if self.image and self.image not in seen:
            filenames.insert(0, self.image)
        return filenames

    def __repr__(self):
        return f'<Item {self.name}>'

class Location(db.Model):
    __tablename__ = 'locations'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)    # 位置名称
    parent_id = db.Column(db.Integer, db.ForeignKey('locations.id'))  # 父级位置
    clean_status = db.Column(db.String(20))
    latitude = db.Column(db.Float, index=True)
    longitude = db.Column(db.Float, index=True)
    coordinate_source = db.Column(db.String(20))
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

    images = db.relationship(
        'LocationImage',
        backref='location',
        lazy='select',
        cascade='all, delete-orphan',
        order_by='LocationImage.created_at'
    )

    @property
    def image_filenames(self):
        filenames = []
        seen = set()
        for img in self.images:
            if img.filename and img.filename not in seen:
                filenames.append(img.filename)
                seen.add(img.filename)
        if self.image and self.image not in seen:
            filenames.insert(0, self.image)
        return filenames

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
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=True)
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


class EventParticipant(db.Model):
    __tablename__ = 'event_participants'
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), primary_key=True)
    role = db.Column(db.String(20), default='participant', nullable=False)
    status = db.Column(db.String(20), default='confirmed', nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    event = db.relationship('Event', back_populates='participant_links')
    member = db.relationship('Member', back_populates='event_participations')

    def __repr__(self):
        return f'<EventParticipant event={self.event_id} member={self.member_id} role={self.role}>'


class Event(db.Model):
    __tablename__ = 'events'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    visibility = db.Column(db.String(20), nullable=False, default='personal')  # personal/internal/public
    owner_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    detail_link = db.Column(db.String(255))
    feedback_log = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = db.relationship('Member', backref=db.backref('events_owned', lazy='dynamic'))
    participant_links = db.relationship(
        'EventParticipant',
        back_populates='event',
        cascade='all, delete-orphan',
        lazy='select'
    )
    participants = db.relationship(
        'Member',
        secondary='event_participants',
        viewonly=True,
        lazy='select',
        backref=db.backref('events_participating', lazy='select')
    )
    items = db.relationship(
        'Item',
        secondary=event_items,
        lazy='select',
        backref=db.backref('events', lazy='select')
    )
    locations = db.relationship(
        'Location',
        secondary=event_locations,
        lazy='select',
        backref=db.backref('events', lazy='select')
    )
    images = db.relationship(
        'EventImage',
        backref='event',
        lazy='select',
        cascade='all, delete-orphan',
        order_by='EventImage.created_at'
    )
    logs = db.relationship('Log', backref='event', lazy=True)

    @property
    def image_filenames(self):
        return [img.filename for img in self.images if img.filename]

    def can_view(self, member):
        if self.visibility == 'public':
            return True
        if not member:
            return False
        if member.id == self.owner_id:
            return True
        return any(link.member_id == member.id for link in self.participant_links)

    def can_edit(self, member):
        return bool(member and member.id == self.owner_id)

    def can_join(self, member):
        if not member or member.id == self.owner_id:
            return False
        if self.visibility != 'public':
            return False
        return all(link.member_id != member.id for link in self.participant_links)

    def is_participant(self, member):
        if not member:
            return False
        return any(link.member_id == member.id for link in self.participant_links)

    def touch(self):
        self.updated_at = datetime.utcnow()

    def participant_count(self):
        return len(self.participant_links)

    def __repr__(self):
        return f'<Event {self.id} {self.title}>'


def parse_datetime_local(raw_value):
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value, '%Y-%m-%dT%H:%M')
    except (ValueError, TypeError):
        return None


def format_datetime_local(value):
    if not value:
        return ''
    return value.strftime('%Y-%m-%dT%H:%M')


def detect_entity_mentions(text, model):
    if not text:
        return []
    mentions = []
    seen_ids = set()
    for obj in model.query.order_by(model.name).all():
        name = getattr(obj, 'name', None)
        if not name or obj.id in seen_ids:
            continue
        if name in text:
            mentions.append(obj)
            seen_ids.add(obj.id)
    return mentions


def link_text_with_entities(text, replacements):
    if not text:
        return Markup('')
    if not replacements:
        return Markup(escape(text))
    # 优先替换较长的名称，避免短词抢占
    ordered_names = sorted(replacements.keys(), key=len, reverse=True)
    pattern = re.compile('|'.join(re.escape(name) for name in ordered_names))
    pieces = []
    last_index = 0
    for match in pattern.finditer(text):
        start, end = match.span()
        if start > last_index:
            pieces.append(escape(text[last_index:start]))
        name = match.group(0)
        url = replacements.get(name)
        if url:
            pieces.append(Markup(f'<a href="{url}">{escape(name)}</a>'))
        else:
            pieces.append(escape(name))
        last_index = end
    if last_index < len(text):
        pieces.append(escape(text[last_index:]))
    return Markup('').join(pieces)


def compute_missing_resources(event):
    content = event.description or ''
    mentioned_items = detect_entity_mentions(content, Item)
    mentioned_locations = detect_entity_mentions(content, Location)
    selected_item_ids = {item.id for item in event.items}
    selected_location_ids = {loc.id for loc in event.locations}
    missing_items = [item for item in mentioned_items if item.id not in selected_item_ids]
    missing_locations = [loc for loc in mentioned_locations if loc.id not in selected_location_ids]
    return missing_items, missing_locations


def build_event_view_model(event):
    content = event.description or ''
    item_links = {item.name: url_for('item_detail', item_id=item.id) for item in event.items if item.name}
    location_links = {loc.name: url_for('view_location', loc_id=loc.id) for loc in event.locations if loc.name}
    combined_links = {**item_links, **{k: v for k, v in location_links.items() if k not in item_links}}
    linked_content = link_text_with_entities(content, combined_links)
    missing_items, missing_locations = compute_missing_resources(event)
    return linked_content, missing_items, missing_locations


def ensure_owner_participation(event):
    owner_link = None
    for link in event.participant_links:
        if link.member_id == event.owner_id:
            owner_link = link
            break
    if owner_link:
        owner_link.role = 'owner'
        owner_link.status = 'confirmed'
    else:
        event.participant_links.append(
            EventParticipant(member_id=event.owner_id, role='owner', status='confirmed')
        )


def add_event_images(event, file_storages):
    for image_file in file_storages:
        stored_name = save_uploaded_image(image_file)
        if stored_name:
            event.images.append(EventImage(filename=stored_name))


class ItemImage(db.Model):
    __tablename__ = 'item_images'
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<ItemImage {self.filename}>'


class LocationImage(db.Model):
    __tablename__ = 'location_images'
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<LocationImage {self.filename}>'


class EventImage(db.Model):
    __tablename__ = 'event_images'
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<EventImage {self.filename}>'

# 初始化数据库并创建默认用户
with app.app_context():
    db.create_all()
    try:
        inspector = inspect(db.engine)
        table_names = inspector.get_table_names()
    except Exception:
        table_names = []
    if 'locations' in table_names:
        existing_cols = {col['name'] for col in inspector.get_columns('locations')}
        alter_statements = []
        if 'latitude' not in existing_cols:
            alter_statements.append('ALTER TABLE locations ADD COLUMN latitude REAL')
        if 'longitude' not in existing_cols:
            alter_statements.append('ALTER TABLE locations ADD COLUMN longitude REAL')
        if 'coordinate_source' not in existing_cols:
            alter_statements.append('ALTER TABLE locations ADD COLUMN coordinate_source VARCHAR(20)')
        if alter_statements:
            with db.engine.begin() as conn:
                for stmt in alter_statements:
                    conn.execute(text(stmt))
    if 'logs' in table_names:
        log_cols = {col['name'] for col in inspector.get_columns('logs')}
        if 'event_id' not in log_cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE logs ADD COLUMN event_id INTEGER'))
    if 'members' in table_names:
        member_cols = {col['name'] for col in inspector.get_columns('members')}
        if 'feedback_log' not in member_cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE members ADD COLUMN feedback_log TEXT'))
    if 'events' in table_names:
        event_cols = {col['name'] for col in inspector.get_columns('events')}
        if 'feedback_log' not in event_cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE events ADD COLUMN feedback_log TEXT'))
    if Member.query.count() == 0:
        default_user = Member(name="Admin User", username="admin", contact="admin@example.com", notes="Default admin user")
        default_user.set_password("admin")
        db.session.add(default_user)
        db.session.commit()

@login_manager.user_loader
def load_user(user_id):
    if not user_id:
        return None
    try:
        return db.session.get(Member, int(user_id))
    except (TypeError, ValueError):
        return None

# 路由定义
@app.route('/')
def index():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    center_member = Member.query.options(
        selectinload(Member.following),
        selectinload(Member.followers),
        selectinload(Member.items).selectinload(Item.locations),
        selectinload(Member.responsible_locations)
    ).filter_by(id=current_user.id).first()
    if not center_member:
        abort(404)

    graph_payload = build_lab_universe_graph(center_member)
    graph_json = json.dumps(graph_payload, ensure_ascii=False)

    return render_template('index.html', graph_json=graph_json)

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


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

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


@app.route('/events')
@login_required
def events_overview():
    events_query = Event.query.options(
        selectinload(Event.owner),
        selectinload(Event.items),
        selectinload(Event.locations),
        selectinload(Event.participant_links).selectinload(EventParticipant.member)
    )
    accessible_events = events_query.filter(
        or_(
            Event.visibility == 'public',
            Event.owner_id == current_user.id,
            Event.participant_links.any(EventParticipant.member_id == current_user.id)
        )
    ).order_by(Event.start_time.asc(), Event.created_at.desc()).all()
    now = datetime.utcnow()
    upcoming_events = []
    past_events = []
    for event in accessible_events:
        if event.start_time and event.start_time < now:
            past_events.append(event)
        else:
            upcoming_events.append(event)
    return render_template('events.html', events=accessible_events, now=now, upcoming_events=upcoming_events, past_events=past_events)


def _event_form_choices():
    members = Member.query.order_by(Member.name).all()
    items = Item.query.order_by(Item.name).all()
    locations = Location.query.order_by(Location.name).all()
    return members, items, locations


def _collect_selected_ids(raw_list):
    selected = set()
    for raw in raw_list:
        try:
            selected.add(int(raw))
        except (TypeError, ValueError):
            continue
    return selected


def build_lab_universe_graph(center_member):
    """Build a universe graph centered on the given member."""
    nodes = {}
    links = []

    def add_node(node_id, label, node_type, meta=None):
        if node_id in nodes:
            if meta:
                nodes[node_id]['meta'].update(meta)
            return nodes[node_id]
        nodes[node_id] = {
            'id': node_id,
            'label': label,
            'type': node_type,
            'meta': meta or {}
        }
        return nodes[node_id]

    center_id = f'member-{center_member.id}'
    add_node(
        center_id,
        center_member.name or center_member.username,
        'member',
        {
            'username': center_member.username,
            'contact': center_member.contact,
            'isCenter': True
        }
    )

    # Follow relationships (outgoing)
    for followed in getattr(center_member, 'following', []) or []:
        target_id = f'member-{followed.id}'
        add_node(
            target_id,
            followed.name or followed.username,
            'member',
            {
                'username': followed.username,
                'contact': followed.contact,
                'relation': 'following'
            }
        )
        links.append({
            'source': center_id,
            'target': target_id,
            'type': 'follows'
        })

    # Followers (incoming)
    for follower in getattr(center_member, 'followers', []) or []:
        source_id = f'member-{follower.id}'
        add_node(
            source_id,
            follower.name or follower.username,
            'member',
            {
                'username': follower.username,
                'contact': follower.contact,
                'relation': 'follower'
            }
        )
        links.append({
            'source': source_id,
            'target': center_id,
            'type': 'follows'
        })

    # Items the member is responsible for
    for item in getattr(center_member, 'items', []) or []:
        item_id = f'item-{item.id}'
        add_node(
            item_id,
            item.name,
            'item',
            {
                'category': item.category,
                'stockStatus': item.stock_status
            }
        )
        links.append({
            'source': center_id,
            'target': item_id,
            'type': 'responsible_for'
        })
        for loc in getattr(item, 'locations', []) or []:
            loc_id = f'location-{loc.id}'
            add_node(loc_id, loc.name, 'location')
            links.append({
                'source': item_id,
                'target': loc_id,
                'type': 'stored_at'
            })

    # Locations the member is directly responsible for
    for loc in getattr(center_member, 'responsible_locations', []) or []:
        loc_id = f'location-{loc.id}'
        add_node(loc_id, loc.name, 'location')
        links.append({
            'source': center_id,
            'target': loc_id,
            'type': 'manages'
        })

    # Events the member owns or participates in
    events = Event.query.options(
        selectinload(Event.owner),
        selectinload(Event.items),
        selectinload(Event.locations),
        selectinload(Event.participant_links).selectinload(EventParticipant.member)
    ).filter(
        or_(
            Event.owner_id == center_member.id,
            Event.participant_links.any(EventParticipant.member_id == center_member.id)
        )
    ).all()

    for event in events:
        event_id = f'event-{event.id}'
        add_node(
            event_id,
            event.title,
            'event',
            {
                'startTime': event.start_time.isoformat() if event.start_time else None,
                'visibility': event.visibility
            }
        )
        if event.owner:
            owner_id = f'member-{event.owner.id}'
            add_node(
                owner_id,
                event.owner.name or event.owner.username,
                'member',
                {'username': event.owner.username, 'relation': 'event_owner'}
            )
            links.append({
                'source': owner_id,
                'target': event_id,
                'type': 'owns_event'
            })
        for link in event.participant_links:
            participant = link.member
            if not participant:
                continue
            participant_id = f'member-{participant.id}'
            add_node(
                participant_id,
                participant.name or participant.username,
                'member',
                {'username': participant.username, 'relation': 'event_participant'}
            )
            links.append({
                'source': event_id,
                'target': participant_id,
                'type': f'participant_{link.role}'
            })
        for item in event.items:
            item_id = f'item-{item.id}'
            add_node(
                item_id,
                item.name,
                'item',
                {
                    'category': item.category,
                    'stockStatus': item.stock_status
                }
            )
            links.append({
                'source': event_id,
                'target': item_id,
                'type': 'event_item'
            })
        for loc in event.locations:
            loc_id = f'location-{loc.id}'
            add_node(loc_id, loc.name, 'location')
            links.append({
                'source': event_id,
                'target': loc_id,
                'type': 'event_location'
            })

    return {
        'nodes': list(nodes.values()),
        'links': links
    }


def _load_event_for_edit(event_id):
    return Event.query.options(
        selectinload(Event.owner),
        selectinload(Event.items),
        selectinload(Event.locations),
        selectinload(Event.participant_links).selectinload(EventParticipant.member)
    ).get_or_404(event_id)


@app.route('/events/add', methods=['GET', 'POST'])
@login_required
def add_event():
    members, items, locations = _event_form_choices()
    default_state = {
        'title': '',
        'description': '',
        'visibility': 'personal',
        'start_time': '',
        'end_time': '',
        'item_ids': [],
        'location_ids': [],
        'participant_ids': [],
        'detail_link': '',
        'external_image_urls': ''
    }
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        description = request.form.get('description') or ''
        visibility = request.form.get('visibility', 'personal')
        if visibility not in {'personal', 'internal', 'public'}:
            visibility = 'personal'
        start_raw = request.form.get('start_time')
        end_raw = request.form.get('end_time')
        start_time = parse_datetime_local(start_raw)
        end_time = parse_datetime_local(end_raw)
        item_ids = _collect_selected_ids(request.form.getlist('item_ids'))
        location_ids = _collect_selected_ids(request.form.getlist('location_ids'))
        participant_ids = _collect_selected_ids(request.form.getlist('participant_ids'))
        detail_link = (request.form.get('detail_link') or '').strip()

        available_member_ids = {member.id for member in members}
        participant_ids = {pid for pid in participant_ids if pid in available_member_ids and pid != current_user.id}

        errors = []
        if not title:
            errors.append('事项标题不能为空')
        if start_time and end_time and end_time < start_time:
            errors.append('结束时间不能早于开始时间')
        if visibility == 'internal' and not participant_ids:
            errors.append('内部事项需要至少选择一名参与人员')

        form_state = {
            'title': title,
            'description': description,
            'visibility': visibility,
            'start_time': start_raw or '',
            'end_time': end_raw or '',
            'item_ids': list(item_ids),
            'location_ids': list(location_ids),
            'participant_ids': list(participant_ids),
            'detail_link': detail_link,
            'external_image_urls': request.form.get('external_event_image_urls', '')
        }

        if errors:
            for msg in errors:
                flash(msg, 'danger')
            return render_template('event_form.html', event=None, members=members, items=items, locations=locations, form_state=form_state)

        event = Event(
            title=title,
            description=description,
            visibility=visibility,
            owner_id=current_user.id,
            start_time=start_time,
            end_time=end_time,
            detail_link=detail_link or None
        )
        db.session.add(event)

        ensure_owner_participation(event)

        if visibility in {'internal', 'public'}:
            for pid in participant_ids:
                event.participant_links.append(EventParticipant(member_id=pid, role='participant', status='confirmed'))

        selected_items = Item.query.filter(Item.id.in_(item_ids)).all() if item_ids else []
        selected_locations = Location.query.filter(Location.id.in_(location_ids)).all() if location_ids else []
        event.items = selected_items
        event.locations = selected_locations

        uploaded_event_files = request.files.getlist('event_images')
        cleaned_files = [f for f in uploaded_event_files if f and getattr(f, 'filename', '')]
        if cleaned_files:
            add_event_images(event, cleaned_files)
        external_urls = _extract_external_urls(request.form.get('external_event_image_urls'))
        if external_urls:
            existing_refs = {img.filename for img in event.images}
            for url in external_urls:
                if url not in existing_refs:
                    event.images.append(EventImage(filename=url))
                    existing_refs.add(url)
        event.touch()
        db.session.commit()

        missing_items, missing_locations = compute_missing_resources(event)
        if missing_items:
            flash('事项内容提到了以下物品但未在“所需物品”中选择：' + '、'.join(item.name for item in missing_items), 'warning')
        if missing_locations:
            flash('事项内容提到了以下位置但未在“活动地点”中选择：' + '、'.join(loc.name for loc in missing_locations), 'warning')

        log = Log(
            user_id=current_user.id,
            event_id=event.id,
            action_type="新增事项",
            details=f"Created event {event.title} (visibility={event.visibility})"
        )
        db.session.add(log)
        db.session.commit()

        flash('事项已创建', 'success')
        return redirect(url_for('event_detail', event_id=event.id))

    return render_template('event_form.html', event=None, members=members, items=items, locations=locations, form_state=default_state)


@app.route('/events/<int:event_id>')
@login_required
def event_detail(event_id):
    event = _load_event_for_edit(event_id)
    if not event.can_view(current_user):
        abort(403)

    participant_links = sorted(
        event.participant_links,
        key=lambda link: (
            0 if link.role == 'owner' else 1,
            (link.member.name or link.member.username) if link.member else '',
            link.joined_at or datetime.utcnow()
        )
    )
    linked_description, missing_items, missing_locations = build_event_view_model(event)
    allow_join = event.can_join(current_user)
    member_index, mention_lookup = build_member_lookup()
    feedback_entries = prepare_feedback_entries(event.feedback_log, member_index, mention_lookup)
    return render_template(
        'event_detail.html',
        event=event,
        linked_description=linked_description,
        participant_links=participant_links,
        missing_items=missing_items,
        missing_locations=missing_locations,
        allow_join=allow_join,
        feedback_entries=feedback_entries,
        feedback_post_url=url_for('post_event_feedback', event_id=event.id)
    )


@app.route('/events/<int:event_id>/feedback', methods=['POST'])
@login_required
def post_event_feedback(event_id):
    event = _load_event_for_edit(event_id)
    if not event.can_view(current_user):
        abort(403)
    content = request.form.get('content', '')
    if content and content.strip():
        append_feedback_entry(event, current_user, content.strip())
        event.touch()
        db.session.commit()
        flash('留言已发布', 'success')
    else:
        flash('留言内容不能为空', 'warning')
    return redirect(url_for('event_detail', event_id=event_id))


@app.route('/events/<int:event_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_event(event_id):
    event = _load_event_for_edit(event_id)
    if not event.can_edit(current_user):
        abort(403)

    members, items, locations = _event_form_choices()

    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        description = request.form.get('description') or ''
        visibility = request.form.get('visibility', event.visibility)
        if visibility not in {'personal', 'internal', 'public'}:
            visibility = event.visibility
        start_raw = request.form.get('start_time')
        end_raw = request.form.get('end_time')
        start_time = parse_datetime_local(start_raw)
        end_time = parse_datetime_local(end_raw)
        item_ids = _collect_selected_ids(request.form.getlist('item_ids'))
        location_ids = _collect_selected_ids(request.form.getlist('location_ids'))
        participant_ids = _collect_selected_ids(request.form.getlist('participant_ids'))
        detail_link = (request.form.get('detail_link') or '').strip()

        available_member_ids = {member.id for member in members}
        participant_ids = {pid for pid in participant_ids if pid in available_member_ids and pid != current_user.id}

        errors = []
        if not title:
            errors.append('事项标题不能为空')
        if start_time and end_time and end_time < start_time:
            errors.append('结束时间不能早于开始时间')
        if visibility == 'internal' and not participant_ids:
            errors.append('内部事项需要至少选择一名参与人员')

        form_state = {
            'title': title,
            'description': description,
            'visibility': visibility,
            'start_time': start_raw or '',
            'end_time': end_raw or '',
            'item_ids': list(item_ids),
            'location_ids': list(location_ids),
            'participant_ids': list(participant_ids),
            'detail_link': detail_link,
            'external_image_urls': request.form.get('external_event_image_urls', '')
        }

        if errors:
            for msg in errors:
                flash(msg, 'danger')
            return render_template('event_form.html', event=event, members=members, items=items, locations=locations, form_state=form_state)

        event.title = title
        event.description = description
        event.visibility = visibility
        event.start_time = start_time
        event.end_time = end_time
        event.detail_link = detail_link or None

        current_links = {link.member_id: link for link in event.participant_links}
        desired_ids = participant_ids if visibility in {'internal', 'public'} else set()

        for member_id, link in list(current_links.items()):
            if member_id == event.owner_id:
                continue
            if member_id not in desired_ids:
                db.session.delete(link)

        if visibility in {'internal', 'public'}:
            for pid in desired_ids:
                if pid not in current_links:
                    event.participant_links.append(EventParticipant(member_id=pid, role='participant', status='confirmed'))

        ensure_owner_participation(event)

        selected_items = Item.query.filter(Item.id.in_(item_ids)).all() if item_ids else []
        selected_locations = Location.query.filter(Location.id.in_(location_ids)).all() if location_ids else []
        event.items = selected_items
        event.locations = selected_locations

        remove_image_ids_raw = request.form.getlist('remove_event_image_ids')
        remove_image_ids = {int(x) for x in remove_image_ids_raw if x.isdigit()}
        if remove_image_ids:
            for img in list(event.images):
                if img.id in remove_image_ids:
                    remove_uploaded_file(img.filename)
                    event.images.remove(img)
                    db.session.delete(img)

        uploaded_event_files = request.files.getlist('event_images')
        cleaned_files = [f for f in uploaded_event_files if f and getattr(f, 'filename', '')]
        if cleaned_files:
            add_event_images(event, cleaned_files)
        external_urls = _extract_external_urls(request.form.get('external_event_image_urls'))
        if external_urls:
            existing_refs = {img.filename for img in event.images}
            for url in external_urls:
                if url not in existing_refs:
                    event.images.append(EventImage(filename=url))
                    existing_refs.add(url)

        event.touch()
        db.session.commit()

        missing_items, missing_locations = compute_missing_resources(event)
        if missing_items:
            flash('事项内容提到了以下物品但未在“所需物品”中选择：' + '、'.join(item.name for item in missing_items), 'warning')
        if missing_locations:
            flash('事项内容提到了以下位置但未在“活动地点”中选择：' + '、'.join(loc.name for loc in missing_locations), 'warning')

        log = Log(
            user_id=current_user.id,
            event_id=event.id,
            action_type="修改事项",
            details=f"Updated event {event.title}"
        )
        db.session.add(log)
        db.session.commit()

        flash('事项已更新', 'success')
        return redirect(url_for('event_detail', event_id=event.id))

    form_state = {
        'title': event.title,
        'description': event.description or '',
        'visibility': event.visibility,
        'start_time': format_datetime_local(event.start_time),
        'end_time': format_datetime_local(event.end_time),
        'item_ids': [item.id for item in event.items],
        'location_ids': [loc.id for loc in event.locations],
        'participant_ids': [link.member_id for link in event.participant_links if link.member_id != event.owner_id],
        'detail_link': event.detail_link or '',
        'external_image_urls': ''
    }
    return render_template('event_form.html', event=event, members=members, items=items, locations=locations, form_state=form_state)


@app.route('/events/<int:event_id>/delete', methods=['POST'])
@login_required
def delete_event(event_id):
    event = Event.query.get_or_404(event_id)
    if not event.can_edit(current_user):
        abort(403)
    event_title = event.title
    event_identifier = event.id
    for img in list(event.images):
        remove_uploaded_file(img.filename)
    db.session.delete(event)
    db.session.commit()
    log = Log(
        user_id=current_user.id,
        event_id=event_identifier,
        action_type="删除事项",
        details=f"Deleted event {event_title}"
    )
    db.session.add(log)
    db.session.commit()
    flash('事项已删除', 'info')
    return redirect(url_for('events_overview'))


@app.route('/events/<int:event_id>/signup', methods=['POST'])
@login_required
def signup_event(event_id):
    event = _load_event_for_edit(event_id)
    if not event.can_join(current_user):
        flash('无法报名该事项', 'warning')
        return redirect(url_for('event_detail', event_id=event.id))
    event.participant_links.append(EventParticipant(member_id=current_user.id, role='participant', status='confirmed'))
    event.touch()
    db.session.commit()
    log = Log(
        user_id=current_user.id,
        event_id=event.id,
        action_type="参加事项",
        details=f"Joined event {event.title}"
    )
    db.session.add(log)
    db.session.commit()
    flash('报名成功，已加入事项', 'success')
    return redirect(url_for('event_detail', event_id=event.id))


@app.route('/events/<int:event_id>/withdraw', methods=['POST'])
@login_required
def withdraw_event(event_id):
    event = _load_event_for_edit(event_id)
    if not event.is_participant(current_user):
        flash('你尚未参与该事项', 'warning')
        return redirect(url_for('event_detail', event_id=event.id))
    if event.owner_id == current_user.id:
        flash('事项负责人不能退出该事项', 'warning')
        return redirect(url_for('event_detail', event_id=event.id))
    removed = False
    for link in list(event.participant_links):
        if link.member_id == current_user.id:
            db.session.delete(link)
            removed = True
    if removed:
        event.touch()
        db.session.commit()
        log = Log(
            user_id=current_user.id,
            event_id=event.id,
            action_type="退出事项",
            details=f"Withdrew from event {event.title}"
        )
        db.session.add(log)
        db.session.commit()
        flash('已退出该事项', 'info')
    return redirect(url_for('event_detail', event_id=event.id))

@app.route('/items')
@login_required
def items():
    items_list = (
        Item.query.options(
            selectinload(Item.locations),
            selectinload(Item.responsible_member)
        )
        .order_by(func.lower(Item.name))
        .all()
    )
    category_map = {}
    uncategorized_bucket = []
    for item in items_list:
        category_name = (item.category or '').strip()
        if category_name:
            category_map.setdefault(category_name, []).append(item)
        else:
            uncategorized_bucket.append(item)
    categories = sorted(category_map.keys(), key=lambda name: name.lower())
    category_payload = []
    for name in categories:
        members = sorted(category_map[name], key=lambda x: x.name.lower())
        category_payload.append({
            'name': name,
            'items': [{'id': it.id, 'name': it.name} for it in members]
        })
    uncategorized_payload = [
        {'id': it.id, 'name': it.name}
        for it in sorted(uncategorized_bucket, key=lambda x: x.name.lower())
    ]
    return render_template(
        'items.html',
        items=items_list,
        categories=categories,
        category_payload=category_payload,
        uncategorized_items=uncategorized_payload
    )

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

        # 图片处理（支持多张）
        uploaded_files = request.files.getlist('images')
        if not uploaded_files:
            fallback_file = request.files.get('image')
            uploaded_files = [fallback_file] if fallback_file else []

        saved_filenames = []
        for image_file in uploaded_files:
            stored_name = save_uploaded_image(image_file)
            if stored_name:
                saved_filenames.append(stored_name)

        external_urls = _extract_external_urls(request.form.get('external_image_urls'))
        primary_image = saved_filenames[0] if saved_filenames else (external_urls[0] if external_urls else None)

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
            image=primary_image
        )
        # 绑定多个位置（若前端未选择则为空列表）
        loc_ids = [int(x) for x in location_ids] if location_ids else []
        if not loc_ids and default_loc_id:
            loc_ids = [default_loc_id]
        if loc_ids:
            new_item.locations = Location.query.filter(Location.id.in_(loc_ids)).all()
        db.session.add(new_item)
        existing_refs = set()
        for fname in saved_filenames:
            if fname not in existing_refs:
                new_item.images.append(ItemImage(filename=fname))
                existing_refs.add(fname)
        for url in external_urls:
            if url not in existing_refs:
                new_item.images.append(ItemImage(filename=url))
                existing_refs.add(url)
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
    categories = [
        c for (c,) in db.session.query(Item.category)
        .filter(Item.category.isnot(None), Item.category != '')
        .distinct()
        .order_by(Item.category)
    ]
    return render_template(
        'item_form.html',
        members=members,
        locations=locations,
        item=None,
        default_loc_id=default_loc_id,
        categories=categories
    )

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

        if item.image and not any(img.filename == item.image for img in item.images):
            item.images.append(ItemImage(filename=item.image))

        # 删除勾选的旧图片
        remove_image_ids_raw = request.form.getlist('remove_image_ids')
        remove_image_ids = {int(x) for x in remove_image_ids_raw if x.isdigit()}
        if remove_image_ids:
            for img in list(item.images):
                if img.id in remove_image_ids:
                    remove_uploaded_file(img.filename)
                    item.images.remove(img)
                    db.session.delete(img)

        remove_primary = request.form.get('remove_primary_image') in {'1', 'on', 'true'}
        if remove_primary and item.image:
            if not any(img.filename == item.image for img in item.images):
                remove_uploaded_file(item.image)
            item.image = None

        # 处理新增上传（支持多张）
        uploaded_files = request.files.getlist('images')
        if not uploaded_files:
            fallback_file = request.files.get('image')
            uploaded_files = [fallback_file] if fallback_file else []
        for image_file in uploaded_files:
            stored_name = save_uploaded_image(image_file)
            if stored_name:
                item.images.append(ItemImage(filename=stored_name))

        external_urls = _extract_external_urls(request.form.get('external_image_urls'))
        if external_urls:
            existing_refs = {img.filename for img in item.images}
            if item.image:
                existing_refs.add(item.image)
            for url in external_urls:
                if url not in existing_refs:
                    item.images.append(ItemImage(filename=url))
                    existing_refs.add(url)

        if item.images:
            item.image = item.images[0].filename
        elif remove_primary:
            item.image = None

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
    categories = [
        c for (c,) in db.session.query(Item.category)
        .filter(Item.category.isnot(None), Item.category != '')
        .distinct()
        .order_by(Item.category)
    ]
    return render_template(
        'item_form.html',
        members=members,
        locations=locations,
        item=item,
        categories=categories
    )

@app.route('/items/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)
    for fname in set(item.image_filenames):
        remove_uploaded_file(fname)
    db.session.delete(item)
    db.session.commit()
    # 记录日志
    log = Log(user_id=current_user.id, item_id=item_id, action_type="删除物品", details=f"Deleted item {item.name}")
    db.session.add(log)
    db.session.commit()
    flash('物品已删除', 'info')
    return redirect(url_for('items'))

@app.route('/items/manage-category', methods=['POST'])
@login_required
def manage_item_category():
    category_name = (request.form.get('category_name') or '').strip()
    if not category_name:
        flash('请输入要管理的类别名称', 'warning')
        return redirect(url_for('items'))

    def parse_ids(key):
        raw = request.form.getlist(key)
        return {int(val) for val in raw if val.isdigit()}

    add_ids = parse_ids('add_item_ids')
    remove_ids = parse_ids('remove_item_ids')

    if not add_ids and not remove_ids:
        flash('未选择任何需要调整的物品', 'info')
        return redirect(url_for('items'))

    added_items = []
    removed_items = []
    now = datetime.utcnow()

    if add_ids:
        candidates = Item.query.filter(Item.id.in_(add_ids)).all()
        for item in candidates:
            if (item.category or '').strip():
                continue
            item.category = category_name
            item.last_modified = now
            added_items.append(item)
            log = Log(
                user_id=current_user.id,
                item_id=item.id,
                action_type="物品类别调整",
                details=f"将物品 {item.name} 归类为 {category_name}"
            )
            db.session.add(log)

    if remove_ids:
        candidates = Item.query.filter(Item.id.in_(remove_ids)).all()
        for item in candidates:
            if (item.category or '').strip() != category_name:
                continue
            item.category = None
            item.last_modified = now
            removed_items.append(item)
            log = Log(
                user_id=current_user.id,
                item_id=item.id,
                action_type="物品类别调整",
                details=f"取消物品 {item.name} 的类别 {category_name}"
            )
            db.session.add(log)

    if not added_items and not removed_items:
        flash('没有物品符合调整条件', 'info')
        return redirect(url_for('items'))

    db.session.commit()
    summary = []
    if added_items:
        summary.append(f"新增 {len(added_items)} 个物品")
    if removed_items:
        summary.append(f"取消 {len(removed_items)} 个物品")
    flash('，'.join(summary) + f' 于类别 {category_name}', 'success')
    return redirect(url_for('items'))

@app.route('/locations')
@login_required
def locations_list():
    locations = Location.query.options(
        # selectinload 用于加载多级 children 避免 N+1 查询问题
        db.selectinload(Location.children).selectinload(Location.children)
    ).order_by(Location.name).all()
    return render_template('locations.html', locations=locations)


@app.route('/api/locations/search')
@login_required
def search_locations():
    keyword = (request.args.get('q') or '').strip()
    if not keyword:
        return jsonify([])
    pattern = f"%{keyword}%"
    matches = (
        Location.query
        .filter(Location.name.ilike(pattern))
        .order_by(func.lower(Location.name))
        .limit(12)
        .all()
    )
    payload = []
    for loc in matches:
        payload.append({
            'id': loc.id,
            'name': loc.name,
            'latitude': loc.latitude,
            'longitude': loc.longitude,
            'detailUrl': url_for('view_location', loc_id=loc.id),
            'hasCoordinates': loc.latitude is not None and loc.longitude is not None
        })
    return jsonify(payload)

@app.route('/api/items/search')
@login_required
def search_items():
    keyword = (request.args.get('q') or '').strip()
    if not keyword:
        return jsonify([])
    like_pattern = f"%{keyword}%"
    keyword_digits = re.sub(r'[^0-9]', '', keyword)
    digit_pattern = f"%{keyword_digits}%" if keyword_digits else None
    cas_col = func.coalesce(Item.cas_no, '')
    cas_no_hyphenless = func.replace(cas_col, '-', '')
    filters = [
        Item.name.ilike(like_pattern),
        Item.notes.ilike(like_pattern),
        Item.cas_no.ilike(like_pattern)
    ]
    if digit_pattern:
        filters.append(cas_no_hyphenless.like(digit_pattern))
    matches = (
        Item.query
        .filter(or_(*filters))
        .order_by(func.lower(Item.name))
        .limit(12)
        .all()
    )
    payload = []
    for item in matches:
        payload.append({
            'id': item.id,
            'name': item.name,
            'category': item.category,
            'detailUrl': url_for('item_detail', item_id=item.id)
        })
    return jsonify(payload)

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
        latitude = _parse_coordinate(request.form.get('latitude'))
        longitude = _parse_coordinate(request.form.get('longitude'))
        coordinate_source = request.form.get('coordinate_source') or None
        if latitude is None or longitude is None:
            latitude = None
            longitude = None
            coordinate_source = None

        uploaded_files = request.files.getlist('images')
        if not uploaded_files:
            fallback_file = request.files.get('image')
            uploaded_files = [fallback_file] if fallback_file else []

        saved_filenames = []
        for image_file in uploaded_files:
            stored_name = save_uploaded_image(image_file)
            if stored_name:
                saved_filenames.append(stored_name)
        external_urls = _extract_external_urls(request.form.get('external_image_urls'))
        primary_image = saved_filenames[0] if saved_filenames else (external_urls[0] if external_urls else None)
        # 先创建 Location
        new_loc = Location(
            name=name,
            parent_id=parent_id if parent_id else None,
            notes=notes,
            image=primary_image,
            clean_status=clean_status,
            detail_link=detail_link,
            latitude=latitude,
            longitude=longitude,
            coordinate_source=coordinate_source
        )
        db.session.add(new_loc)
        existing_refs = set()
        for fname in saved_filenames:
            if fname not in existing_refs:
                new_loc.images.append(LocationImage(filename=fname))
                existing_refs.add(fname)
        for url in external_urls:
            if url not in existing_refs:
                new_loc.images.append(LocationImage(filename=url))
                existing_refs.add(url)
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
        location.clean_status = request.form.get('clean_status') or None
        latitude = _parse_coordinate(request.form.get('latitude'))
        longitude = _parse_coordinate(request.form.get('longitude'))
        coordinate_source = request.form.get('coordinate_source') or None
        if latitude is None or longitude is None:
            latitude = None
            longitude = None
            coordinate_source = None

        if location.image and not any(img.filename == location.image for img in location.images):
            location.images.append(LocationImage(filename=location.image))

        remove_image_ids_raw = request.form.getlist('remove_image_ids')
        remove_image_ids = {int(x) for x in remove_image_ids_raw if x.isdigit()}
        if remove_image_ids:
            for img in list(location.images):
                if img.id in remove_image_ids:
                    remove_uploaded_file(img.filename)
                    location.images.remove(img)
                    db.session.delete(img)

        remove_primary = request.form.get('remove_primary_image') in {'1', 'on', 'true'}
        if remove_primary and location.image:
            if not any(img.filename == location.image for img in location.images):
                remove_uploaded_file(location.image)
            location.image = None

        uploaded_files = request.files.getlist('images')
        if not uploaded_files:
            fallback_file = request.files.get('image')
            uploaded_files = [fallback_file] if fallback_file else []
        for image_file in uploaded_files:
            stored_name = save_uploaded_image(image_file)
            if stored_name:
                location.images.append(LocationImage(filename=stored_name))

        external_urls = _extract_external_urls(request.form.get('external_image_urls'))
        if external_urls:
            existing_refs = {img.filename for img in location.images}
            if location.image:
                existing_refs.add(location.image)
            for url in external_urls:
                if url not in existing_refs:
                    location.images.append(LocationImage(filename=url))
                    existing_refs.add(url)

        if location.images:
            location.image = location.images[0].filename
        elif remove_primary:
            location.image = None
        # 多负责人
        member_objs = []
        if responsible_ids:
            member_objs = Member.query.filter(Member.id.in_([int(mid) for mid in responsible_ids])).all()
        if not member_objs:
            member_objs = [current_user]
        location.responsible_members = member_objs
        location.notes = notes
        location.detail_link = detail_link
        location.latitude = latitude
        location.longitude = longitude
        location.coordinate_source = coordinate_source
        location.last_modified = datetime.utcnow()
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, location_id=location.id, action_type="修改位置", details=f"Edited location {location.name}")
        db.session.add(log)
        db.session.commit()
        flash('位置信息已更新', 'success')
        return redirect(url_for('locations_list'))
    members = Member.query.all()
    parents = Location.query.filter(Location.id != location.id).all()
    return render_template('location_form.html', members=members, location=location, parents=parents)

@app.route('/locations/<int:loc_id>/delete', methods=['POST'])
@login_required
def delete_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    for fname in set(location.image_filenames):
        remove_uploaded_file(fname)
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
    items_at_location = sorted(location.items, key=lambda item: item.name.lower())
    # 分类统计状态标签（如：用完、少量、充足）
    status_counter = Counter()
    category_counter = Counter()
    feature_counter = Counter()
    responsible_counter = Counter()
    responsible_map = {}
    for item in items_at_location:
        if item.stock_status:
            statuses = item.stock_status.split(',')  # 支持多个状态
            for s in statuses:
                label = s.strip()
                if label:
                    status_counter[label] += 1
        if item.category:
            category_counter[item.category.strip()] += 1
        if item.features:
            feature_counter[item.features.strip()] += 1
        if item.responsible_member:
            responsible_counter[item.responsible_member.id] += 1
            responsible_map[item.responsible_member.id] = item.responsible_member

    def _counter_to_stats(counter, limit=None):
        pairs = sorted(counter.items(), key=lambda x: (-x[1], x[0]))
        if limit:
            pairs = pairs[:limit]
        return [{'label': label, 'count': count} for label, count in pairs]

    status_stats = _counter_to_stats(status_counter)
    category_stats = _counter_to_stats(category_counter, limit=8)
    feature_stats = _counter_to_stats(feature_counter, limit=8)
    responsible_stats = []
    for mid, count in sorted(responsible_counter.items(), key=lambda x: (-x[1], (responsible_map[x[0]].name or responsible_map[x[0]].username or '').lower())):
        member = responsible_map[mid]
        responsible_stats.append({'member': member, 'count': count})

    available_items = Item.query.filter(~Item.locations.any(Location.id == location.id)) \
                                .order_by(Item.name.asc()).all()

    return render_template('location.html', location=location, 
                           items=items_at_location,
                           status_counter=status_counter,
                           status_stats=status_stats,
                           category_stats=category_stats,
                           feature_stats=feature_stats,
                           responsible_stats=responsible_stats,
                           available_items=available_items)


@app.route('/locations/<int:loc_id>/items/manage', methods=['POST'])
@login_required
def manage_location_items(loc_id):
    location = Location.query.get_or_404(loc_id)
    action = request.form.get('action')
    if action not in {'add_existing', 'remove'}:
        flash('无效操作类型', 'danger')
        return redirect(url_for('view_location', loc_id=loc_id))

    if action == 'add_existing':
        raw_ids = request.form.getlist('existing_item_ids')
        selected_ids = {int(x) for x in raw_ids if x.isdigit()}
        if not selected_ids:
            flash('请选择要加入的位置物品', 'warning')
            return redirect(url_for('view_location', loc_id=loc_id))
        items_to_add = Item.query.filter(Item.id.in_(selected_ids)).all()
        added_items = []
        for item in items_to_add:
            if location not in item.locations:
                item.locations.append(location)
                added_items.append(item)
        if not added_items:
            flash('所选物品已在该位置中', 'info')
            return redirect(url_for('view_location', loc_id=loc_id))
        now = datetime.utcnow()
        location.last_modified = now
        for item in added_items:
            item.last_modified = now
            log = Log(
                user_id=current_user.id,
                item_id=item.id,
                location_id=location.id,
                action_type="物品关联位置",
                details=f"关联物品 {item.name} 至位置 {location.name}"
            )
            db.session.add(log)
        db.session.commit()
        flash(f"{len(added_items)} 个物品已加入此位置", 'success')
        return redirect(url_for('view_location', loc_id=loc_id))

    # action == 'remove'
    raw_ids = request.form.getlist('remove_item_ids')
    selected_ids = {int(x) for x in raw_ids if x.isdigit()}
    if not selected_ids:
        flash('请选择要移除的物品', 'warning')
        return redirect(url_for('view_location', loc_id=loc_id))
    items_to_remove = Item.query.filter(Item.id.in_(selected_ids)).all()
    removed_items = []
    for item in items_to_remove:
        if location in item.locations:
            item.locations.remove(location)
            removed_items.append(item)
    if not removed_items:
        flash('未移除任何物品', 'info')
        return redirect(url_for('view_location', loc_id=loc_id))
    now = datetime.utcnow()
    location.last_modified = now
    for item in removed_items:
        item.last_modified = now
        log = Log(
            user_id=current_user.id,
            item_id=item.id,
            location_id=location.id,
            action_type="物品移出位置",
            details=f"将物品 {item.name} 从位置 {location.name} 移除"
        )
        db.session.add(log)
    db.session.commit()
    flash(f"{len(removed_items)} 个物品已从该位置移除", 'success')
    return redirect(url_for('view_location', loc_id=loc_id))


@app.route('/members')
@login_required
def members_list():
    members = Member.query.order_by(Member.name).all()
    followed_ids = {mem.id for mem in current_user.following}
    def sort_key(member):
        display_name = (member.name or member.username).lower()
        if member.id == current_user.id:
            group = 0
        elif member.id in followed_ids:
            group = 1
        else:
            group = 2
        return (group, display_name)
    members.sort(key=sort_key)
    return render_template('members.html', members=members, followed_ids=followed_ids)


@app.route('/members/<int:member_id>/toggle_follow', methods=['POST'])
@login_required
def toggle_follow(member_id):
    if member_id == current_user.id:
        return jsonify({'error': '不能关注自己'}), 400
    member = Member.query.get_or_404(member_id)
    if member in current_user.following:
        current_user.following.remove(member)
        followed = False
    else:
        current_user.following.append(member)
        followed = True
    db.session.commit()
    return jsonify({'followed': followed})

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
    items_preview = items_resp[:5]
    items_extra = items_resp[5:]
    locations_preview = locations_resp[:5]
    locations_extra = locations_resp[5:]

    # 相关事项（区分进行中与历史）
    events_upcoming = []
    events_past = []
    event_query = Event.query.options(
        selectinload(Event.participant_links).selectinload(EventParticipant.member),
        selectinload(Event.locations)
    ).filter(
        or_(
            Event.owner_id == member.id,
            Event.participant_links.any(EventParticipant.member_id == member.id)
        )
    ).order_by(Event.start_time.asc(), Event.created_at.desc())
    now = datetime.utcnow()
    for evt in event_query.all():
        if not evt.can_view(current_user):
            continue
        if evt.start_time and evt.start_time < now:
            events_past.append(evt)
        else:
            events_upcoming.append(evt)

    # 通知列表：他人对该成员负责的物品/位置的最近更新
    notifications = []
    if member.id == current_user.id:
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
    member_index, mention_lookup = build_member_lookup()
    profile_notes_html = None
    if member.notes:
        profile_notes_html = render_rich_text(member.notes, mention_lookup)
    feedback_entries = prepare_feedback_entries(member.feedback_log, member_index, mention_lookup)

    # 当前用户自己的操作记录（仅查看自己的主页时显示）
    user_logs = []
    if member.id == current_user.id:
        user_logs = Log.query.filter_by(user_id=member.id).order_by(Log.timestamp.desc()).limit(5).all()
    is_following = False
    if member.id != current_user.id:
        is_following = member in current_user.following

    return render_template('profile.html',
                           profile_user=member,
                           items_resp=items_resp,
                           locations_resp=locations_resp,
                           notifications=notifications,
                           user_logs=user_logs,
                           profile_notes_html=profile_notes_html,
                           feedback_entries=feedback_entries,
                           items_preview=items_preview,
                           items_extra=items_extra,
                           locations_preview=locations_preview,
                           locations_extra=locations_extra,
                           any_item_empty=any_item_empty,
                           any_location_dirty=any_location_dirty,
                           events_upcoming=events_upcoming,
                           events_past=events_past,
                           is_following=is_following)

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
            stored_name = save_uploaded_image(image_file)
            if stored_name:
                remove_uploaded_file(member.photo)
                member.photo = stored_name
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
        append_feedback_entry(receiver, current_user, content.strip())
        receiver.last_modified = datetime.utcnow()
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


@app.context_processor
def inject_image_helpers():
    def resolve_image_url(ref):
        if not ref:
            return None
        if _is_external_image(ref):
            return ref
        return url_for('uploaded_file', filename=ref)

    def item_image_urls(item):
        if not item:
            return []
        urls = []
        for fname in getattr(item, 'image_filenames', []) or []:
            resolved = resolve_image_url(fname)
            if resolved:
                urls.append(resolved)
        return urls

    def location_image_urls(location):
        if not location:
            return []
        urls = []
        for fname in getattr(location, 'image_filenames', []) or []:
            resolved = resolve_image_url(fname)
            if resolved:
                urls.append(resolved)
        return urls

    def uploaded_image_url(filename):
        return resolve_image_url(filename)

    return dict(
        item_image_urls=item_image_urls,
        location_image_urls=location_image_urls,
        uploaded_image_url=uploaded_image_url,
    )

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(
        os.getenv("PORT")
        or os.getenv("FLASK_RUN_PORT")
        or os.getenv("BENSCI_PORT")
        or 5001
    )
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
    print(f"Server is running on the http://{host}:{port}")
