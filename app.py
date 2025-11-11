import os
import glob
from datetime import datetime, timedelta, timezone
import re
import json
from io import BytesIO
import urllib.request
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin
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
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover - runtime guard when Pillow missing
    Image = ImageDraw = ImageFont = ImageOps = None

try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_Q
except ImportError:  # pragma: no cover - runtime guard when qrcode missing
    qrcode = None
    ERROR_CORRECT_Q = None

try:
    import oss2
except ImportError:
    oss2 = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None


app = Flask(__name__)
# Behind reverse proxy (e.g., Nginx) fix: trust X-Forwarded-* headers
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# 优先从环境变量读取，生产环境请设置强随机串：export FLASK_SECRET_KEY='...'
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev-only-change-me')
# 记住登录状态的 cookie 时长（天）
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
# 注意：SQLite 在多进程/并发写入下会锁表；生产建议换 PostgreSQL。
# 这里为提高并发容忍度，加入超时参数；并在 engine options 中关闭线程检查。
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///lab.db?timeout=30'
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'connect_args': {'check_same_thread': False}
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['EVENT_SHARE_TOKEN_MAX_AGE'] = int(os.getenv('EVENT_SHARE_TOKEN_MAX_AGE', 60 * 60 * 24 * 30))
app.config['EVENT_SHARE_TOKEN_SALT'] = os.getenv('EVENT_SHARE_TOKEN_SALT', 'benlab-event-share')
public_base = (os.getenv('PUBLIC_BASE_URL') or '').strip()
app.config['PUBLIC_BASE_URL'] = public_base.rstrip('/') if public_base else None
app.config['PREFERRED_URL_SCHEME'] = os.getenv('PREFERRED_URL_SCHEME', 'https')
# 使用绝对路径，避免工作目录变化导致保存失败
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'static', 'images')
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'images')
app.config['MAX_CONTENT_LENGTH'] = 2500 * 1024 * 1024  # 限制上传2500MB以内的文件
OSS_ENDPOINT = os.getenv('ALIYUN_OSS_ENDPOINT')
OSS_ACCESS_KEY_ID = os.getenv('ALIYUN_OSS_ACCESS_KEY_ID')
OSS_ACCESS_KEY_SECRET = os.getenv('ALIYUN_OSS_ACCESS_KEY_SECRET')
OSS_BUCKET_NAME = os.getenv('ALIYUN_OSS_BUCKET')
OSS_PREFIX = (os.getenv('ALIYUN_OSS_PREFIX') or '').strip('/ ')
OSS_PUBLIC_BASE_URL = (os.getenv('ALIYUN_OSS_PUBLIC_BASE_URL') or '').rstrip('/')
MEDIA_STORAGE_MODE = (os.getenv('MEDIA_STORAGE_MODE') or 'auto').strip().lower()
if MEDIA_STORAGE_MODE not in {'auto', 'oss', 'local'}:
    MEDIA_STORAGE_MODE = 'auto'
app.config['MEDIA_STORAGE_MODE'] = MEDIA_STORAGE_MODE
_oss_env_ready = bool(OSS_ENDPOINT and OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET and OSS_BUCKET_NAME)
_can_use_oss = bool(oss2 and _oss_env_ready)
if MEDIA_STORAGE_MODE == 'oss':
    if not _can_use_oss:
        raise RuntimeError(
            'MEDIA_STORAGE_MODE=oss 但 OSS 配置或 oss2 依赖缺失，请检查环境变量与依赖安装。'
        )
    USE_OSS = True
elif MEDIA_STORAGE_MODE == 'local':
    USE_OSS = False
else:
    USE_OSS = _can_use_oss
app.config['USE_OSS'] = USE_OSS
if USE_OSS:
    app.config['OSS_ENDPOINT'] = OSS_ENDPOINT
    app.config['OSS_ACCESS_KEY_ID'] = OSS_ACCESS_KEY_ID
    app.config['OSS_ACCESS_KEY_SECRET'] = OSS_ACCESS_KEY_SECRET
    app.config['OSS_BUCKET'] = OSS_BUCKET_NAME
app.config['OSS_PREFIX'] = OSS_PREFIX
app.config['OSS_PUBLIC_BASE_URL'] = OSS_PUBLIC_BASE_URL
_direct_upload_flag = os.getenv('ENABLE_DIRECT_OSS_UPLOAD')
if _direct_upload_flag is None:
    _enable_direct_upload = True
else:
    _enable_direct_upload = _direct_upload_flag.strip().lower() not in {'0', 'false', 'no', 'off'}
try:
    direct_upload_expiration = int(os.getenv('DIRECT_UPLOAD_URL_EXPIRATION', '900'))
except (TypeError, ValueError):
    direct_upload_expiration = 900
app.config['DIRECT_OSS_UPLOAD_ENABLED'] = bool(USE_OSS and _enable_direct_upload)
app.config['DIRECT_UPLOAD_URL_EXPIRATION'] = max(60, direct_upload_expiration)
REMOTE_FIELD_SUFFIX = '_remote_keys'
_oss_bucket = None

UTC = timezone.utc
if ZoneInfo:
    try:
        CHINA_TZ = ZoneInfo('Asia/Shanghai')
    except Exception:  # pragma: no cover - fallback if zoneinfo data missing
        CHINA_TZ = timezone(timedelta(hours=8))
else:  # pragma: no cover - Python < 3.9 fallback
    CHINA_TZ = timezone(timedelta(hours=8))


def _as_utc(value):
    """Return a timezone-aware datetime in UTC."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            sanitized = value.strip()
            if sanitized.endswith('Z'):
                sanitized = sanitized[:-1] + '+00:00'
            value = datetime.fromisoformat(sanitized)
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_china_timezone(value):
    """Convert a datetime (string/naive/aware) to Asia/Shanghai."""
    dt = _as_utc(value)
    if not dt:
        return None
    return dt.astimezone(CHINA_TZ)


def format_china_time(value, fmt='%Y-%m-%d %H:%M'):
    """Format datetime in Asia/Shanghai timezone, fallback to empty string."""
    dt = to_china_timezone(value)
    return dt.strftime(fmt) if dt else ''


app.jinja_env.filters['china_time'] = format_china_time

# Poster/QR generation helpers -------------------------------------------------
FONT_SEARCH_DIRECTORIES = [
    os.path.join(BASE_DIR, 'static', 'fonts'),
    os.path.join(BASE_DIR, 'fonts'),
    '/System/Library/Fonts',
    '/System/Library/Fonts/Supplemental',
    '/Library/Fonts',
    '/usr/share/fonts',
    '/usr/share/fonts/truetype',
    '/usr/share/fonts/opentype',
    '/usr/local/share/fonts'
]

FONT_REGULAR_CANDIDATES = [
    'LXGWWenKaiLite-Regular.ttf',
    'PingFang.ttc',
    'PingFang SC.ttc',
    'SourceHanSansSC-Regular.otf',
    'SourceHanSansCN-Regular.otf',
    'NotoSansCJK-Regular.ttc',
    'NotoSansSC-Regular.otf',
    'NotoSans-Regular.ttf',
    'HarmonyOS_Sans_SC_Regular.ttf',
    'Alibaba-PuHuiTi-Regular.ttf',
    'AlibabaPuHuiTi-2-55-Regular.ttf',
    'AlibabaSans-Regular.otf',
    'Arial Unicode.ttf',
    'ArialUnicode.ttf',
    'Arial.ttf',
    'Helvetica.ttc',
    'DejaVuSans.ttf',
    'FreeSans.ttf'
]

FONT_BOLD_CANDIDATES = [
    'LXGWWenKaiLite-Bold.ttf',
    'PingFang.ttc',
    'PingFang SC.ttc',
    'SourceHanSansSC-Bold.otf',
    'SourceHanSansCN-Bold.otf',
    'NotoSansCJK-Bold.ttc',
    'NotoSans-Bold.ttf',
    'HarmonyOS_Sans_SC_Bold.ttf',
    'AlibabaPuHuiTi-2-75-SemiBold.ttf',
    'AlibabaSans-Bold.otf',
    'Arial Bold.ttf',
    'Arial-Bold.ttf',
    'Arialbd.ttf',
    'Helvetica.ttc',
    'DejaVuSans-Bold.ttf',
    'FreeSansBold.ttf'
]


def _resolve_font_path(candidates):
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        for directory in FONT_SEARCH_DIRECTORIES:
            if not directory or not os.path.isdir(directory):
                continue
            path = os.path.join(directory, candidate)
            if os.path.exists(path):
                return path
            matches = glob.glob(os.path.join(directory, candidate))
            if matches:
                return matches[0]
    return None


def _load_font(size, bold=False):
    if ImageFont is None:
        return None
    path = _resolve_font_path(FONT_BOLD_CANDIDATES if bold else FONT_REGULAR_CANDIDATES)
    if path:
        try:
            layout_engine = getattr(ImageFont, 'LAYOUT_RAQM', None)
            if layout_engine is not None:
                return ImageFont.truetype(path, size, layout_engine=layout_engine)
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _poster_resample_filter():
    if Image is None:
        return None
    resampling = getattr(Image, 'Resampling', None)
    if resampling:
        return getattr(resampling, 'LANCZOS', getattr(resampling, 'BICUBIC', None))
    return getattr(Image, 'LANCZOS', getattr(Image, 'BICUBIC', getattr(Image, 'ANTIALIAS', None)))


def _text_width(draw_ctx, text, font):
    if hasattr(draw_ctx, 'textlength'):
        return draw_ctx.textlength(text, font=font)
    bbox = draw_ctx.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _font_line_height(font):
    if hasattr(font, 'getmetrics'):
        ascent, descent = font.getmetrics()
        return ascent + descent
    if hasattr(font, 'size') and isinstance(font.size, (int, float)):
        return int(font.size)
    return 32


def _wrap_text(draw_ctx, text, font, max_width, max_lines=None):
    if not text:
        return []
    sanitized = text.replace('\r', '')
    lines = []
    current = ''
    truncated = False
    for char in sanitized:
        if char == '\n':
            lines.append(current)
            current = ''
            continue
        candidate = f"{current}{char}"
        if current and _text_width(draw_ctx, candidate, font) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
        if max_lines and len(lines) >= max_lines:
            truncated = True
            current = ''
            break
    if current:
        if not max_lines or len(lines) < max_lines:
            lines.append(current)
        else:
            truncated = True
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    if truncated and lines:
        last = lines[-1].rstrip(' …')
        ellipsis = '…'
        while last and _text_width(draw_ctx, f"{last}{ellipsis}", font) > max_width:
            last = last[:-1]
        lines[-1] = (last or '') + ellipsis
    return lines


def _format_event_time_range(event):
    start = event.start_time
    end = event.end_time
    if start and end:
        if start.date() == end.date():
            return f"{start.strftime('%Y-%m-%d %H:%M')} - {end.strftime('%H:%M')}"
        return f"{start.strftime('%Y-%m-%d %H:%M')} - {end.strftime('%Y-%m-%d %H:%M')}"
    if start:
        return start.strftime('%Y-%m-%d %H:%M')
    return '时间待定'


def _load_event_cover_image(event, target_size):
    if Image is None:
        return None
    image_entry = next((img for img in (event.images or []) if img.filename), None)
    if not image_entry:
        return None
    filename = image_entry.filename
    data = None
    try:
        if _is_external_media(filename):
            with urllib.request.urlopen(filename, timeout=5) as resp:
                data = resp.read()
        elif app.config.get('USE_OSS'):
            url = _build_oss_url(filename)
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = resp.read()
        else:
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.exists(path):
                with open(path, 'rb') as fh:
                    data = fh.read()
    except (URLError, HTTPError, OSError):
        data = None
    if not data:
        return None
    try:
        image = Image.open(BytesIO(data)).convert('RGB')
    except (OSError, ValueError):
        return None
    if target_size:
        resample = _poster_resample_filter() or Image.BICUBIC
        try:
            image = ImageOps.fit(image, target_size, method=resample, centering=(0.5, 0.5))
        except Exception:
            image = image.resize(target_size, resample=resample)
    return image


def _build_qr_image(data, box_size=10):
    if not qrcode:
        return None
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_Q or qrcode.constants.ERROR_CORRECT_Q,
        box_size=box_size,
        border=2
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill_color='#202020', back_color='white').convert('RGB')


def _event_share_serializer():
    secret = app.config['SECRET_KEY']
    salt = app.config.get('EVENT_SHARE_TOKEN_SALT', 'benlab-event-share')
    return URLSafeTimedSerializer(secret_key=secret, salt=salt)


def generate_event_share_token(event):
    serializer = _event_share_serializer()
    issued_at = int(datetime.utcnow().timestamp())
    payload = {
        'event_id': event.id,
        'visibility': event.visibility,
        'owner_id': event.owner_id,
        'issued_at': issued_at
    }
    return serializer.dumps(payload)


def verify_event_share_token(token):
    serializer = _event_share_serializer()
    max_age = app.config.get('EVENT_SHARE_TOKEN_MAX_AGE')
    try:
        return serializer.loads(token, max_age=max_age), None
    except SignatureExpired:
        return None, 'expired'
    except BadSignature:
        return None, 'invalid'


def build_event_share_url(event):
    token = generate_event_share_token(event)
    public_base = app.config.get('PUBLIC_BASE_URL')
    if public_base:
        relative = url_for('event_share_entry', event_id=event.id, token=token)
        normalized = public_base.rstrip('/') + '/'
        return urljoin(normalized, relative.lstrip('/'))
    return url_for('event_share_entry', event_id=event.id, token=token, _external=True)


def describe_duration(seconds):
    if not seconds:
        return '长期有效'
    units = [
        (60 * 60 * 24, '天'),
        (60 * 60, '小时'),
        (60, '分钟')
    ]
    parts = []
    remaining = int(seconds)
    for span, label in units:
        if remaining >= span:
            value = remaining // span
            remaining -= value * span
            parts.append(f'{value}{label}')
            if len(parts) == 2:
                break
    if not parts:
        return f'{remaining}秒'
    return ''.join(parts)


def build_event_share_metadata(event):
    if not event or event.visibility == 'personal':
        return None
    url = build_event_share_url(event)
    max_age = app.config.get('EVENT_SHARE_TOKEN_MAX_AGE')
    issued_at = datetime.utcnow()
    expires_at = issued_at + timedelta(seconds=max_age) if max_age else None
    return {
        'url': url,
        'issued_at': issued_at,
        'expires_at': expires_at,
        'max_age_seconds': max_age,
        'validity_hint': describe_duration(max_age),
        'base_override': app.config.get('PUBLIC_BASE_URL')
    }


def generate_event_share_poster(event, detail_url, validity_hint=None):
    if not (Image and ImageDraw and ImageFont and qrcode):
        raise RuntimeError('海报生成依赖 Pillow 和 qrcode，请先安装相关依赖。')

    poster_width, poster_height = 1080, 1600
    outer_margin = 64
    card_radius = 48
    accent_color = (94, 110, 255)
    background_color = (245, 246, 250)

    base = Image.new('RGB', (poster_width, poster_height), background_color)
    card_width = poster_width - outer_margin * 2
    card_height = poster_height - outer_margin * 2
    card = Image.new('RGB', (card_width, card_height), 'white')
    card_draw = ImageDraw.Draw(card)

    cover_height = 420
    cover = _load_event_cover_image(event, (card_width, cover_height))
    if cover:
        card.paste(cover, (0, 0))
        overlay = Image.new('RGBA', (card_width, cover_height), (0, 0, 0, 70))
        card.paste(overlay, (0, 0), overlay)
    else:
        top_start = (86, 125, 255)
        top_end = (137, 84, 255)
        for y in range(cover_height):
            ratio = y / max(cover_height - 1, 1)
            color = tuple(
                int(top_start[idx] * (1 - ratio) + top_end[idx] * ratio)
                for idx in range(3)
            )
            card_draw.line([(0, y), (card_width, y)], fill=color)

    content_x = 64
    content_y = cover_height + 56
    max_text_width = card_width - content_x * 2

    title_font = _load_font(64, bold=True) or ImageFont.load_default()
    info_font = _load_font(32) or ImageFont.load_default()
    section_font = _load_font(36, bold=True) or ImageFont.load_default()
    body_font = _load_font(30) or ImageFont.load_default()
    hint_font = _load_font(26) or ImageFont.load_default()

    loaded_fonts = [title_font, info_font, section_font, body_font, hint_font]
    if any(not isinstance(getattr(font, 'path', None), str) or not os.path.exists(font.path) for font in loaded_fonts if font is not None):
        raise RuntimeError('未找到可用的中文字体，请在 static/fonts 目录放置支持中文的字体文件或在系统层面安装后重试。')

    title_lines = _wrap_text(card_draw, event.title or '活动', title_font, max_text_width, max_lines=2)
    for line in title_lines:
        card_draw.text((content_x, content_y), line, font=title_font, fill=(33, 33, 33))
        content_y += _font_line_height(title_font) + 6

    content_y += 12
    time_text = _format_event_time_range(event)
    location_text = '、'.join(loc.name for loc in event.locations if loc.name) or '地点待定'
    owner_text = (event.owner.name if event.owner and event.owner.name else (event.owner.username if event.owner else '负责人待定'))

    info_lines = [
        f"时间：{time_text}",
        f"地点：{location_text}",
        f"联系人：{owner_text}"
    ]
    for line in info_lines:
        card_draw.text((content_x, content_y), line, font=info_font, fill=(74, 74, 74))
        content_y += _font_line_height(info_font) + 4

    content_y += 24
    card_draw.text((content_x, content_y), '活动亮点', font=section_font, fill=accent_color)
    content_y += _font_line_height(section_font) + 12

    description = (event.description or '').strip()
    if not description:
        description = '扫码了解事项详情，和更多伙伴一起准备与参与。'
    highlight_lines = _wrap_text(card_draw, description, body_font, max_text_width, max_lines=6)
    for line in highlight_lines:
        card_draw.text((content_x, content_y), line, font=body_font, fill=(60, 60, 60))
        content_y += _font_line_height(body_font) + 6

    qr_size = 280
    qr_margin_bottom = 56
    qr_img = _build_qr_image(detail_url)
    if qr_img:
        resample = _poster_resample_filter() or Image.BICUBIC
        qr_img = qr_img.resize((qr_size, qr_size), resample=resample)
        qr_with_border = ImageOps.expand(qr_img, border=12, fill='white')
        qr_box_size = qr_with_border.size[0]
        qr_x = card_width - content_x - qr_box_size
        qr_y = card_height - qr_margin_bottom - qr_box_size
        card.paste(qr_with_border, (qr_x, qr_y))
        card_draw.text((content_x, qr_y), '扫码加入事项', font=section_font, fill=(33, 33, 33))
        guidance_width = min(max_text_width, max(qr_x - content_x - 24, 200))
        guidance_lines = _wrap_text(card_draw, '长按识别二维码，直接进入活动详情页。', hint_font, guidance_width, max_lines=2)
        line_y = qr_y + _font_line_height(section_font) + 12
        for line in guidance_lines:
            card_draw.text((content_x, line_y), line, font=hint_font, fill=(102, 102, 102))
            line_y += _font_line_height(hint_font) + 2
        if validity_hint:
            card_draw.text(
                (content_x, line_y + 8),
                f'二维码有效期：{validity_hint}',
                font=hint_font,
                fill=(120, 120, 120)
            )

    else:
        fallback_text = '二维码生成失败，请稍后重试。'
        card_draw.text((content_x, card_height - qr_margin_bottom - _font_line_height(info_font)), fallback_text, font=info_font, fill=(180, 30, 30))

    base.paste(card, (outer_margin, outer_margin))

    header_font = _load_font(28, bold=True) or ImageFont.load_default()
    base_draw = ImageDraw.Draw(base)
    base_draw.text((outer_margin, 24), 'Benlab 活动分享海报', font=header_font, fill=(126, 128, 145))

    output = BytesIO()
    base.save(output, format='PNG', optimize=True)
    output.seek(0)
    return output

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

# 物品-负责人：多对多关联表
item_members = db.Table(
    'item_members',
    db.Column('item_id', db.Integer, db.ForeignKey('items.id'), primary_key=True),
    db.Column('member_id', db.Integer, db.ForeignKey('members.id'), primary_key=True)
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

# 允许上传的媒体扩展名
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v'}
AUDIO_EXTENSIONS = {'mp3', 'wav', 'aac', 'm4a', 'ogg', 'flac'}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
MEDIA_KIND_LABELS = {
    'image': '图片',
    'video': '视频',
    'audio': '音频',
    'file': '文件'
}


def _extract_file_extension(filename):
    if not filename:
        return ''
    name = filename.rsplit('/', 1)[-1]
    if '?' in name:
        name = name.split('?', 1)[0]
    if '#' in name:
        name = name.split('#', 1)[0]
    if '.' not in name:
        return ''
    return name.rsplit('.', 1)[1].lower()


def allowed_file(filename):
    return _extract_file_extension(filename) in ALLOWED_EXTENSIONS


def _parse_coordinate(raw_value):
    """Return a normalized float for latitude/longitude or None on failure."""
    if raw_value in (None, '', 'undefined'):
        return None
    try:
        return round(float(raw_value), 8)
    except (TypeError, ValueError):
        return None


_EXTERNAL_MEDIA_PREFIXES = ('http://', 'https://', '//')


def _is_external_media(ref):
    return isinstance(ref, str) and ref.startswith(_EXTERNAL_MEDIA_PREFIXES)


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


def _member_display_key(member):
    """Sort key for member display/order handling."""
    if not member:
        return ('', 0)
    name = (member.name or member.username or '').strip().lower()
    return (name, member.id or 0)


def _ensure_item_responsible_members(item):
    """Backfill legacy responsible relationship using responsible_id if needed."""
    if not item:
        return False
    members = list(getattr(item, 'responsible_members', []) or [])
    if members:
        return False
    legacy_id = getattr(item, 'responsible_id', None)
    if not legacy_id:
        return False
    legacy_member = db.session.get(Member, legacy_id)
    if not legacy_member:
        return False
    item.assign_responsible_members([legacy_member])
    return True

def determine_media_kind(ref):
    """Return media kind string for the given filename or URL."""
    ext = _extract_file_extension(ref)
    if ext in IMAGE_EXTENSIONS:
        return 'image'
    if ext in VIDEO_EXTENSIONS:
        return 'video'
    if ext in AUDIO_EXTENSIONS:
        return 'audio'
    if _is_external_media(ref):
        return 'image'
    return 'file'

_LINK_PATTERN = re.compile(r'(?P<url>https?://[^\s<>"\'`]+)', re.IGNORECASE)
_HASHTAG_PATTERN = re.compile(r'(?<![\w#])#(?P<tag>[\w\u4e00-\u9fa5-]+)')
_MENTION_PATTERN = re.compile(r'(?<![\w@])@(?P<handle>[\w\u4e00-\u9fa5-]+)')
_SENTIMENT_GOOD_TOKEN = '__SENT_POS__'
_SENTIMENT_DOUBT_TOKEN = '__SENT_QUEST__'
_ALLOWED_ITEM_FEATURES = {'公共', '私人'}
_ITEM_FEATURE_INTENTS = {
    '公共': 'public',
    '私人': 'private'
}
_STOCK_STATUS_INTENTS = {
    '充足': 'positive',
    '少量': 'warning',
    '用完': 'critical',
    '舍弃': 'muted',
    '闲置': 'muted',
    '借出': 'info',
    '待售': 'warning',
    '售出': 'muted'
}
_MEMBER_RELATION_TYPES = {
    'study': '上学',
    'work': '工作',
    'live': '居住',
    'own': '拥有',
    'other': '其他'
}
_MEMBER_ITEM_REL_TYPES = {
    'borrow': '租借',
    'praise': '好评',
    'favorite': '收藏',
    'wishlist': '待购',
    'other': '其他'
}
_MEMBER_EVENT_REL_TYPES = {
    'host': '主办',
    'join': '参与',
    'support': '协助',
    'follow': '关注',
    'interested': '想参加',
    'other': '其他'
}
_LOCATION_USAGE_CHOICES = [
    ('study', '学习空间'),
    ('leisure', '休闲娱乐'),
    ('event', '活动场地'),
    ('public', '公共设施'),
    ('rental', '出租空间'),
    ('residence', '生活社区'),
    ('other', '其他')
]
_LOCATION_USAGE_KEYS = {key for key, _ in _LOCATION_USAGE_CHOICES}
_LOCATION_USAGE_LABELS = dict(_LOCATION_USAGE_CHOICES)


def _ensure_string(value):
    return value if isinstance(value, str) else ''


def _parse_item_detail_links(raw):
    """Return normalized list of {'label','url'} dicts from stored string/list."""
    if not raw:
        return []
    data = raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            lines = [part.strip() for part in raw.splitlines() if part.strip()]
            entries = []
            for token in lines:
                if '|||' in token:
                    label, url = token.split('|||', 1)
                elif '|' in token:
                    label, url = token.split('|', 1)
                else:
                    label, url = '', token
                label = label.strip()
                url = url.strip()
                if url:
                    entries.append({'label': label, 'url': url})
            if entries:
                return entries
            chunks = [part.strip() for part in re.split(r'[\n,]', raw) if part.strip()]
            data = [{'label': '', 'url': chunk} for chunk in chunks]
    entries = []
    if isinstance(data, dict) and 'url' in data:
        data = [data]
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, str):
                url = entry.strip()
                if url:
                    entries.append({'label': '', 'url': url})
            elif isinstance(entry, dict):
                url = _ensure_string(entry.get('url')).strip()
                label = _ensure_string(entry.get('label')).strip()
                if url:
                    entries.append({'label': label, 'url': url})
    # 去重，保留顺序
    seen = set()
    normalized = []
    for entry in entries:
        key = entry['url']
        if key not in seen:
            normalized.append(entry)
            seen.add(key)
    return normalized


def _serialize_item_detail_links(entries, max_length=64):
    """Serialize list of {'label','url'} dicts into compact string, enforcing length."""
    if not entries:
        return None, False
    payload = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = _ensure_string(entry.get('url')).strip()
        if not url or url in seen:
            continue
        label = _ensure_string(entry.get('label')).strip()
        payload.append({'label': label, 'url': url})
        seen.add(url)
    trimmed = False
    while payload:
        lines = []
        for entry in payload:
            label = entry['label'].replace('\n', ' ').strip()
            url = entry['url']
            token = f"{label}|||{url}" if label else url
            lines.append(token)
        serialized = '\n'.join(lines)
        if not serialized:
            break
        if max_length is None or len(serialized) <= max_length:
            return serialized, trimmed
        payload.pop()  # 移除最后一条，避免超长
        trimmed = True
    return None, trimmed


def _normalize_item_feature(value):
    value = (value or '').strip()
    return value if value in _ALLOWED_ITEM_FEATURES else None


def _feature_intent(value):
    """Return semantic intent for public/private feature badges."""
    value = (value or '').strip()
    return _ITEM_FEATURE_INTENTS.get(value, 'neutral')


def _stock_status_intent(value):
    """Return semantic intent token for inventory stock status badges/rows."""
    if not value:
        return 'neutral'
    tokens = [segment.strip() for segment in str(value).split(',') if segment.strip()]
    for token in tokens:
        if token in _STOCK_STATUS_INTENTS:
            return _STOCK_STATUS_INTENTS[token]
    return 'neutral'


def _collect_detail_links_from_form(form):
    labels = form.getlist('detail_link_label')
    urls = form.getlist('detail_link_url')
    entries = []
    for label, url in zip(labels, urls):
        url = _ensure_string(url).strip()
        label = _ensure_string(label).strip()
        if url:
            entries.append({'label': label, 'url': url})
    if entries:
        return entries
    fallback = form.get('detail_link_text') or form.get('cas_no')
    if fallback:
        return _parse_item_detail_links(fallback)
    return []


def _parse_location_notes(raw):
    empty = {
        'description': '',
        'usage_tags': [],
        'access_info': '',
        'is_public': False
    }
    if not raw:
        return empty, False
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            empty['description'] = _ensure_string(raw)
            return empty, False
    if isinstance(data, dict) and 'description' in data:
        description = _ensure_string(data.get('description'))
        usage_raw = data.get('usage_tags') or []
        usage_tags = []
        if isinstance(usage_raw, list):
            for tag in usage_raw:
                tag = _ensure_string(tag)
                if tag in _LOCATION_USAGE_KEYS:
                    usage_tags.append(tag)
        access_info = _ensure_string(data.get('access_info'))
        is_public = bool(data.get('is_public'))
        meta = {
            'description': description,
            'usage_tags': usage_tags,
            'access_info': access_info,
            'is_public': is_public
        }
        return meta, True
    empty['description'] = _ensure_string(raw if isinstance(raw, str) else '')
    return empty, False


def _serialize_location_notes(meta):
    payload = {
        'description': _ensure_string(meta.get('description')),
        'usage_tags': [],
        'access_info': _ensure_string(meta.get('access_info')),
        'is_public': bool(meta.get('is_public'))
    }
    usage_tags = meta.get('usage_tags') or []
    seen = set()
    for tag in usage_tags:
        tag = _ensure_string(tag)
        if tag in _LOCATION_USAGE_KEYS and tag not in seen:
            payload['usage_tags'].append(tag)
            seen.add(tag)
    return json.dumps(payload, ensure_ascii=False)


def _parse_profile_notes(raw):
    """Decode member.notes into structured profile meta."""
    empty = {
        'bio': '',
        'social_links': [],
        'location_relations': [],
        'item_relations': [],
        'event_relations': []
    }
    if not raw:
        return empty, False
    if isinstance(raw, dict):
        data = raw
        structured = True
    else:
        try:
            data = json.loads(raw)
            structured = isinstance(data, dict) and 'bio' in data
        except json.JSONDecodeError:
            structured = False
            data = None
    if structured and isinstance(data, dict):
        meta = {
            'bio': _ensure_string(data.get('bio')),
            'social_links': [],
            'location_relations': [],
            'item_relations': [],
            'event_relations': []
        }
        social = data.get('social_links') or []
        if isinstance(social, list):
            for entry in social:
                if not isinstance(entry, dict):
                    continue
                label = _ensure_string(entry.get('label')).strip()
                url = _ensure_string(entry.get('url')).strip()
                if url:
                    meta['social_links'].append({'label': label, 'url': url})
        locs = data.get('location_relations') or []
        if isinstance(locs, list):
            for entry in locs:
                if not isinstance(entry, dict):
                    continue
                loc_id = entry.get('location_id')
                try:
                    loc_id = int(loc_id)
                except (TypeError, ValueError):
                    continue
                relation = _ensure_string(entry.get('relation')).strip()
                if relation not in _MEMBER_RELATION_TYPES:
                    relation = 'other'
                note = _ensure_string(entry.get('note')).strip()
                meta['location_relations'].append({
                    'location_id': loc_id,
                    'relation': relation,
                    'note': note
                })
        item_links = data.get('item_relations') or []
        if isinstance(item_links, list):
            for entry in item_links:
                if not isinstance(entry, dict):
                    continue
                item_id = entry.get('item_id')
                try:
                    item_id = int(item_id)
                except (TypeError, ValueError):
                    continue
                relation = _ensure_string(entry.get('relation')).strip()
                if relation not in _MEMBER_ITEM_REL_TYPES:
                    relation = 'other'
                note = _ensure_string(entry.get('note')).strip()
                meta['item_relations'].append({
                    'item_id': item_id,
                    'relation': relation,
                    'note': note
                })
        event_links = data.get('event_relations') or []
        if isinstance(event_links, list):
            for entry in event_links:
                if not isinstance(entry, dict):
                    continue
                event_id = entry.get('event_id')
                try:
                    event_id = int(event_id)
                except (TypeError, ValueError):
                    continue
                relation = _ensure_string(entry.get('relation')).strip()
                if relation not in _MEMBER_EVENT_REL_TYPES:
                    relation = 'other'
                note = _ensure_string(entry.get('note')).strip()
                meta['event_relations'].append({
                    'event_id': event_id,
                    'relation': relation,
                    'note': note
                })
        return meta, True
    # 兼容旧文本
    empty['bio'] = _ensure_string(raw)
    return empty, False


def _serialize_profile_notes(meta):
    """Serialize structured profile meta to JSON string."""
    payload = {
        'bio': _ensure_string(meta.get('bio')),
        'social_links': [],
        'location_relations': [],
        'item_relations': [],
        'event_relations': []
    }
    social = meta.get('social_links') or []
    for entry in social:
        if not isinstance(entry, dict):
            continue
        label = _ensure_string(entry.get('label')).strip()
        url = _ensure_string(entry.get('url')).strip()
        if url:
            payload['social_links'].append({'label': label, 'url': url})
    locs = meta.get('location_relations') or []
    seen = set()
    for entry in locs:
        if not isinstance(entry, dict):
            continue
        try:
            loc_id = int(entry.get('location_id'))
        except (TypeError, ValueError):
            continue
        relation = _ensure_string(entry.get('relation')).strip()
        if relation not in _MEMBER_RELATION_TYPES:
            relation = 'other'
        note = _ensure_string(entry.get('note')).strip()
        key = (loc_id, relation, note)
        if key in seen:
            continue
        seen.add(key)
        payload['location_relations'].append({
            'location_id': loc_id,
            'relation': relation,
            'note': note
        })
    items_rel = meta.get('item_relations') or []
    seen_items = set()
    for entry in items_rel:
        if not isinstance(entry, dict):
            continue
        try:
            item_id = int(entry.get('item_id'))
        except (TypeError, ValueError):
            continue
        relation = _ensure_string(entry.get('relation')).strip()
        if relation not in _MEMBER_ITEM_REL_TYPES:
            relation = 'other'
        note = _ensure_string(entry.get('note')).strip()
        key = (item_id, relation, note)
        if key in seen_items:
            continue
        seen_items.add(key)
        payload['item_relations'].append({
            'item_id': item_id,
            'relation': relation,
            'note': note
        })
    events_rel = meta.get('event_relations') or []
    seen_events = set()
    for entry in events_rel:
        if not isinstance(entry, dict):
            continue
        try:
            event_id = int(entry.get('event_id'))
        except (TypeError, ValueError):
            continue
        relation = _ensure_string(entry.get('relation')).strip()
        if relation not in _MEMBER_EVENT_REL_TYPES:
            relation = 'other'
        note = _ensure_string(entry.get('note')).strip()
        key = (event_id, relation, note)
        if key in seen_events:
            continue
        seen_events.add(key)
        payload['event_relations'].append({
            'event_id': event_id,
            'relation': relation,
            'note': note
        })
    return json.dumps(payload, ensure_ascii=False)


def _build_event_summary(events, recent_past_limit=5):
    """Categorize events into ongoing/upcoming/etc and compute summary stats."""
    now = datetime.utcnow()
    ongoing = []
    upcoming = []
    unscheduled = []
    past = []
    for ev in events:
        start = ev.start_time
        end = ev.end_time
        if start and end:
            if end < now:
                past.append(ev)
            elif start > now:
                upcoming.append(ev)
            else:
                ongoing.append(ev)
        elif start:
            if start >= now:
                upcoming.append(ev)
            else:
                past.append(ev)
        else:
            unscheduled.append(ev)

    def sort_key(ev):
        if ev.start_time:
            return ev.start_time
        if ev.end_time:
            return ev.end_time
        return ev.created_at or datetime.utcnow()

    ongoing.sort(key=sort_key)
    upcoming.sort(key=sort_key)
    unscheduled.sort(key=lambda ev: ev.updated_at or ev.created_at or datetime.utcnow(), reverse=True)
    past.sort(key=lambda ev: ev.end_time or ev.start_time or ev.updated_at or ev.created_at or datetime.min, reverse=True)
    recent_past = past[:recent_past_limit]
    summary = {
        'total': len(events),
        'ongoing': len(ongoing),
        'upcoming': len(upcoming),
        'unscheduled': len(unscheduled),
        'past': len(past),
        'participants': sum(len(ev.participant_links) for ev in events)
    }
    return {
        'summary': summary,
        'ongoing': ongoing,
        'upcoming': upcoming,
        'unscheduled': unscheduled,
        'recent_past': recent_past,
        'past_total': len(past)
    }


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
    now = datetime.now(UTC)
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
    return _as_utc(raw)


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
        timestamp_display = format_china_time(ts) if ts else ''
        entries.append({
            'html': render_rich_text(content, mention_lookup),
            'timestamp': ts,
            'timestamp_display': timestamp_display or '未知时间',
            'sender_id': sender_id,
            'sender_name': display_name,
            'sender_url': sender_url,
            'sentiment': sentiment
        })
    entries.sort(
        key=lambda item: item['timestamp'] or datetime.min.replace(tzinfo=UTC),
        reverse=True
    )
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

def _get_oss_bucket():
    """Lazy initialise and return OSS bucket instance when启用."""
    global _oss_bucket
    if not app.config.get('USE_OSS'):
        return None
    if _oss_bucket is None:
        if not oss2:
            raise RuntimeError('oss2 library not available but OSS is enabled.')
        auth = oss2.Auth(
            app.config['OSS_ACCESS_KEY_ID'],
            app.config['OSS_ACCESS_KEY_SECRET']
        )
        _oss_bucket = oss2.Bucket(auth, app.config['OSS_ENDPOINT'], app.config['OSS_BUCKET'])
    return _oss_bucket


def _generate_stored_filename(original_name):
    sanitized = secure_filename(original_name or '')
    if not sanitized:
        sanitized = 'upload'
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    return f"{timestamp}_{sanitized}"


def save_uploaded_media(file_storage):
    """Persist an uploaded media file and return the stored filename."""
    if not file_storage or file_storage.filename == '':
        return None
    if not allowed_file(file_storage.filename):
        return None
    stored_name = _generate_stored_filename(file_storage.filename)
    if app.config.get('USE_OSS'):
        bucket = _get_oss_bucket()
        prefix = app.config.get('OSS_PREFIX')
        object_key = f"{prefix}/{stored_name}" if prefix else stored_name
        file_storage.stream.seek(0)
        bucket.put_object(object_key, file_storage.stream.read())
        return object_key
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], stored_name)
    file_storage.save(filepath)
    return stored_name


def remove_uploaded_file(filename):
    """Delete a previously saved image file if it still exists."""
    if not filename or _is_external_media(filename):
        return
    if app.config.get('USE_OSS'):
        bucket = _get_oss_bucket()
        prefix = app.config.get('OSS_PREFIX')
        key = filename
        if prefix and key and not key.startswith(prefix):
            # legacy local path retained after切换：尝试删除本地文件
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            return
        if not prefix:
            local_legacy_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.exists(local_legacy_path):
                try:
                    os.remove(local_legacy_path)
                except OSError:
                    pass
        try:
            bucket.delete_object(key)
        except oss2.exceptions.NoSuchKey:  # type: ignore[attr-defined]
            pass
        return
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _build_oss_url(key):
    key = key.lstrip('/')
    public_base = app.config.get('OSS_PUBLIC_BASE_URL')
    if public_base:
        return f"{public_base}/{key}"
    endpoint = app.config.get('OSS_ENDPOINT', '')
    endpoint = endpoint.replace('https://', '').replace('http://', '')
    bucket = app.config.get('OSS_BUCKET')
    return f"https://{bucket}.{endpoint}/{key}"


def _normalize_object_key(value):
    if not value:
        return None
    token = str(value).strip().replace('\\', '/')
    token = token.lstrip('/')
    if not token or '..' in token or token.startswith('http://') or token.startswith('https://'):
        return None
    return token


def _collect_remote_object_keys(field_name):
    if not field_name or not app.config.get('USE_OSS') or not app.config.get('DIRECT_OSS_UPLOAD_ENABLED'):
        return []
    remote_field = f"{field_name}{REMOTE_FIELD_SUFFIX}"
    seen = set()
    refs = []
    for raw in request.form.getlist(remote_field):
        normalized = _normalize_object_key(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        refs.append(normalized)
    return refs


def _append_media_records(collection, model_cls, filenames):
    if not filenames:
        return
    existing = {getattr(entry, 'filename') for entry in collection if getattr(entry, 'filename', None)}
    for name in filenames:
        if not name or name in existing:
            continue
        collection.append(model_cls(filename=name))
        existing.add(name)


def _build_direct_upload_config():
    enabled = bool(app.config.get('DIRECT_OSS_UPLOAD_ENABLED'))
    config = {
        'enabled': enabled,
        'field_suffix': REMOTE_FIELD_SUFFIX
    }
    if not enabled:
        return config
    max_size = app.config.get('MAX_CONTENT_LENGTH')
    if max_size and max_size > 0:
        if max_size >= 1024 * 1024:
            human_size = f"{max_size / (1024 * 1024):.1f} MB"
        else:
            human_size = f"{max_size / 1024:.0f} KB"
    else:
        human_size = None
    config.update({
        'presign_url': url_for('create_direct_oss_upload'),
        'max_size': max_size,
        'max_size_label': human_size,
        'storage': 'oss'
    })
    return config

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
    items = db.relationship(
        'Item',
        secondary='item_members',
        backref=db.backref('responsible_members', lazy='select'),
        lazy='select'
    )
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
    cas_no = db.Column(db.String(64), nullable=True)     # 详情链接集合（JSON 字符串，兼容旧 CAS 数据）
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
    def detail_links(self):
        return _parse_item_detail_links(self.cas_no)

    def set_detail_links(self, entries):
        serialized, trimmed = _serialize_item_detail_links(entries)
        self.cas_no = serialized
        return trimmed

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

    def assign_responsible_members(self, members):
        """Assign responsible members ensuring uniqueness and stable order."""
        unique = []
        seen_ids = set()
        for member in members or []:
            if not member:
                continue
            if member.id in seen_ids:
                continue
            unique.append(member)
            seen_ids.add(member.id)
        unique.sort(key=_member_display_key)
        self.responsible_members = unique
        self.responsible_id = unique[0].id if unique else None
        return unique

    @property
    def primary_responsible(self):
        members = list(self.responsible_members or [])
        if not members:
            return None
        members.sort(key=_member_display_key)
        return members[0]

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
    allow_participant_edit = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
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
        if not member:
            return False
        if member.id == self.owner_id:
            return True
        allow_edit = getattr(self, 'allow_participant_edit', False)
        if self.visibility == 'internal' and allow_edit:
            return any(link.member_id == member.id for link in self.participant_links)
        return False

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


def grant_internal_event_access(event, member, source='share-link'):
    """Auto-enroll a member into an internal event when arriving via share."""
    if not member or not event or event.visibility != 'internal':
        return False
    if member.id == event.owner_id or event.is_participant(member):
        return False
    event.participant_links.append(
        EventParticipant(member_id=member.id, role='participant', status='confirmed')
    )
    event.touch()
    log = Log(
        user_id=member.id,
        event_id=event.id,
        action_type="扫码加入事项",
        details=f"Auto-joined via {source}"
    )
    db.session.add(log)
    db.session.commit()
    return True


def add_event_images(event, file_storages):
    for image_file in file_storages:
        stored_name = save_uploaded_media(image_file)
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
        if 'allow_participant_edit' not in event_cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE events ADD COLUMN allow_participant_edit BOOLEAN DEFAULT 0'))
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
    next_url = request.args.get('next', '')
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember_raw = request.form.get('remember')
        remember_me = str(remember_raw).lower() in {'1', 'true', 'on', 'yes'}
        next_url = request.form.get('next') or next_url
        user = Member.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=remember_me)
            if next_url and next_url.startswith('/'):
                return redirect(next_url)
            return redirect(url_for('index'))
        else:
            flash('用户名或密码不正确', 'danger')
    return render_template('login.html', next_url=next_url)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/api/uploads/oss/presign', methods=['POST'])
@login_required
def create_direct_oss_upload():
    if not app.config.get('DIRECT_OSS_UPLOAD_ENABLED'):
        abort(404)
    if not app.config.get('USE_OSS'):
        abort(404)
    bucket = _get_oss_bucket()
    if bucket is None:
        abort(503)
    payload = request.get_json(silent=True) or {}
    filename = (payload.get('filename') or '').strip()
    content_type = (payload.get('content_type') or 'application/octet-stream').strip() or 'application/octet-stream'
    if not filename:
        return jsonify({'error': 'missing_filename'}), 400
    if not allowed_file(filename):
        return jsonify({'error': 'unsupported_type'}), 400
    max_size = app.config.get('MAX_CONTENT_LENGTH')
    declared_size = payload.get('size') or payload.get('filesize')
    if declared_size is not None:
        try:
            declared_size_value = int(declared_size)
        except (TypeError, ValueError):
            declared_size_value = None
        if declared_size_value is not None and max_size and declared_size_value > max_size:
            return jsonify({'error': 'file_too_large', 'max_size': max_size}), 400
    stored_name = _generate_stored_filename(filename)
    prefix = app.config.get('OSS_PREFIX')
    object_key = f"{prefix}/{stored_name}" if prefix else stored_name
    expires = app.config.get('DIRECT_UPLOAD_URL_EXPIRATION', 900)
    headers = {'Content-Type': content_type}
    upload_url = bucket.sign_url('PUT', object_key, expires, headers=headers)
    return jsonify({
        'object_key': object_key,
        'upload_url': upload_url,
        'headers': headers,
        'access_url': _build_oss_url(object_key),
        'expires_in': expires,
        'max_size': max_size
    })


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        next_target = request.args.get('next') or request.form.get('next')
        if next_target and next_target.startswith('/'):
            return redirect(next_target)
        return redirect(url_for('index'))
    next_url = request.args.get('next', '')
    if request.method == 'POST':
        name = request.form.get('name')
        username = request.form.get('username')
        password = request.form.get('password')
        contact = request.form.get('contact')
        next_url = request.form.get('next') or next_url
        # 检查用户名是否已存在
        if Member.query.filter_by(username=username).first():
            flash('用户名已存在', 'warning')
        else:
            new_user = Member(name=name, username=username, contact=contact)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            flash('注册成功，请登录', 'success')
            if next_url and next_url.startswith('/'):
                return redirect(url_for('login', next=next_url))
            return redirect(url_for('login'))
    return render_template('register.html', next_url=next_url)


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


def _merge_selection_ids(new_ids, existing_ids, touched_flag):
    """
    Normalize selection lists coming from the form.

    If the browser sent a non-empty list we always trust it, even when the
    “touched” hidden flag failed to flip (this happens on Safari when the
    modal markup is cached). When the browser sent nothing, we only treat it
    as “cleared” if the hidden flag was set; otherwise we assume the user
    never re-opened that modal and keep the previously stored IDs.
    """
    existing_ids = set(existing_ids or [])
    new_ids = set(new_ids or [])
    if new_ids:
        if new_ids != existing_ids:
            touched_flag = True
        return new_ids, touched_flag
    if touched_flag:
        # User explicitly cleared the selection
        return set(), True
    # Browser submitted nothing — reuse what we already have
    return existing_ids, False


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
        'external_image_urls': '',
        'allow_participant_edit': False,
        'participant_selection_touched': '0',
        'location_selection_touched': '0',
        'item_selection_touched': '0',
        'start_time_touched': '0',
        'end_time_touched': '0'
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
        allow_participant_edit = request.form.get('allow_participant_edit') in {'1', 'true', 'on'}
        if visibility != 'internal':
            allow_participant_edit = False

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
            'external_image_urls': request.form.get('external_event_image_urls', ''),
            'allow_participant_edit': allow_participant_edit,
            'participant_selection_touched': request.form.get('participant_selection_touched', '0'),
            'location_selection_touched': request.form.get('location_selection_touched', '0'),
            'item_selection_touched': request.form.get('item_selection_touched', '0'),
            'start_time_touched': request.form.get('start_time_touched', '0'),
            'end_time_touched': request.form.get('end_time_touched', '0')
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
            detail_link=detail_link or None,
            allow_participant_edit=allow_participant_edit
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
        remote_event_refs = _collect_remote_object_keys('event_images')
        if remote_event_refs:
            _append_media_records(event.images, EventImage, remote_event_refs)
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
    share_meta = build_event_share_metadata(event)
    return render_template(
        'event_detail.html',
        event=event,
        linked_description=linked_description,
        participant_links=participant_links,
        missing_items=missing_items,
        missing_locations=missing_locations,
        allow_join=allow_join,
        feedback_entries=feedback_entries,
        feedback_post_url=url_for('post_event_feedback', event_id=event.id),
        share_meta=share_meta
    )


@app.route('/events/<int:event_id>/poster.png')
def event_share_poster(event_id):
    event = Event.query.options(
        selectinload(Event.owner),
        selectinload(Event.locations),
        selectinload(Event.images)
    ).get_or_404(event_id)

    if event.visibility != 'public':
        if not current_user.is_authenticated or not event.can_view(current_user):
            abort(403)

    share_meta = build_event_share_metadata(event)
    if share_meta:
        detail_url = share_meta['url']
        validity_hint = share_meta['validity_hint']
    else:
        detail_url = url_for('event_detail', event_id=event.id, _external=True)
        validity_hint = None
    try:
        output = generate_event_share_poster(event, detail_url, validity_hint=validity_hint)
    except RuntimeError as exc:
        abort(503, description=str(exc))

    download = request.args.get('download') == '1'
    filename = f"event-{event.id}-poster.png"
    response = send_file(
        output,
        mimetype='image/png',
        as_attachment=download,
        download_name=filename
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/events/<int:event_id>/share/<string:token>')
def event_share_entry(event_id, token):
    event = Event.query.options(selectinload(Event.participant_links)).get_or_404(event_id)
    payload, error = verify_event_share_token(token)
    if error == 'expired':
        flash('分享链接已过期，请联系活动负责人重新生成最新海报。', 'warning')
        if current_user.is_authenticated:
            if event.can_view(current_user):
                return redirect(url_for('event_detail', event_id=event.id))
            return redirect(url_for('events_overview'))
        return redirect(url_for('login'))
    if not payload or payload.get('event_id') != event.id:
        abort(403)
    if event.visibility == 'personal':
        abort(403)

    if not current_user.is_authenticated:
        flash('请先登录或注册，我们会在回来后自动为你开通该事项的权限。', 'info')
        return redirect(url_for('login', next=request.path))

    joined = grant_internal_event_access(event, current_user, source='share-link')
    if joined:
        flash('已自动将你加入事项，可立即查看详情。', 'success')
    return redirect(url_for('event_detail', event_id=event.id))


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
        requested_visibility = request.form.get('visibility', event.visibility)
        if requested_visibility not in {'personal', 'internal', 'public'}:
            requested_visibility = event.visibility
        if current_user.id == event.owner_id:
            visibility = requested_visibility
        else:
            visibility = event.visibility
        start_raw = request.form.get('start_time')
        end_raw = request.form.get('end_time')
        start_time = parse_datetime_local(start_raw)
        end_time = parse_datetime_local(end_raw)
        existing_item_ids = {item.id for item in event.items}
        existing_location_ids = {loc.id for loc in event.locations}
        item_ids = _collect_selected_ids(request.form.getlist('item_ids'))
        location_ids = _collect_selected_ids(request.form.getlist('location_ids'))
        existing_participant_ids = {
            link.member_id for link in event.participant_links
            if link.member_id != event.owner_id
        }
        participant_ids = _collect_selected_ids(request.form.getlist('participant_ids'))
        participant_selection_touched = request.form.get('participant_selection_touched') == '1'
        location_selection_touched = request.form.get('location_selection_touched') == '1'
        item_selection_touched = request.form.get('item_selection_touched') == '1'
        start_time_touched = request.form.get('start_time_touched') == '1'
        end_time_touched = request.form.get('end_time_touched') == '1'
        detail_link = (request.form.get('detail_link') or '').strip()
        allow_participant_edit = request.form.get('allow_participant_edit') in {'1', 'true', 'on'}
        if visibility != 'internal':
            allow_participant_edit = False
        can_manage_members = current_user.id == event.owner_id
        if not can_manage_members:
            participant_ids = set(existing_participant_ids)
            participant_selection_touched = False
        else:
            participant_ids, participant_selection_touched = _merge_selection_ids(
                participant_ids, existing_participant_ids, participant_selection_touched
            )

        location_ids, location_selection_touched = _merge_selection_ids(
            location_ids, existing_location_ids, location_selection_touched
        )
        item_ids, item_selection_touched = _merge_selection_ids(
            item_ids, existing_item_ids, item_selection_touched
        )

        if not start_time_touched:
            start_time = event.start_time
        if not end_time_touched:
            end_time = event.end_time

        available_member_ids = {member.id for member in members}
        participant_ids = {pid for pid in participant_ids if pid in available_member_ids and pid != event.owner_id}

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
            'external_image_urls': request.form.get('external_event_image_urls', ''),
            'allow_participant_edit': allow_participant_edit,
            'participant_selection_touched': '1' if participant_selection_touched else '0',
            'location_selection_touched': '1' if location_selection_touched else '0',
            'item_selection_touched': '1' if item_selection_touched else '0',
            'start_time_touched': '1' if start_time_touched else '0',
            'end_time_touched': '1' if end_time_touched else '0'
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
        if current_user.id == event.owner_id:
            event.allow_participant_edit = allow_participant_edit and visibility == 'internal'
        elif event.visibility != 'internal':
            event.allow_participant_edit = False

        if can_manage_members:
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
        remote_event_refs = _collect_remote_object_keys('event_images')
        if remote_event_refs:
            _append_media_records(event.images, EventImage, remote_event_refs)
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
        'external_image_urls': '',
        'allow_participant_edit': bool(getattr(event, 'allow_participant_edit', False)),
        'participant_selection_touched': '0',
        'location_selection_touched': '0',
        'item_selection_touched': '0',
        'start_time_touched': '0',
        'end_time_touched': '0'
    }
    return render_template('event_form.html', event=event, members=members, items=items, locations=locations, form_state=form_state)


@app.route('/events/<int:event_id>/delete', methods=['POST'])
@login_required
def delete_event(event_id):
    event = Event.query.get_or_404(event_id)
    if event.owner_id != current_user.id:
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
            selectinload(Item.responsible_members)
        )
        .order_by(func.lower(Item.name))
        .all()
    )
    migrated = False
    for item in items_list:
        if _ensure_item_responsible_members(item):
            migrated = True
    if migrated:
        db.session.commit()
    event_counts = dict(
        db.session.query(
            event_items.c.item_id,
            func.count(event_items.c.event_id)
        ).group_by(event_items.c.item_id).all()
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
        uncategorized_items=uncategorized_payload,
        item_event_counts=event_counts
    )

@app.route('/items/<int:item_id>')
@login_required
def item_detail(item_id):
    # 查看物品详情
    item = (
        Item.query.options(
            selectinload(Item.responsible_members),
            selectinload(Item.locations)
        ).get_or_404(item_id)
    )
    if _ensure_item_responsible_members(item):
        db.session.commit()
    events = (
        Event.query
        .join(event_items)
        .filter(event_items.c.item_id == item.id)
        .options(
            db.selectinload(Event.owner),
            db.selectinload(Event.participant_links).selectinload(EventParticipant.member),
            db.selectinload(Event.locations)
        )
        .all()
    )
    event_bundle = _build_event_summary(events)
    interest_relation_lookup = dict(_MEMBER_ITEM_REL_TYPES)
    interest_counter = Counter()
    interest_total = 0
    members_interest_summary = []
    members = Member.query.options(load_only(Member.id, Member.name, Member.username, Member.notes)).all()
    for member in members:
        meta, _ = _parse_profile_notes(member.notes)
        for relation_entry in meta.get('item_relations', []) or []:
            if relation_entry.get('item_id') != item.id:
                continue
            relation_key = _ensure_string(relation_entry.get('relation')).strip()
            if relation_key not in interest_relation_lookup:
                relation_key = 'other'
            interest_counter[relation_key] += 1
            interest_total += 1
    for rel_key, count in sorted(interest_counter.items(), key=lambda pair: (-pair[1], pair[0])):
        members_interest_summary.append({
            'relation': rel_key,
            'label': interest_relation_lookup.get(rel_key, rel_key),
            'count': count
        })
    return render_template(
        'item_detail.html',
        item=item,
        detail_links=item.detail_links,
        event_summary=event_bundle['summary'],
        ongoing_events=event_bundle['ongoing'],
        upcoming_events=event_bundle['upcoming'],
        unscheduled_events=event_bundle['unscheduled'],
        recent_past_events=event_bundle['recent_past'],
        past_events_total=event_bundle['past_total'],
        interest_summary=members_interest_summary,
        interest_total=interest_total,
        interest_relation_lookup=interest_relation_lookup
    )

@app.route('/items/add', methods=['GET', 'POST'])
@login_required
def add_item():
    default_loc_id = request.args.get('loc_id', type=int)
    if request.method == 'POST':
        # 获取表单数据
        name = request.form.get('name')
        category = request.form.get('category')
        stock_status = request.form.get('stock_status')  # ✅ 单选字段
        feature_raw = request.form.get('features')
        features_str = _normalize_item_feature(feature_raw)  # 统一为公共/私人
        if not features_str:
            flash('请选择物品特性（公共或私人）。', 'danger')
            return redirect(request.url)

        value = request.form.get('value')
        value = float(value) if value else None          # ✅ 数字输入

        quantity = request.form.get('quantity')
        quantity = float(quantity) if quantity else None # ✅ 数量输入
        unit = request.form.get('unit')                  # ✅ 单位选择

        purchase_date_str = request.form.get('purchase_date')  # ✅ 日期处理
        purchase_date = datetime.strptime(purchase_date_str, '%Y-%m-%d').date() if purchase_date_str else None

        responsible_ids_raw = request.form.getlist('responsible_ids')
        responsible_ids = {int(x) for x in responsible_ids_raw if x.isdigit()}
        responsible_members = []
        if responsible_ids:
            responsible_members = Member.query.filter(Member.id.in_(responsible_ids)).all()
        if features_str == '私人' and not responsible_members:
            responsible_members = [current_user]
        location_ids = request.form.getlist('location_ids')  # ✅ 支持多个位置
        notes = request.form.get('notes')
        purchase_link = request.form.get('purchase_link')
        detail_links = _collect_detail_links_from_form(request.form)

        # 图片处理（支持多张）
        uploaded_files = request.files.getlist('images')
        if not uploaded_files:
            fallback_file = request.files.get('image')
            uploaded_files = [fallback_file] if fallback_file else []

        saved_filenames = []
        for image_file in uploaded_files:
            stored_name = save_uploaded_media(image_file)
            if stored_name:
                saved_filenames.append(stored_name)

        external_urls = _extract_external_urls(request.form.get('external_image_urls'))
        primary_image = saved_filenames[0] if saved_filenames else (external_urls[0] if external_urls else None)

        # ✅ 创建新的 Item 实例（已更新字段）
        new_item = Item(
            name=name,
            category=category,
            stock_status=stock_status,
            features=features_str,
            value=value,
            quantity=quantity,
            unit=unit,
            purchase_date=purchase_date,
            responsible_id=None,
            notes=notes,
            purchase_link=purchase_link,
            image=primary_image
        )
        assigned_members = new_item.assign_responsible_members(responsible_members)
        if features_str == '私人' and not assigned_members:
            new_item.assign_responsible_members([current_user])
        trimmed_links = new_item.set_detail_links(detail_links)
        if trimmed_links:
            flash('部分详情链接过长（字段限 64 字符），已保留前几条，请确认链接长度。', 'warning')
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
    if _ensure_item_responsible_members(item):
        db.session.commit()
    if item.features == '私人' and current_user not in item.responsible_members:
        abort(403)
    if request.method == 'POST':
        # 更新物品信息
        item.name = request.form.get('name')
        item.category = request.form.get('category')
        detail_links = _collect_detail_links_from_form(request.form)
        trimmed_links = item.set_detail_links(detail_links)
        if trimmed_links:
            flash('部分详情链接过长（字段限 64 字符），已保留前几条，请确认链接长度。', 'warning')

        item.stock_status = request.form.get('stock_status')  # 单选库存状态
        feature_raw = request.form.get('features')
        features_str = _normalize_item_feature(feature_raw)          # 单选物品特性
        if not features_str:
            flash('请选择物品特性（公共或私人）。', 'danger')
            return redirect(request.url)
        item.features = features_str
        
        item.value = request.form.get('value', type=float)    # 新增：价值（数值）
        item.quantity = request.form.get('quantity', type=float)  # 数量（数字）
        item.unit = request.form.get('unit')                      # 数量单位

        purchase_date_str = request.form.get('purchase_date')
        if purchase_date_str:
            item.purchase_date = datetime.strptime(purchase_date_str, '%Y-%m-%d').date()

        responsible_ids_raw = request.form.getlist('responsible_ids')
        responsible_ids = {int(x) for x in responsible_ids_raw if x.isdigit()}
        responsible_members = []
        if responsible_ids:
            responsible_members = Member.query.filter(Member.id.in_(responsible_ids)).all()
        assigned_members = item.assign_responsible_members(responsible_members)
        if features_str == '私人' and not assigned_members:
            item.assign_responsible_members([current_user])
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
            stored_name = save_uploaded_media(image_file)
            if stored_name:
                item.images.append(ItemImage(filename=stored_name))
        remote_refs = _collect_remote_object_keys('images')
        if remote_refs:
            _append_media_records(item.images, ItemImage, remote_refs)

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
    if _ensure_item_responsible_members(item):
        db.session.commit()
    if item.features == '私人' and current_user not in item.responsible_members:
        abort(403)
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
    migration_needed = False

    if add_ids:
        candidates = Item.query.filter(Item.id.in_(add_ids)).all()
        for item in candidates:
            if _ensure_item_responsible_members(item):
                migration_needed = True
            if item.features == '私人' and current_user not in item.responsible_members:
                continue
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
            if _ensure_item_responsible_members(item):
                migration_needed = True
            if item.features == '私人' and current_user not in item.responsible_members:
                continue
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
        if migration_needed:
            db.session.commit()
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
    event_counts = dict(
        db.session.query(
            event_locations.c.location_id,
            func.count(event_locations.c.event_id)
        ).group_by(event_locations.c.location_id).all()
    )
    meta_map = {loc.id: _parse_location_notes(loc.notes)[0] for loc in locations}
    return render_template('locations.html',
                           locations=locations,
                           event_counts=event_counts,
                           location_meta_map=meta_map,
                           location_usage_labels=_LOCATION_USAGE_LABELS)


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
        detail_link = request.form.get('detail_link')
        latitude = _parse_coordinate(request.form.get('latitude'))
        longitude = _parse_coordinate(request.form.get('longitude'))
        coordinate_source = request.form.get('coordinate_source') or None
        if latitude is None or longitude is None:
            latitude = None
            longitude = None
            coordinate_source = None
        is_public = request.form.get('is_public') in {'1', 'on', 'true', 'yes'}
        usage_tags = [tag for tag in request.form.getlist('usage_tags') if tag in _LOCATION_USAGE_KEYS]
        description = request.form.get('description') or ''
        access_info = request.form.get('access_info') or ''
        notes_meta = {
            'description': description,
            'usage_tags': usage_tags,
            'access_info': access_info,
            'is_public': is_public
        }
        serialized_notes = _serialize_location_notes(notes_meta)

        uploaded_files = request.files.getlist('images')
        if not uploaded_files:
            fallback_file = request.files.get('image')
            uploaded_files = [fallback_file] if fallback_file else []

        saved_filenames = []
        for image_file in uploaded_files:
            stored_name = save_uploaded_media(image_file)
            if stored_name:
                saved_filenames.append(stored_name)
        saved_filenames.extend(_collect_remote_object_keys('images'))
        external_urls = _extract_external_urls(request.form.get('external_image_urls'))
        primary_image = saved_filenames[0] if saved_filenames else (external_urls[0] if external_urls else None)
        # 先创建 Location
        new_loc = Location(
            name=name,
            parent_id=parent_id if parent_id else None,
            notes=serialized_notes,
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
        if not member_objs and not is_public:
            member_objs = [current_user]
        new_loc.responsible_members = member_objs
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, location_id=new_loc.id, action_type="新增位置", details=f"Added location {new_loc.name}")
        db.session.add(log)
        db.session.commit()

        flash('社区空间已添加', 'success')
        return redirect(url_for('locations_list'))
    
    members = Member.query.all()
    parents = Location.query.all()
    default_meta, _ = _parse_location_notes(None)
    return render_template('location_form.html',
                           members=members,
                           location=None,
                           parents=parents,
                           location_meta=default_meta,
                           location_usage_choices=_LOCATION_USAGE_CHOICES)

@app.route('/locations/<int:loc_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    if location.responsible_members and current_user not in location.responsible_members:
        abort(403)
    current_meta, _ = _parse_location_notes(location.notes)
    if request.method == 'POST':
        # 更新位置信息
        location.name = request.form.get('name')
        responsible_ids = request.form.getlist('responsible_ids')
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
            stored_name = save_uploaded_media(image_file)
            if stored_name:
                location.images.append(LocationImage(filename=stored_name))
        remote_refs = _collect_remote_object_keys('images')
        if remote_refs:
            _append_media_records(location.images, LocationImage, remote_refs)

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
        is_public = request.form.get('is_public') in {'1', 'on', 'true', 'yes'}
        usage_tags = [tag for tag in request.form.getlist('usage_tags') if tag in _LOCATION_USAGE_KEYS]
        description = request.form.get('description') or ''
        access_info = request.form.get('access_info') or ''
        notes_meta = {
            'description': description,
            'usage_tags': usage_tags,
            'access_info': access_info,
            'is_public': is_public
        }
        location.notes = _serialize_location_notes(notes_meta)
        # 多负责人
        member_objs = []
        if responsible_ids:
            member_objs = Member.query.filter(Member.id.in_([int(mid) for mid in responsible_ids])).all()
        if not member_objs and not is_public:
            member_objs = [current_user]
        location.responsible_members = member_objs
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
        flash('空间信息已更新', 'success')
        return redirect(url_for('locations_list'))
    members = Member.query.all()
    parents = Location.query.filter(Location.id != location.id).all()
    return render_template('location_form.html',
                           members=members,
                           location=location,
                           parents=parents,
                           location_meta=current_meta,
                           location_usage_choices=_LOCATION_USAGE_CHOICES)

@app.route('/locations/<int:loc_id>/delete', methods=['POST'])
@login_required
def delete_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    if location.responsible_members and current_user not in location.responsible_members:
        abort(403)
    for fname in set(location.image_filenames):
        remove_uploaded_file(fname)
    db.session.delete(location)
    db.session.commit()
    # 记录日志
    log = Log(user_id=current_user.id, location_id=loc_id, action_type="删除位置", details=f"Deleted location {location.name}")
    db.session.add(log)
    db.session.commit()
    flash('社区空间已删除', 'info')
    return redirect(url_for('locations_list'))

@app.route('/locations/<int:loc_id>')
@login_required
def view_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    # 获取该位置包含的所有物品（多对多）
    items_at_location = sorted(location.items, key=lambda item: item.name.lower())
    migrated = False
    for item in items_at_location:
        if _ensure_item_responsible_members(item):
            migrated = True
    if migrated:
        db.session.commit()
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
        if item.responsible_members:
            for member in item.responsible_members:
                responsible_counter[member.id] += 1
                responsible_map[member.id] = member

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

    events = (
        Event.query
        .join(event_locations)
        .filter(event_locations.c.location_id == location.id)
        .options(
            db.selectinload(Event.owner),
            db.selectinload(Event.participant_links).selectinload(EventParticipant.member)
        )
        .all()
    )
    event_bundle = _build_event_summary(events)

    location_meta, _ = _parse_location_notes(location.notes)
    usage_badges = [
        {'key': tag, 'label': _LOCATION_USAGE_LABELS.get(tag, tag)}
        for tag in location_meta['usage_tags']
    ]
    relation_members = {}
    seen_pairs = set()
    members = Member.query.options(load_only(Member.id, Member.name, Member.username, Member.notes)).all()
    for mem in members:
        meta, _ = _parse_profile_notes(mem.notes)
        for rel in meta['location_relations']:
            if rel.get('location_id') != location.id:
                continue
            relation = rel.get('relation', 'other')
            if relation not in _MEMBER_RELATION_TYPES:
                relation = 'other'
            note = _ensure_string(rel.get('note')).strip()
            identity = (mem.id, relation, note)
            if identity in seen_pairs:
                continue
            seen_pairs.add(identity)
            relation_members.setdefault(relation, []).append({
                'member': mem,
                'note': note
            })
    affiliation_summary = []
    affiliation_total = 0
    for relation, entries in relation_members.items():
        entries.sort(key=lambda entry: ((entry['member'].name or entry['member'].username or '').lower()))
        count = len(entries)
        affiliation_total += count
        affiliation_summary.append({
            'relation': relation,
            'label': _MEMBER_RELATION_TYPES.get(relation, relation),
            'count': count,
            'members': entries
        })
    affiliation_summary.sort(key=lambda entry: (-entry['count'], entry['label']))

    return render_template('location_detail.html', location=location, 
                           items=items_at_location,
                           status_counter=status_counter,
                           status_stats=status_stats,
                           category_stats=category_stats,
                           feature_stats=feature_stats,
                           responsible_stats=responsible_stats,
                           available_items=available_items,
                           event_summary=event_bundle['summary'],
                           ongoing_events=event_bundle['ongoing'],
                           upcoming_events=event_bundle['upcoming'],
                           unscheduled_events=event_bundle['unscheduled'],
                           recent_past_events=event_bundle['recent_past'],
                           past_events_total=event_bundle['past_total'],
                           location_meta=location_meta,
                           location_usage_badges=usage_badges,
                           affiliation_summary=affiliation_summary,
                           affiliation_total=affiliation_total)


@app.route('/locations/<int:loc_id>/items/manage', methods=['POST'])
@login_required
def manage_location_items(loc_id):
    location = Location.query.get_or_404(loc_id)
    if location.responsible_members and current_user not in location.responsible_members:
        abort(403)
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

    is_self = member.id == current_user.id

    # 负责的物品
    all_items = list(member.items)
    legacy_candidates = Item.query.filter_by(responsible_id=member.id).all()
    migrated_items = False
    for legacy_item in legacy_candidates:
        if legacy_item not in all_items:
            if _ensure_item_responsible_members(legacy_item):
                migrated_items = True
    if migrated_items:
        db.session.commit()
        all_items = list(member.items)
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
    if is_self:
        # 只查找该成员负责的物品和位置的日志（通过多对多关系判断负责权）
        notifications = (
            Log.query
            .filter(
                or_(
                    Log.item.has(Item.responsible_members.any(Member.id == member.id)),
                    Log.location.has(Location.responsible_members.any(Member.id == member.id))
                ),
                Log.user_id != member.id
            )
            .order_by(Log.timestamp.desc())
            .limit(5)
            .all()
        )
    member_index, mention_lookup = build_member_lookup()
    profile_notes_html = None
    profile_meta, _ = _parse_profile_notes(member.notes)
    if profile_meta['bio']:
        profile_notes_html = render_rich_text(profile_meta['bio'], mention_lookup)
    feedback_entries = prepare_feedback_entries(member.feedback_log, member_index, mention_lookup)

    affiliation_entries = []
    relation_lookup = dict(_MEMBER_RELATION_TYPES)
    if is_self and profile_meta['location_relations']:
        loc_ids = [entry['location_id'] for entry in profile_meta['location_relations']]
        unique_ids = {lid for lid in loc_ids}
        if unique_ids:
            loc_map = {loc.id: loc for loc in Location.query.filter(Location.id.in_(unique_ids)).all()}
            for entry in profile_meta['location_relations']:
                loc = loc_map.get(entry['location_id'])
                if not loc:
                    continue
                relation = entry.get('relation', 'other')
                if relation not in relation_lookup:
                    relation = 'other'
                affiliation_entries.append({
                    'location': loc,
                    'relation': relation,
                    'relation_label': relation_lookup[relation],
                    'note': entry.get('note', '').strip()
                })

    item_relation_lookup = dict(_MEMBER_ITEM_REL_TYPES)
    interest_entries = []
    if is_self and profile_meta['item_relations']:
        item_ids = [entry['item_id'] for entry in profile_meta['item_relations']]
        unique_item_ids = {iid for iid in item_ids}
        item_map = {}
        if unique_item_ids:
            item_map = {itm.id: itm for itm in Item.query.filter(Item.id.in_(unique_item_ids)).all()}
        for entry in profile_meta['item_relations']:
            itm = item_map.get(entry['item_id'])
            if not itm:
                continue
            relation = entry.get('relation', 'other')
            if relation not in item_relation_lookup:
                relation = 'other'
            interest_entries.append({
                'item': itm,
                'relation': relation,
                'relation_label': item_relation_lookup[relation],
                'note': entry.get('note', '').strip()
            })

    event_relation_lookup = dict(_MEMBER_EVENT_REL_TYPES)
    event_entries = []
    if is_self and profile_meta['event_relations']:
        event_ids = [entry['event_id'] for entry in profile_meta['event_relations']]
        unique_event_ids = {eid for eid in event_ids}
        event_map = {}
        if unique_event_ids:
            event_map = {ev.id: ev for ev in Event.query.filter(Event.id.in_(unique_event_ids)).all()}
        for entry in profile_meta['event_relations']:
            ev = event_map.get(entry['event_id'])
            if not ev:
                continue
            relation = entry.get('relation', 'other')
            if relation not in event_relation_lookup:
                relation = 'other'
            event_entries.append({
                'event': ev,
                'relation': relation,
                'relation_label': event_relation_lookup[relation],
                'note': entry.get('note', '').strip()
            })

    # 当前用户自己的操作记录（仅查看自己的主页时显示）
    user_logs = []
    if is_self:
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
                           is_following=is_following,
                           profile_meta=profile_meta,
                           profile_social_links=profile_meta['social_links'],
                           profile_affiliations=affiliation_entries,
                           profile_interests=interest_entries,
                           profile_events=event_entries,
                           relation_lookup=relation_lookup,
                           item_relation_lookup=item_relation_lookup,
                           event_relation_lookup=event_relation_lookup,
                           is_self=is_self)

@app.route('/member/<int:member_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_profile(member_id):
    if current_user.id != member_id:
        flash('无权编辑他人信息', 'danger')
        return redirect(url_for('profile', member_id=member_id))
    member = Member.query.get_or_404(member_id)
    profile_meta, structured = _parse_profile_notes(member.notes)
    if request.method == 'POST':
        # 更新个人信息
        member.name = request.form.get('name')
        member.contact = request.form.get('contact')
        bio = request.form.get('bio') or ''
        social_labels = request.form.getlist('social_label')
        social_urls = request.form.getlist('social_url')
        social_links = []
        for label, url in zip(social_labels, social_urls):
            url = _ensure_string(url).strip()
            if not url:
                continue
            if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', url):
                if url.startswith('www.'):
                    url = 'https://' + url
            label = _ensure_string(label).strip()
            social_links.append({'label': label, 'url': url})
        aff_loc_ids = request.form.getlist('affiliation_location_id')
        aff_relations = request.form.getlist('affiliation_relation')
        aff_notes = request.form.getlist('affiliation_note')
        location_relations = []
        for loc_id_raw, relation_raw, note_raw in zip(aff_loc_ids, aff_relations, aff_notes):
            try:
                loc_id = int(loc_id_raw)
            except (TypeError, ValueError):
                continue
            relation = _ensure_string(relation_raw).strip()
            if relation not in _MEMBER_RELATION_TYPES:
                relation = 'other'
            note = _ensure_string(note_raw).strip()
            location_relations.append({
                'location_id': loc_id,
                'relation': relation,
                'note': note
            })
        interest_item_ids = request.form.getlist('interest_item_id')
        interest_relations = request.form.getlist('interest_item_relation')
        interest_notes = request.form.getlist('interest_item_note')
        item_relations = []
        for item_id_raw, relation_raw, note_raw in zip(interest_item_ids, interest_relations, interest_notes):
            try:
                item_id = int(item_id_raw)
            except (TypeError, ValueError):
                continue
            relation = _ensure_string(relation_raw).strip()
            if relation not in _MEMBER_ITEM_REL_TYPES:
                relation = 'other'
            note = _ensure_string(note_raw).strip()
            item_relations.append({
                'item_id': item_id,
                'relation': relation,
                'note': note
            })
        event_ids = request.form.getlist('event_relation_event_id')
        event_relations_raw = request.form.getlist('event_relation_relation')
        event_notes = request.form.getlist('event_relation_note')
        event_relations = []
        for event_id_raw, relation_raw, note_raw in zip(event_ids, event_relations_raw, event_notes):
            try:
                event_id = int(event_id_raw)
            except (TypeError, ValueError):
                continue
            relation = _ensure_string(relation_raw).strip()
            if relation not in _MEMBER_EVENT_REL_TYPES:
                relation = 'other'
            note = _ensure_string(note_raw).strip()
            event_relations.append({
                'event_id': event_id,
                'relation': relation,
                'note': note
            })
        profile_payload = {
            'bio': bio,
            'social_links': social_links,
            'location_relations': location_relations,
            'item_relations': item_relations,
            'event_relations': event_relations
        }
        member.notes = _serialize_profile_notes(profile_payload)
        # 如填写了新密码则更新密码
        new_password = request.form.get('password')
        if new_password and new_password.strip() != '':
            member.set_password(new_password)
        # 更新头像
        new_photo_ref = None
        remote_photo_keys = _collect_remote_object_keys('photo')
        if remote_photo_keys:
            new_photo_ref = remote_photo_keys[-1]
        else:
            image_file = request.files.get('photo')
            if image_file and image_file.filename != '' and allowed_file(image_file.filename):
                if determine_media_kind(image_file.filename) != 'image':
                    flash('头像仅支持图片格式，请选择 JPG / PNG 等常见格式。', 'warning')
                else:
                    stored_name = save_uploaded_media(image_file)
                    if stored_name:
                        new_photo_ref = stored_name
        if new_photo_ref:
            if member.photo and member.photo != new_photo_ref:
                remove_uploaded_file(member.photo)
            member.photo = new_photo_ref
        member.last_modified = datetime.utcnow()
        db.session.commit()
        flash('个人信息已更新', 'success')
        return redirect(url_for('profile', member_id=member_id))
    locations = Location.query.order_by(func.lower(Location.name)).all()
    items = Item.query.order_by(func.lower(Item.name)).all()
    events = Event.query.order_by(func.lower(Event.title)).all()
    return render_template('edit_profile.html',
                           member=member,
                           profile_meta=profile_meta,
                           structured_notes=structured,
                           relation_lookup=_MEMBER_RELATION_TYPES,
                           item_relation_lookup=_MEMBER_ITEM_REL_TYPES,
                           event_relation_lookup=_MEMBER_EVENT_REL_TYPES,
                           locations=locations,
                           items=items,
                           events=events)

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


@app.errorhandler(403)
def forbidden(error):
    description = getattr(error, 'description', None) or '无权访问该资源。'
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify({'error': 'forbidden', 'message': description}), 403
    back_url = request.referrer if request.referrer else url_for('index')
    return render_template('error_403.html', description=description, back_url=back_url), 403

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
    def resolve_media_url(ref):
        if not ref:
            return None
        if _is_external_media(ref):
            return ref
        if app.config.get('USE_OSS'):
            prefix = app.config.get('OSS_PREFIX')
            key = ref.lstrip('/')
            looks_like_oss = False
            if prefix and key.startswith(prefix):
                looks_like_oss = True
            elif '/' in key:
                looks_like_oss = True
            else:
                local_path = os.path.join(app.config['UPLOAD_FOLDER'], ref)
                looks_like_oss = not os.path.exists(local_path)
            if looks_like_oss:
                return _build_oss_url(key)
        return url_for('uploaded_file', filename=ref)

    def media_display_name(ref):
        if not ref:
            return ''
        token = str(ref)
        token = token.split('?', 1)[0]
        token = token.split('#', 1)[0]
        return os.path.basename(token)

    def _build_media_entries(sources):
        entries = []
        seen = set()
        for fname in sources or []:
            if not fname or fname in seen:
                continue
            seen.add(fname)
            resolved = resolve_media_url(fname)
            if not resolved:
                continue
            entries.append({
                'url': resolved,
                'kind': determine_media_kind(fname),
                'filename': fname,
                'display_name': media_display_name(fname),
                'is_remote': _is_external_media(fname)
            })
        return entries

    def item_media_entries(item):
        if not item:
            return []
        return _build_media_entries(getattr(item, 'image_filenames', []) or [])

    def location_media_entries(location):
        if not location:
            return []
        return _build_media_entries(getattr(location, 'image_filenames', []) or [])

    def event_media_entries(event):
        if not event:
            return []
        filenames = []
        for img in getattr(event, 'images', []) or []:
            if img.filename:
                filenames.append(img.filename)
        return _build_media_entries(filenames)

    def uploaded_media_url(filename):
        return resolve_media_url(filename)

    def item_image_urls(item):
        return [entry['url'] for entry in item_media_entries(item) if entry['kind'] == 'image']

    def location_image_urls(location):
        return [entry['url'] for entry in location_media_entries(location) if entry['kind'] == 'image']

    def uploaded_image_url(filename):
        return uploaded_media_url(filename)

    return dict(
        item_media_entries=item_media_entries,
        location_media_entries=location_media_entries,
        event_media_entries=event_media_entries,
        uploaded_media_url=uploaded_media_url,
        item_image_urls=item_image_urls,
        location_image_urls=location_image_urls,
        uploaded_image_url=uploaded_image_url,
        media_kind=determine_media_kind,
        media_kind_labels=MEDIA_KIND_LABELS,
        media_display_name=media_display_name,
        direct_upload_config=_build_direct_upload_config(),
        feature_intent=_feature_intent,
        stock_status_intent=_stock_status_intent,
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
