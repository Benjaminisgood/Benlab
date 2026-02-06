import os
import glob
import tempfile
import zipfile
import threading
import time
import base64
import mimetypes
import hashlib
from datetime import datetime, timedelta, timezone
import re
import json
from io import BytesIO
import urllib.request
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, send_from_directory, abort, jsonify, after_this_request
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


def _normalize_base_url(value, default_scheme='https'):
    """Normalize domain/CNAME inputs to absolute URLs with scheme, keep explicit http."""
    if not value:
        return ''
    token = str(value).strip()
    if not token:
        return ''
    if token.startswith('//'):
        token = f"{default_scheme}:{token}"
    elif not token.lower().startswith(('http://', 'https://')):
        token = f"{default_scheme}://{token}"
    token = token.rstrip('/')
    return token


def _parse_env_flag(value, default=False):
    """Return a boolean from environment-style values."""
    if value is None:
        return default
    return str(value).strip().lower() not in {'0', 'false', 'no', 'off'}


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
attachments_root = (os.getenv('ATTACHMENTS_FOLDER') or os.getenv('ATTACHMENTS_DIR') or '').strip()
if not attachments_root:
    attachments_root = os.path.join(BASE_DIR, 'attachments')
app.config['ATTACHMENTS_FOLDER'] = attachments_root
TEMP_PAGE_DIR = app.instance_path
# 确保运行所需的实例目录存在
os.makedirs(TEMP_PAGE_DIR, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 2500 * 1024 * 1024  # 限制上传2500MB以内的文件
OSS_ENDPOINT = _normalize_base_url(os.getenv('ALIYUN_OSS_ENDPOINT'))
OSS_ACCESS_KEY_ID = os.getenv('ALIYUN_OSS_ACCESS_KEY_ID')
OSS_ACCESS_KEY_SECRET = os.getenv('ALIYUN_OSS_ACCESS_KEY_SECRET')
OSS_BUCKET_NAME = os.getenv('ALIYUN_OSS_BUCKET')
OSS_PREFIX = (os.getenv('ALIYUN_OSS_PREFIX') or '').strip('/ ')
OSS_PUBLIC_BASE_URL = _normalize_base_url(os.getenv('ALIYUN_OSS_PUBLIC_BASE_URL'))
OSS_ASSUME_PUBLIC = _parse_env_flag(os.getenv('ALIYUN_OSS_ASSUME_PUBLIC'), default=False)
_oss_env_ready = bool(OSS_ENDPOINT and OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET and OSS_BUCKET_NAME)
if not _oss_env_ready:
    raise RuntimeError('OSS 配置缺失，请检查环境变量。')
if not oss2:
    raise RuntimeError('oss2 library not available but OSS is enabled.')
USE_OSS = True
app.config['USE_OSS'] = USE_OSS
app.config['OSS_ENDPOINT'] = OSS_ENDPOINT
app.config['OSS_ACCESS_KEY_ID'] = OSS_ACCESS_KEY_ID
app.config['OSS_ACCESS_KEY_SECRET'] = OSS_ACCESS_KEY_SECRET
app.config['OSS_BUCKET'] = OSS_BUCKET_NAME
app.config['OSS_PREFIX'] = OSS_PREFIX
app.config['OSS_PUBLIC_BASE_URL'] = OSS_PUBLIC_BASE_URL
app.config['OSS_ASSUME_PUBLIC'] = OSS_ASSUME_PUBLIC
try:
    direct_upload_expiration = int(os.getenv('DIRECT_UPLOAD_URL_EXPIRATION', '900'))
except (TypeError, ValueError):
    direct_upload_expiration = 900
direct_upload_enabled = _parse_env_flag(os.getenv('DIRECT_OSS_UPLOAD_ENABLED'), default=True)
app.config['DIRECT_OSS_UPLOAD_ENABLED'] = bool(USE_OSS and direct_upload_enabled)
app.config['DIRECT_OSS_UPLOAD_VALIDATE_CORS'] = _parse_env_flag(
    os.getenv('DIRECT_OSS_UPLOAD_VALIDATE_CORS'),
    default=True
)
app.config['DIRECT_UPLOAD_URL_EXPIRATION'] = max(60, direct_upload_expiration)
app.config['ATTACHMENTS_SYNC_ON_START'] = _parse_env_flag(
    os.getenv('ATTACHMENTS_SYNC_ON_START'),
    default=True
)
app.config['ATTACHMENTS_CLEANUP_ON_START'] = _parse_env_flag(
    os.getenv('ATTACHMENTS_CLEANUP_ON_START'),
    default=True
)
try:
    cleanup_grace_seconds = int(os.getenv('ATTACHMENTS_CLEANUP_GRACE_SECONDS', '86400'))
except (TypeError, ValueError):
    cleanup_grace_seconds = 86400
app.config['ATTACHMENTS_CLEANUP_GRACE_SECONDS'] = max(0, cleanup_grace_seconds)
db_backup_source = (os.getenv('DB_BACKUP_SOURCE_PATH') or '').strip()
if not db_backup_source:
    db_backup_source = os.path.join(app.instance_path, 'lab.db')
app.config['DB_BACKUP_SOURCE_PATH'] = db_backup_source
app.config['DB_BACKUP_ON_START'] = _parse_env_flag(
    os.getenv('DB_BACKUP_ON_START'),
    default=True
)
try:
    db_backup_interval = int(os.getenv('DB_BACKUP_INTERVAL_SECONDS', '0'))
except (TypeError, ValueError):
    db_backup_interval = 0
app.config['DB_BACKUP_INTERVAL_SECONDS'] = max(0, db_backup_interval)
db_backup_prefix = (os.getenv('DB_BACKUP_PREFIX') or 'db-backups').strip('/ ')
app.config['DB_BACKUP_PREFIX'] = db_backup_prefix
try:
    db_backup_retention_days = int(os.getenv('DB_BACKUP_RETENTION_DAYS', '0'))
except (TypeError, ValueError):
    db_backup_retention_days = 0
app.config['DB_BACKUP_RETENTION_DAYS'] = max(0, db_backup_retention_days)
REMOTE_FIELD_SUFFIX = '_remote_keys'
_oss_bucket = None
_db_backup_lock = threading.Lock()

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
    image_entry = next(
        (att for att in (event.attachments or []) if att.filename and determine_media_kind(att.filename) == 'image'),
        None
    )
    if not image_entry:
        return None
    data = _read_media_bytes(image_entry.filename, timeout=5)
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
_ITEM_STOCK_STATUS_CHOICES = ('正常', '少量', '用完', '借出', '舍弃')
_ITEM_STOCK_STATUS_INTENTS = {
    '正常': 'positive',
    '少量': 'warning',
    '用完': 'critical',
    '借出': 'info',
    '舍弃': 'muted'
}
_ITEM_ALERT_STOCK_STATUSES = ('用完', '舍弃', '少量', '借出')
_ITEM_ALERT_STATUS_PRIORITY = {
    status: idx for idx, status in enumerate(_ITEM_ALERT_STOCK_STATUSES)
}
_ITEM_ALERT_LEVELS = {
    '用完': 'danger',
    '舍弃': 'warning',
    '少量': 'warning',
    '借出': 'warning'
}
_ITEM_ALERT_ACTION_LABELS = {
    '用完': '补货处理',
    '舍弃': '弃置处理',
    '少量': '补货处理',
    '借出': '借出跟进'
}
_ITEM_ALERT_MESSAGE_TEMPLATES = {
    '用完': '你有 {count} 个库存用完的物品，请立即补货！',
    '舍弃': '你有 {count} 个标记为舍弃的物品，请尽快完成弃置处理。',
    '少量': '你有 {count} 个库存少量的物品，建议尽快补货。',
    '借出': '你有 {count} 个借出中的物品，请及时跟进归还。'
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
    ('storage', '储物空间'),
    ('travel', '旅游推荐'),
    ('residence', '生活社区'),
    ('other', '其他')
]
_LOCATION_USAGE_KEYS = {key for key, _ in _LOCATION_USAGE_CHOICES}
_LOCATION_USAGE_LABELS = dict(_LOCATION_USAGE_CHOICES)
_LOCATION_USAGE_LABEL_TO_KEY = {label: key for key, label in _LOCATION_USAGE_CHOICES}
_LOCATION_USAGE_REF_LABEL = '用途'
_LOCATION_STATUS_CHOICES = ('正常', '脏', '报修', '危险', '禁止')
_LOCATION_STATUS_INTENTS = {
    '正常': 'positive',
    '禁止': 'neutral'
}
_LOCATION_DIRTY_STATUS_VALUES = {'脏', '报修', '危险'}
_LOCATION_ALERT_STATUSES = ('危险', '报修', '脏')
_LOCATION_ALERT_STATUS_PRIORITY = {
    status: idx for idx, status in enumerate(_LOCATION_ALERT_STATUSES)
}
_LOCATION_ALERT_LEVELS = {
    '危险': 'danger',
    '报修': 'warning',
    '脏': 'warning'
}
_LOCATION_ALERT_ACTION_LABELS = {
    '危险': '立即隔离',
    '报修': '安排报修',
    '脏': '清洁处理'
}
_LOCATION_ALERT_MESSAGE_TEMPLATES = {
    '危险': '你有 {count} 个危险状态的位置，请立即处理并限制使用。',
    '报修': '你有 {count} 个报修状态的位置，请尽快安排维修。',
    '脏': '你有 {count} 个脏状态的位置，请及时清洁。'
}


def _ensure_string(value):
    return value if isinstance(value, str) else ''


def _parse_item_detail_refs(raw):
    """Return normalized list of {'label','value'} dicts from stored string/list."""
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
                    label, value = token.split('|||', 1)
                elif '|' in token:
                    label, value = token.split('|', 1)
                else:
                    label, value = '', token
                label = label.strip()
                value = value.strip()
                if value:
                    entries.append({'label': label, 'value': value})
            if entries:
                return entries
            chunks = [part.strip() for part in re.split(r'[\n,]', raw) if part.strip()]
            data = [{'label': '', 'value': chunk} for chunk in chunks]
    entries = []
    if isinstance(data, dict) and ('value' in data or 'url' in data):
        data = [data]
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, str):
                value = entry.strip()
                if value:
                    entries.append({'label': '', 'value': value})
            elif isinstance(entry, dict):
                value = _ensure_string(entry.get('value') or entry.get('url')).strip()
                label = _ensure_string(entry.get('label')).strip()
                if value:
                    entries.append({'label': label, 'value': value})
    # 去重，保留顺序
    seen = set()
    normalized = []
    for entry in entries:
        key = entry['value']
        if key not in seen:
            normalized.append(entry)
            seen.add(key)
    return normalized


def _serialize_item_detail_refs(entries, max_length=None):
    """Serialize list of {'label','value'} dicts into compact string."""
    if not entries:
        return None, False
    payload = []
    seen = set()
    for entry in entries:
        label = ''
        value = ''
        if isinstance(entry, dict):
            value = _ensure_string(entry.get('value') or entry.get('url')).strip()
            label = _ensure_string(entry.get('label')).strip()
        elif isinstance(entry, str):
            value = entry.strip()
        if not value or value in seen:
            continue
        payload.append({'label': label, 'value': value})
        seen.add(value)
    trimmed = False
    while payload:
        lines = []
        for entry in payload:
            label = entry['label'].replace('\n', ' ').strip()
            value = entry['value'].replace('\n', ' ').strip()
            token = f"{label}|||{value}" if label else value
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


def _normalize_item_stock_status(value):
    """Return normalized item stock status, or None when empty/invalid."""
    status = _ensure_string(value).strip()
    if not status:
        return None
    return status if status in _ITEM_STOCK_STATUS_CHOICES else None


def _is_item_alert_status(value):
    """Return whether item stock status should trigger reminder."""
    status = _normalize_item_stock_status(value)
    return status in _ITEM_ALERT_STOCK_STATUSES


def _item_alert_level(value):
    status = _normalize_item_stock_status(value)
    return _ITEM_ALERT_LEVELS.get(status, 'warning')


def _item_alert_action_label(value):
    status = _normalize_item_stock_status(value)
    return _ITEM_ALERT_ACTION_LABELS.get(status, '处理')


def _item_alert_message(value, count):
    status = _normalize_item_stock_status(value)
    template = _ITEM_ALERT_MESSAGE_TEMPLATES.get(status)
    if not template:
        return ''
    return template.format(count=count)


def _stock_status_intent(value):
    """Return semantic intent token for inventory stock status badges/rows."""
    status = _normalize_item_stock_status(value)
    if not status:
        return 'neutral'
    return _ITEM_STOCK_STATUS_INTENTS.get(status, 'neutral')


def _normalize_location_status(value):
    """Return normalized location status, or None when empty/invalid."""
    status = _ensure_string(value).strip()
    if not status:
        return None
    return status if status in _LOCATION_STATUS_CHOICES else None


def _is_location_dirty_status(value):
    """Return whether location status should be treated as critical."""
    normalized = _normalize_location_status(value)
    return normalized in _LOCATION_DIRTY_STATUS_VALUES


def _is_location_alert_status(value):
    """Return whether location status should trigger reminder."""
    status = _normalize_location_status(value)
    return status in _LOCATION_ALERT_STATUSES


def _location_alert_level(value):
    status = _normalize_location_status(value)
    return _LOCATION_ALERT_LEVELS.get(status, 'warning')


def _location_alert_action_label(value):
    status = _normalize_location_status(value)
    return _LOCATION_ALERT_ACTION_LABELS.get(status, '处理')


def _location_alert_message(value, count):
    status = _normalize_location_status(value)
    template = _LOCATION_ALERT_MESSAGE_TEMPLATES.get(status)
    if not template:
        return ''
    return template.format(count=count)


def _location_status_intent(value):
    """Return semantic intent token for location status badges/rows."""
    normalized = _normalize_location_status(value)
    if not normalized:
        return 'neutral'
    if normalized in _LOCATION_DIRTY_STATUS_VALUES:
        return 'critical'
    return _LOCATION_STATUS_INTENTS.get(normalized, 'neutral')


def _collect_detail_refs_from_form(form):
    labels = form.getlist('detail_ref_label')
    values = form.getlist('detail_ref_value')
    if not labels and not values:
        labels = form.getlist('detail_link_label')
        values = form.getlist('detail_link_url')
    entries = []
    for label, value in zip(labels, values):
        value = _ensure_string(value).strip()
        label = _ensure_string(label).strip()
        if value:
            entries.append({'label': label, 'value': value})
    if entries:
        return entries
    fallback = form.get('detail_ref_text') or form.get('detail_link_text')
    if fallback:
        return _parse_item_detail_refs(fallback)
    return []


def _extract_usage_tags_from_detail_refs(entries):
    tags = []
    seen = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        label = _ensure_string(entry.get('label')).strip()
        value = _ensure_string(entry.get('value')).strip()
        if label != _LOCATION_USAGE_REF_LABEL or not value:
            continue
        key = _LOCATION_USAGE_LABEL_TO_KEY.get(value)
        if key and key not in seen:
            tags.append(key)
            seen.add(key)
    return tags


def _strip_usage_tag_refs(entries):
    cleaned = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        label = _ensure_string(entry.get('label')).strip()
        value = _ensure_string(entry.get('value')).strip()
        if label == _LOCATION_USAGE_REF_LABEL and value in _LOCATION_USAGE_LABEL_TO_KEY:
            continue
        if value:
            cleaned.append({'label': label, 'value': value})
    return cleaned


def _merge_usage_tags_into_detail_refs(entries, usage_tags):
    merged = _strip_usage_tag_refs(entries)
    seen = {entry['value'] for entry in merged if entry.get('value')}
    for tag in usage_tags or []:
        if tag not in _LOCATION_USAGE_KEYS:
            continue
        value = _LOCATION_USAGE_LABELS.get(tag)
        if not value or value in seen:
            continue
        merged.append({'label': _LOCATION_USAGE_REF_LABEL, 'value': value})
        seen.add(value)
    return merged


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


def _oss_direct_upload_ready():
    """Return whether browser direct-upload should stay enabled."""
    if not app.config.get('DIRECT_OSS_UPLOAD_ENABLED'):
        return False
    if not app.config.get('USE_OSS'):
        return False
    if not app.config.get('DIRECT_OSS_UPLOAD_VALIDATE_CORS', True):
        return True
    bucket = _get_oss_bucket()
    if bucket is None:
        return False

    cors_info = None
    last_exc = None
    for attempt in range(3):
        try:
            cors_info = bucket.get_bucket_cors()
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(0.4 * (attempt + 1))
    if last_exc is not None or cors_info is None:
        app.logger.warning('读取 OSS CORS 配置失败，已关闭浏览器直传并回退为服务端上传: %s', last_exc)
        return False

    rules = getattr(cors_info, 'cors_rule_list', None)
    if rules is None:
        rules = getattr(cors_info, 'rules', None)
    rules = rules or []

    def normalize_seq(value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        try:
            return list(value)
        except TypeError:
            return [value]

    for rule in rules:
        raw_methods = getattr(rule, 'allowed_methods', None)
        if isinstance(raw_methods, str):
            allowed_methods = [part.strip().upper() for part in raw_methods.split(',') if part.strip()]
        else:
            allowed_methods = [str(method).strip().upper() for method in normalize_seq(raw_methods) if str(method).strip()]

        raw_origins = getattr(rule, 'allowed_origins', None)
        if isinstance(raw_origins, str):
            allowed_origins = [part.strip() for part in raw_origins.split(',') if part.strip()]
        else:
            allowed_origins = [str(origin).strip() for origin in normalize_seq(raw_origins) if str(origin).strip()]

        if 'PUT' in allowed_methods and any(allowed_origins):
            return True
    app.logger.warning('OSS CORS 未配置 PUT 规则，已自动关闭浏览器直传并回退为服务端上传。')
    return False


def _attachment_storage_roots():
    attachments_root = app.config.get('ATTACHMENTS_FOLDER')
    return [attachments_root] if attachments_root else []


def _attachment_ref_candidates(ref, prefer_prefix=False):
    if not ref or _is_external_media(ref):
        return []
    token = str(ref).strip().replace('\\', '/')
    token = token.lstrip('/')
    if not token:
        return []
    prefix = (app.config.get('OSS_PREFIX') or '').strip('/ ')
    candidates = []
    if not prefix:
        candidates.append(token)
        return candidates
    prefix_token = f"{prefix}/"
    if token.startswith(prefix_token):
        candidates.append(token)
        stripped = token[len(prefix_token):]
        if stripped:
            candidates.append(stripped)
        return candidates
    if prefer_prefix:
        candidates.append(f"{prefix}/{token}")
        candidates.append(token)
    else:
        candidates.append(token)
        candidates.append(f"{prefix}/{token}")
    return candidates


def _local_attachment_root():
    root = app.config.get('ATTACHMENTS_FOLDER')
    if root and os.path.isdir(root):
        return root
    return None


def _ensure_local_attachment_root():
    root = app.config.get('ATTACHMENTS_FOLDER')
    if not root:
        return None
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        return None
    return root


def _safe_attachment_path(root, ref):
    if not root or not ref:
        return None
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, ref))
    if not candidate.startswith(root_abs):
        return None
    return candidate


def _find_local_attachment(ref):
    if not ref:
        return None
    for root in _attachment_storage_roots():
        if not root or not os.path.isdir(root):
            continue
        for candidate_ref in _attachment_ref_candidates(ref):
            candidate = _safe_attachment_path(root, candidate_ref)
            if candidate and os.path.exists(candidate):
                return candidate
    return None


def _resolve_local_attachment_ref(ref):
    if not ref:
        return None, None
    for root in _attachment_storage_roots():
        if not root or not os.path.isdir(root):
            continue
        for candidate_ref in _attachment_ref_candidates(ref):
            candidate = _safe_attachment_path(root, candidate_ref)
            if candidate and os.path.exists(candidate):
                root_abs = os.path.abspath(root)
                rel = os.path.relpath(candidate, root_abs)
                return root_abs, rel
    return None, None


def _local_attachment_exists(ref):
    return bool(_find_local_attachment(ref))


def _generate_stored_filename(original_name):
    sanitized = secure_filename(original_name or '')
    if not sanitized:
        sanitized = 'upload'
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    return f"{timestamp}_{sanitized}"


def save_uploaded_media(file_storage):
    """Persist an uploaded media file and return the stored object key."""
    if not file_storage or file_storage.filename == '':
        return None
    if not allowed_file(file_storage.filename):
        return None
    stored_name = _generate_stored_filename(file_storage.filename)
    if app.config.get('USE_OSS'):
        bucket = _get_oss_bucket()
        if bucket:
            prefix = app.config.get('OSS_PREFIX')
            object_key = f"{prefix}/{stored_name}" if prefix else stored_name
            file_storage.stream.seek(0)
            try:
                bucket.put_object(object_key, file_storage.stream.read())
                return object_key
            except Exception as exc:
                # Keep write path available when OSS has transient failures.
                app.logger.warning('OSS 上传失败，回退本地存储: %s', exc)
                try:
                    file_storage.stream.seek(0)
                except Exception:
                    pass
    attachments_root = app.config.get('ATTACHMENTS_FOLDER')
    if not attachments_root:
        return None
    os.makedirs(attachments_root, exist_ok=True)
    filepath = os.path.join(attachments_root, stored_name)
    file_storage.save(filepath)
    return stored_name


def remove_uploaded_file(filename):
    """Delete a previously saved attachment file if it still exists."""
    if not filename or _is_external_media(filename):
        return
    for root in _attachment_storage_roots():
        if not root or not os.path.isdir(root):
            continue
        local_path = _safe_attachment_path(root, filename)
        if not local_path or not os.path.exists(local_path):
            continue
        try:
            os.remove(local_path)
        except OSError:
            pass
    if not app.config.get('USE_OSS'):
        return
    bucket = _get_oss_bucket()
    if not bucket:
        return
    key = filename
    try:
        bucket.delete_object(key)
    except oss2.exceptions.NoSuchKey:  # type: ignore[attr-defined]
        pass
    except Exception:
        pass


def _sign_oss_get_url(key):
    if not key:
        return None
    bucket = _get_oss_bucket()
    if not bucket:
        return None
    expiry = app.config.get('DIRECT_UPLOAD_URL_EXPIRATION', 900)
    try:
        expires_in = int(expiry)
    except (TypeError, ValueError):
        expires_in = 900
    expires_in = max(60, expires_in)
    try:
        signed_url = bucket.sign_url('GET', key, expires_in)
    except Exception:
        return None
    return _finalize_signed_upload_url(signed_url)


def _build_oss_url(key):
    key = key.lstrip('/')
    assume_public = bool(app.config.get('OSS_ASSUME_PUBLIC'))
    public_base = app.config.get('OSS_PUBLIC_BASE_URL')
    if assume_public and public_base:
        return f"{public_base}/{key}"
    if not assume_public:
        signed = _sign_oss_get_url(key)
        if signed:
            return signed
    endpoint = app.config.get('OSS_ENDPOINT', '')
    endpoint = endpoint.replace('https://', '').replace('http://', '')
    bucket = app.config.get('OSS_BUCKET')
    return f"https://{bucket}.{endpoint}/{key}"


def _normalize_external_url(ref):
    if not ref:
        return None
    if ref.startswith('//'):
        return f"https:{ref}"
    return ref


def _read_media_bytes(ref, timeout=10):
    """Load media bytes from local storage, OSS, or an external URL."""
    if not ref:
        return None
    url = None
    if _is_external_media(ref):
        url = _normalize_external_url(ref)
    else:
        local_path = _find_local_attachment(ref)
        if local_path:
            try:
                with open(local_path, 'rb') as fh:
                    return fh.read()
            except OSError:
                return None
        if app.config.get('USE_OSS'):
            candidates = _attachment_ref_candidates(ref, prefer_prefix=True)
            key = candidates[0] if candidates else ref
            url = _build_oss_url(key)
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except (URLError, HTTPError, OSError, ValueError):
        return None


def _iter_oss_objects(prefix=None):
    bucket = _get_oss_bucket()
    if not bucket:
        return
    marker = ''
    while True:
        try:
            result = bucket.list_objects(prefix=prefix or None, marker=marker, max_keys=1000)
        except Exception as exc:
            app.logger.warning('列举 OSS 对象失败，跳过本轮任务: %s', exc)
            break
        for obj in result.object_list or []:
            yield obj
        if getattr(result, 'is_truncated', False):
            marker = getattr(result, 'next_marker', '')
            if not marker:
                break
        else:
            break


def _sync_oss_attachments_to_local():
    if not app.config.get('USE_OSS'):
        return {'downloaded': 0, 'skipped': 0}
    attachments_root = _ensure_local_attachment_root()
    if not attachments_root:
        return {'downloaded': 0, 'skipped': 0}
    bucket = _get_oss_bucket()
    if not bucket:
        return {'downloaded': 0, 'skipped': 0}
    prefix = app.config.get('OSS_PREFIX') or None
    if prefix:
        prefix = f"{prefix}/"
    downloaded = 0
    skipped = 0
    root_abs = os.path.abspath(attachments_root)
    for obj in _iter_oss_objects(prefix=prefix):
        key = getattr(obj, 'key', None)
        if not key or key.endswith('/'):
            continue
        local_path = _safe_attachment_path(root_abs, key)
        if not local_path:
            continue
        expected_size = getattr(obj, 'size', None)
        if os.path.exists(local_path) and expected_size is not None:
            try:
                if os.path.getsize(local_path) == expected_size:
                    skipped += 1
                    continue
            except OSError:
                pass
        local_dir = os.path.dirname(local_path)
        if local_dir and not os.path.isdir(local_dir):
            os.makedirs(local_dir, exist_ok=True)
        try:
            bucket.get_object_to_file(key, local_path)
            downloaded += 1
        except Exception:
            continue
    return {'downloaded': downloaded, 'skipped': skipped}


def _sync_attachments_async(refs):
    if not refs:
        return
    if not app.config.get('USE_OSS'):
        return
    if not _ensure_local_attachment_root():
        return
    unique_refs = []
    seen = set()
    for ref in refs:
        if not ref or _is_external_media(ref):
            continue
        for candidate in _attachment_ref_candidates(ref, prefer_prefix=True):
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            unique_refs.append(candidate)
    if not unique_refs:
        return

    def runner():
        root = _ensure_local_attachment_root()
        if not root:
            return
        bucket = _get_oss_bucket()
        if not bucket:
            return
        root_abs = os.path.abspath(root)
        for key in unique_refs:
            local_path = _safe_attachment_path(root_abs, key)
            if not local_path or os.path.exists(local_path):
                continue
            local_dir = os.path.dirname(local_path)
            if local_dir and not os.path.isdir(local_dir):
                os.makedirs(local_dir, exist_ok=True)
            try:
                bucket.get_object_to_file(key, local_path)
            except Exception:
                continue

    thread = threading.Thread(target=runner, name='attachment-sync-on-demand', daemon=True)
    thread.start()


def _collect_referenced_attachment_keys():
    refs = set()

    def add_ref(ref):
        if ref and not _is_external_media(ref):
            refs.add(ref)

    for (value,) in db.session.query(Member.photo).filter(Member.photo.isnot(None)):
        add_ref(value)
    for (value,) in db.session.query(Item.primary_attachment).filter(Item.primary_attachment.isnot(None)):
        add_ref(value)
    for (value,) in db.session.query(Location.primary_attachment).filter(Location.primary_attachment.isnot(None)):
        add_ref(value)
    for (value,) in db.session.query(ItemAttachment.filename):
        add_ref(value)
    for (value,) in db.session.query(LocationAttachment.filename):
        add_ref(value)
    for (value,) in db.session.query(EventAttachment.filename):
        add_ref(value)
    return refs


def _cleanup_local_attachments(referenced, grace_seconds=0):
    removed = 0
    scanned = 0
    now = time.time()
    for root in _attachment_storage_roots():
        if not root or not os.path.isdir(root):
            continue
        root_abs = os.path.abspath(root)
        for base, _, files in os.walk(root_abs):
            for fname in files:
                scanned += 1
                full_path = os.path.join(base, fname)
                rel_path = os.path.relpath(full_path, root_abs)
                if rel_path in referenced:
                    continue
                if grace_seconds:
                    try:
                        if now - os.path.getmtime(full_path) < grace_seconds:
                            continue
                    except OSError:
                        pass
                try:
                    os.remove(full_path)
                    removed += 1
                except OSError:
                    pass
        for base, dirs, files in os.walk(root_abs, topdown=False):
            if dirs or files:
                continue
            try:
                os.rmdir(base)
            except OSError:
                pass
    return {'removed': removed, 'scanned': scanned}


def _cleanup_oss_attachments(referenced, grace_seconds=0):
    if not app.config.get('USE_OSS'):
        return {'removed': 0, 'scanned': 0}
    bucket = _get_oss_bucket()
    if not bucket:
        return {'removed': 0, 'scanned': 0}
    prefix = app.config.get('OSS_PREFIX') or None
    if prefix:
        prefix = f"{prefix}/"
    removed = 0
    scanned = 0
    now = time.time()
    for obj in _iter_oss_objects(prefix=prefix):
        key = getattr(obj, 'key', None)
        if not key or key.endswith('/'):
            continue
        scanned += 1
        if key in referenced:
            continue
        if grace_seconds:
            last_modified = getattr(obj, 'last_modified', None)
            if isinstance(last_modified, (int, float)):
                if now - last_modified < grace_seconds:
                    continue
            elif isinstance(last_modified, datetime):
                delta = datetime.utcnow() - last_modified.replace(tzinfo=None)
                if delta.total_seconds() < grace_seconds:
                    continue
        try:
            bucket.delete_object(key)
            removed += 1
        except oss2.exceptions.NoSuchKey:  # type: ignore[attr-defined]
            pass
        except Exception:
            continue
    return {'removed': removed, 'scanned': scanned}


def _cleanup_orphaned_attachments():
    referenced = _collect_referenced_attachment_keys()
    grace_seconds = app.config.get('ATTACHMENTS_CLEANUP_GRACE_SECONDS', 0)
    local_stats = _cleanup_local_attachments(referenced, grace_seconds=grace_seconds)
    oss_stats = _cleanup_oss_attachments(referenced, grace_seconds=grace_seconds)
    return {'local': local_stats, 'oss': oss_stats}


def _start_attachment_housekeeping():
    sync_enabled = bool(app.config.get('ATTACHMENTS_SYNC_ON_START'))
    cleanup_enabled = bool(app.config.get('ATTACHMENTS_CLEANUP_ON_START'))
    if not sync_enabled and not cleanup_enabled:
        return
    if os.environ.get('FLASK_DEBUG') == '1' and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return

    def runner():
        with app.app_context():
            if sync_enabled and _local_attachment_root():
                try:
                    _sync_oss_attachments_to_local()
                except Exception as exc:
                    app.logger.warning('附件同步任务失败: %s', exc)
            if cleanup_enabled:
                try:
                    _cleanup_orphaned_attachments()
                except Exception as exc:
                    app.logger.warning('附件清理任务失败: %s', exc)

    thread = threading.Thread(target=runner, name='attachment-housekeeping', daemon=True)
    thread.start()


def _resolve_db_backup_source_path():
    source = app.config.get('DB_BACKUP_SOURCE_PATH') or ''
    source = source.strip()
    if not source:
        source = os.path.join(app.instance_path, 'lab.db')
    source = os.path.abspath(source)
    if not os.path.isfile(source):
        return None
    return source


def _db_backup_object_key(source_path):
    prefix = (app.config.get('DB_BACKUP_PREFIX') or '').strip('/ ')
    base = os.path.basename(source_path) or 'lab.db'
    name, ext = os.path.splitext(base)
    if not ext:
        ext = '.db'
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f"{name}-{timestamp}{ext}"
    parts = []
    oss_prefix = (app.config.get('OSS_PREFIX') or '').strip('/ ')
    if oss_prefix:
        parts.append(oss_prefix)
    if prefix:
        parts.append(prefix)
    if parts:
        return '/'.join(parts + [filename])
    return filename


def _db_backup_list_prefix():
    prefix = (app.config.get('DB_BACKUP_PREFIX') or '').strip('/ ')
    oss_prefix = (app.config.get('OSS_PREFIX') or '').strip('/ ')
    parts = [part for part in (oss_prefix, prefix) if part]
    if not parts:
        return None
    return '/'.join(parts) + '/'


def _snapshot_sqlite_database(source_path):
    if not source_path or not os.path.isfile(source_path):
        return None
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    snapshot_path = temp_file.name
    temp_file.close()
    source_conn = None
    dest_conn = None
    try:
        source_conn = sqlite3.connect(source_path, timeout=30)
        dest_conn = sqlite3.connect(snapshot_path, timeout=30)
        source_conn.backup(dest_conn)
        return snapshot_path
    except Exception:
        try:
            os.remove(snapshot_path)
        except OSError:
            pass
        return None
    finally:
        if dest_conn:
            dest_conn.close()
        if source_conn:
            source_conn.close()


def _upload_db_backup_to_oss(snapshot_path, source_path):
    if not snapshot_path or not app.config.get('USE_OSS'):
        return None
    bucket = _get_oss_bucket()
    if not bucket:
        return None
    object_key = _db_backup_object_key(source_path)
    try:
        bucket.put_object_from_file(object_key, snapshot_path)
    except Exception:
        return None
    return object_key


def _cleanup_oss_db_backups(retention_days):
    if retention_days <= 0 or not app.config.get('USE_OSS'):
        return {'removed': 0, 'scanned': 0}
    bucket = _get_oss_bucket()
    if not bucket:
        return {'removed': 0, 'scanned': 0}
    prefix = _db_backup_list_prefix()
    if not prefix:
        return {'removed': 0, 'scanned': 0}
    cutoff_ts = time.time() - (retention_days * 86400)
    removed = 0
    scanned = 0
    for obj in _iter_oss_objects(prefix=prefix):
        key = getattr(obj, 'key', None)
        if not key or key.endswith('/'):
            continue
        scanned += 1
        last_modified = getattr(obj, 'last_modified', None)
        if isinstance(last_modified, datetime):
            last_ts = last_modified.replace(tzinfo=None).timestamp()
        elif isinstance(last_modified, (int, float)):
            last_ts = float(last_modified)
        else:
            continue
        if last_ts >= cutoff_ts:
            continue
        try:
            bucket.delete_object(key)
            removed += 1
        except Exception:
            continue
    return {'removed': removed, 'scanned': scanned}


def _backup_instance_database():
    if not app.config.get('USE_OSS'):
        return None
    source_path = _resolve_db_backup_source_path()
    if not source_path:
        return None
    if not _db_backup_lock.acquire(blocking=False):
        return None
    snapshot_path = None
    try:
        snapshot_path = _snapshot_sqlite_database(source_path)
        if not snapshot_path:
            return None
        object_key = _upload_db_backup_to_oss(snapshot_path, source_path)
        retention_days = app.config.get('DB_BACKUP_RETENTION_DAYS', 0)
        if object_key and retention_days:
            _cleanup_oss_db_backups(retention_days)
        return object_key
    except Exception:
        return None
    finally:
        if snapshot_path:
            try:
                os.remove(snapshot_path)
            except OSError:
                pass
        _db_backup_lock.release()


def _start_db_backup_worker():
    backup_on_start = bool(app.config.get('DB_BACKUP_ON_START'))
    interval_seconds = app.config.get('DB_BACKUP_INTERVAL_SECONDS', 0)
    if not backup_on_start and not interval_seconds:
        return
    if os.environ.get('FLASK_DEBUG') == '1' and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return

    def runner():
        with app.app_context():
            if backup_on_start:
                _backup_instance_database()
            if interval_seconds:
                while True:
                    time.sleep(interval_seconds)
                    _backup_instance_database()

    thread = threading.Thread(target=runner, name='db-backup-worker', daemon=True)
    thread.start()


def _safe_media_basename(ref, fallback_index=0):
    token = (ref or '').split('?', 1)[0].split('#', 1)[0]
    base = os.path.basename(token) or f"media-{fallback_index or 1}"
    safe = secure_filename(base) or f"media-{fallback_index or 1}"
    name, ext = os.path.splitext(safe)
    return name or f"media-{fallback_index or 1}", ext


def _collect_event_media_refs(event, allow_external=True):
    media_refs = []
    seen = set()
    for att in getattr(event, 'attachments', []) or []:
        ref = getattr(att, 'filename', None)
        if not ref or ref in seen:
            continue
        if not allow_external and _is_external_media(ref):
            continue
        seen.add(ref)
        media_refs.append(ref)
    return media_refs


def _build_archive_file(media_refs):
    archive_file = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    used_names = set()
    success_count = 0
    with zipfile.ZipFile(archive_file, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, ref in enumerate(media_refs, start=1):
            data = _read_media_bytes(ref)
            if not data:
                continue
            arcname = _build_archive_name(ref, used_names, idx)
            zf.writestr(arcname, data)
            success_count += 1
    archive_file.flush()
    try:
        archive_file.close()
    except Exception:
        pass
    return archive_file.name, success_count


def _upload_archive_to_oss(path, event_id):
    if not path or not app.config.get('USE_OSS'):
        return None, None
    bucket = _get_oss_bucket()
    if not bucket:
        return None, None
    prefix = app.config.get('OSS_PREFIX')
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f"event-{event_id}-media-{timestamp}.zip"
    object_key = f"archives/{filename}"
    if prefix:
        object_key = f"{prefix}/{object_key}"
    bucket.put_object_from_file(object_key, path)
    expiry = app.config.get('DIRECT_UPLOAD_URL_EXPIRATION', 900)
    try:
        expires_in = int(expiry)
    except (TypeError, ValueError):
        expires_in = 900
    expires_in = max(60, expires_in)
    signed_url = bucket.sign_url('GET', object_key, expires_in)
    signed_url = _finalize_signed_upload_url(signed_url)

    def _delete_later():
        try:
            bucket.delete_object(object_key)
        except Exception:
            pass

    try:
        threading.Timer(expires_in + 300, _delete_later).start()
    except Exception:
        pass
    return signed_url, object_key


def _finalize_signed_upload_url(url):
    """Rewrite signed OSS URL to desired domain/scheme for front-end direct uploads."""
    if not url:
        return url
    preferred_base = ''
    if app.config.get('OSS_ASSUME_PUBLIC'):
        preferred_base = app.config.get('OSS_PUBLIC_BASE_URL') or ''
    parsed = urlsplit(url)
    path = parsed.path or ''
    if preferred_base:
        normalized = _normalize_base_url(preferred_base)
        if normalized:
            base_parts = urlsplit(normalized)
            base_path = base_parts.path.rstrip('/')
            suffix = path or ''
            if suffix and not suffix.startswith('/'):
                suffix = '/' + suffix
            new_path = f"{base_path}{suffix}" if suffix else (base_path or '/')
            if not new_path.startswith('/'):
                new_path = '/' + new_path
            return urlunsplit((
                base_parts.scheme or parsed.scheme or 'https',
                base_parts.netloc or parsed.netloc,
                new_path,
                parsed.query,
                parsed.fragment
            ))
    if parsed.scheme == 'http':
        return urlunsplit(('https', parsed.netloc, path, parsed.query, parsed.fragment))
    return url


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


_AI_AUTOFILL_FORM_TYPES = {'item', 'location'}
_AI_AUTOFILL_IMAGE_LIMIT = 6
_AI_AUTOFILL_REF_LIMIT = 16
_AI_AUTOFILL_TIMEOUT_SECONDS = 45
_AI_AUTOFILL_NOTES_LIMIT = 1200
_AI_AUTOFILL_DETAIL_REF_LIMIT = 8
_AI_AUTOFILL_DETAIL_LABEL_LIMIT = 32
_AI_AUTOFILL_DETAIL_VALUE_LIMIT = 240
_AI_AUTOFILL_IMAGE_MAX_BYTES = 1_700_000
_AI_AUTOFILL_IMAGE_MAX_SIDE = 1600


def _limit_text(value, max_length):
    text_value = _ensure_string(value).strip()
    if not text_value or not max_length or max_length <= 0:
        return text_value
    return text_value[:max_length]


def _chatanywhere_runtime_config():
    api_key = (
        os.getenv('CHAT_ANYWHERE_API_KEY')
        or os.getenv('OPENAI_API_KEY')
        or ''
    ).strip()
    base_url = (os.getenv('CHAT_ANYWHERE_API_BASE_URL') or 'https://api.chatanywhere.com/v1').strip().rstrip('/')
    model = (
        os.getenv('CHAT_ANYWHERE_MODEL')
        or os.getenv('CHAT_ANYWHERE_VISION_MODEL')
        or os.getenv('CHAT_ANYWHERE_CHAT_MODEL')
        or os.getenv('OPENAI_MODEL')
        or 'gpt-4o-mini'
    ).strip()
    endpoint = f"{base_url}/chat/completions" if base_url else ''
    return {
        'api_key': api_key,
        'base_url': base_url,
        'endpoint': endpoint,
        'model': model
    }


def _normalize_ai_uploaded_refs(raw_refs):
    normalized = []
    seen = set()
    for raw_ref in raw_refs or []:
        token = _ensure_string(raw_ref).strip()
        if not token or token in seen:
            continue
        normalized.append(token)
        seen.add(token)
        if len(normalized) >= _AI_AUTOFILL_REF_LIMIT:
            break
    return normalized


def _image_mime_from_ref(ref, fallback='image/jpeg'):
    guessed, _ = mimetypes.guess_type(_ensure_string(ref).strip())
    if guessed and guessed.startswith('image/'):
        return guessed
    ext = _extract_file_extension(ref)
    if ext in {'jpg', 'jpeg'}:
        return 'image/jpeg'
    if ext == 'png':
        return 'image/png'
    if ext == 'gif':
        return 'image/gif'
    if ext == 'webp':
        return 'image/webp'
    if ext == 'bmp':
        return 'image/bmp'
    return fallback


def _prepare_ai_image_bytes(raw_bytes, mime_hint=None):
    if not raw_bytes:
        return None, None
    payload = raw_bytes
    mime_value = (mime_hint or 'image/jpeg').strip().lower()
    if not mime_value.startswith('image/'):
        mime_value = 'image/jpeg'
    if Image and (len(payload) > _AI_AUTOFILL_IMAGE_MAX_BYTES or mime_value in {'image/heic', 'image/heif'}):
        try:
            with Image.open(BytesIO(raw_bytes)) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                width, height = img.size
                longest = max(width, height)
                if longest > _AI_AUTOFILL_IMAGE_MAX_SIDE:
                    ratio = _AI_AUTOFILL_IMAGE_MAX_SIDE / float(longest)
                    target_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
                    img = img.resize(target_size, _poster_resample_filter() or Image.BICUBIC)
                output = BytesIO()
                img.save(output, format='JPEG', quality=84, optimize=True)
                payload = output.getvalue()
                mime_value = 'image/jpeg'
        except Exception:
            payload = raw_bytes
    if len(payload) > _AI_AUTOFILL_IMAGE_MAX_BYTES:
        return None, None
    return payload, mime_value


def _build_ai_image_data_url(raw_bytes, mime_hint=None):
    payload, mime_value = _prepare_ai_image_bytes(raw_bytes, mime_hint=mime_hint)
    if not payload or not mime_value:
        return None
    encoded = base64.b64encode(payload).decode('ascii')
    return f"data:{mime_value};base64,{encoded}"


def _looks_like_image_bytes(raw_bytes):
    if not raw_bytes:
        return False
    if not Image:
        return True
    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            img.verify()
        return True
    except Exception:
        return False


def _collect_ai_image_inputs(uploaded_files, uploaded_refs):
    inputs = []
    digest_seen = set()
    for file_storage in uploaded_files or []:
        if len(inputs) >= _AI_AUTOFILL_IMAGE_LIMIT:
            break
        filename = _ensure_string(getattr(file_storage, 'filename', '')).strip()
        mime_type = _ensure_string(getattr(file_storage, 'mimetype', '')).strip()
        if not filename and not mime_type.startswith('image/'):
            continue
        if filename and determine_media_kind(filename) != 'image' and not mime_type.startswith('image/'):
            continue
        try:
            file_storage.stream.seek(0)
            raw_bytes = file_storage.stream.read()
            file_storage.stream.seek(0)
        except Exception:
            continue
        if not raw_bytes:
            continue
        if not _looks_like_image_bytes(raw_bytes):
            continue
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest in digest_seen:
            continue
        data_url = _build_ai_image_data_url(raw_bytes, mime_hint=(mime_type or _image_mime_from_ref(filename)))
        if not data_url:
            continue
        inputs.append({'type': 'image_url', 'image_url': {'url': data_url}})
        digest_seen.add(digest)
    for ref in uploaded_refs or []:
        if len(inputs) >= _AI_AUTOFILL_IMAGE_LIMIT:
            break
        if determine_media_kind(ref) != 'image':
            continue
        raw_bytes = _read_media_bytes(ref, timeout=8)
        if not raw_bytes:
            continue
        if not _looks_like_image_bytes(raw_bytes):
            continue
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest in digest_seen:
            continue
        data_url = _build_ai_image_data_url(raw_bytes, mime_hint=_image_mime_from_ref(ref))
        if not data_url:
            continue
        inputs.append({'type': 'image_url', 'image_url': {'url': data_url}})
        digest_seen.add(digest)
    return inputs


def _extract_json_object_from_text(raw_text):
    content = _ensure_string(raw_text).strip()
    if not content:
        return None
    fenced = re.match(r'^```(?:json)?\s*([\s\S]*?)\s*```$', content, flags=re.IGNORECASE)
    if fenced:
        content = fenced.group(1).strip()
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = content.find('{')
    end = content.rfind('}')
    if start == -1 or end <= start:
        return None
    candidate = content[start:end + 1]
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def _normalize_ai_detail_refs(entries):
    normalized = []
    seen_values = set()
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return normalized
    for entry in entries:
        if len(normalized) >= _AI_AUTOFILL_DETAIL_REF_LIMIT:
            break
        if isinstance(entry, dict):
            label = _limit_text(entry.get('label') or entry.get('name') or '', _AI_AUTOFILL_DETAIL_LABEL_LIMIT)
            value = _limit_text(entry.get('value') or entry.get('content') or entry.get('url') or '', _AI_AUTOFILL_DETAIL_VALUE_LIMIT)
        elif isinstance(entry, str):
            label = ''
            value = _limit_text(entry, _AI_AUTOFILL_DETAIL_VALUE_LIMIT)
        else:
            continue
        if not value or value in seen_values:
            continue
        normalized.append({'label': label, 'value': value})
        seen_values.add(value)
    return normalized


def _normalize_ai_suggestion(form_type, payload):
    if not isinstance(payload, dict):
        return {}
    suggestion = {}
    if form_type == 'item':
        name = _limit_text(payload.get('name'), 100)
        category = _limit_text(payload.get('category'), 80)
        stock_status = _normalize_item_stock_status(payload.get('stock_status'))
        features = _normalize_item_feature(payload.get('features'))
        notes = _limit_text(payload.get('notes'), _AI_AUTOFILL_NOTES_LIMIT)
        detail_refs = _normalize_ai_detail_refs(payload.get('detail_refs'))
        unit = _limit_text(payload.get('unit'), 20)
        purchase_link = _limit_text(payload.get('purchase_link'), 280)
        if name:
            suggestion['name'] = name
        if category:
            suggestion['category'] = category
        if stock_status:
            suggestion['stock_status'] = stock_status
        if features:
            suggestion['features'] = features
        if notes:
            suggestion['notes'] = notes
        if detail_refs:
            suggestion['detail_refs'] = detail_refs
        quantity = payload.get('quantity')
        if isinstance(quantity, (int, float)):
            suggestion['quantity'] = quantity
        elif isinstance(quantity, str):
            quantity_text = quantity.strip()
            if re.match(r'^-?\d+(\.\d+)?$', quantity_text):
                try:
                    suggestion['quantity'] = float(quantity_text)
                except ValueError:
                    pass
        if unit:
            suggestion['unit'] = unit
        if purchase_link and re.match(r'^https?://', purchase_link, flags=re.IGNORECASE):
            suggestion['purchase_link'] = purchase_link
        return suggestion
    if form_type == 'location':
        name = _limit_text(payload.get('name'), 100)
        status = _normalize_location_status(payload.get('status'))
        notes = _limit_text(payload.get('notes'), _AI_AUTOFILL_NOTES_LIMIT)
        detail_link = _limit_text(payload.get('detail_link'), 280)
        detail_refs = _normalize_ai_detail_refs(payload.get('detail_refs'))
        usage_tags_raw = payload.get('usage_tags')
        usage_tags = []
        if isinstance(usage_tags_raw, list):
            for token in usage_tags_raw:
                key = _ensure_string(token).strip()
                if key in _LOCATION_USAGE_KEYS and key not in usage_tags:
                    usage_tags.append(key)
        if name:
            suggestion['name'] = name
        if status:
            suggestion['status'] = status
        if notes:
            suggestion['notes'] = notes
        if detail_link and re.match(r'^https?://', detail_link, flags=re.IGNORECASE):
            suggestion['detail_link'] = detail_link
        if detail_refs:
            suggestion['detail_refs'] = detail_refs
        if usage_tags:
            suggestion['usage_tags'] = usage_tags
        return suggestion
    return {}


def _build_ai_autofill_messages(form_type, context, image_inputs):
    context_payload = {}
    if isinstance(context, dict):
        for key in ('name', 'notes', 'category', 'stock_status', 'status', 'detail_link', 'purchase_link'):
            value = _limit_text(context.get(key), 200)
            if value:
                context_payload[key] = value
    if form_type == 'item':
        schema_text = (
            '{"name":"", "category":"", "stock_status":"正常|少量|用完|借出|舍弃", '
            '"notes":"", "detail_refs":[{"label":"","value":""}], "quantity":"", "unit":"", "purchase_link":""}'
        )
        task_text = (
            "你是盘点助手。请根据图片推断物品信息，优先提取名称、规格、品牌、用途、注意事项。"
            "若不确定字段就返回空字符串，不要编造。detail_refs 最多 8 条。"
        )
    else:
        schema_text = (
            '{"name":"", "status":"正常|脏|报修|危险|禁止", "notes":"", '
            '"detail_link":"", "detail_refs":[{"label":"","value":""}], '
            '"usage_tags":["study|leisure|event|public|rental|storage|travel|residence|other"]}'
        )
        task_text = (
            "你是空间盘点助手。请根据图片推断空间名称、风险/维护状态、空间说明、规则和注意事项。"
            "若不确定字段就返回空字符串，不要编造。detail_refs 最多 8 条。"
        )
    context_text = json.dumps(context_payload, ensure_ascii=False)
    system_message = (
        "你必须只返回 JSON 对象，不能输出任何额外文本。"
        "字段不存在时返回空字符串或空数组。"
    )
    user_content = [{
        'type': 'text',
        'text': (
            f"{task_text}\n"
            f"已有表单上下文：{context_text or '{}'}\n"
            f"输出 JSON 模板：{schema_text}"
        )
    }]
    user_content.extend(image_inputs or [])
    return [
        {'role': 'system', 'content': system_message},
        {'role': 'user', 'content': user_content}
    ]


def _extract_chat_message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for entry in content:
            if isinstance(entry, dict):
                text_part = entry.get('text')
                if isinstance(text_part, str):
                    parts.append(text_part)
        return ''.join(parts)
    return ''


def _chatanywhere_chat_completion(messages, max_tokens=900):
    runtime = _chatanywhere_runtime_config()
    if not runtime.get('api_key'):
        raise ValueError('CHAT_ANYWHERE_API_KEY 未配置，无法使用 AI 自动填写。')
    if not runtime.get('endpoint'):
        raise ValueError('CHAT_ANYWHERE_API_BASE_URL 未配置，无法使用 AI 自动填写。')
    request_payload = {
        'model': runtime.get('model') or 'gpt-4o-mini',
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': max_tokens
    }
    req = urllib.request.Request(
        runtime['endpoint'],
        data=json.dumps(request_payload, ensure_ascii=False).encode('utf-8'),
        method='POST'
    )
    req.add_header('Authorization', f"Bearer {runtime['api_key']}")
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=_AI_AUTOFILL_TIMEOUT_SECONDS) as resp:
            raw_body = resp.read()
    except HTTPError as exc:
        body = b''
        try:
            body = exc.read() or b''
        except Exception:
            body = b''
        detail = ''
        if body:
            try:
                payload = json.loads(body.decode('utf-8', errors='ignore'))
                if isinstance(payload, dict):
                    err = payload.get('error')
                    if isinstance(err, dict):
                        detail = _ensure_string(err.get('message')).strip()
                    elif isinstance(err, str):
                        detail = err.strip()
            except json.JSONDecodeError:
                detail = ''
        raise RuntimeError(detail or f'AI 服务返回错误（HTTP {exc.code}）。')
    except (URLError, OSError) as exc:
        raise RuntimeError(f'AI 服务连接失败：{exc}')
    try:
        parsed = json.loads(raw_body.decode('utf-8', errors='ignore'))
    except json.JSONDecodeError:
        raise RuntimeError('AI 服务返回了无法解析的响应。')
    if not isinstance(parsed, dict):
        raise RuntimeError('AI 服务响应格式不正确。')
    choices = parsed.get('choices')
    if not isinstance(choices, list) or not choices:
        raise RuntimeError('AI 服务未返回有效结果。')
    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get('message') if isinstance(first_choice, dict) else {}
    content = message.get('content') if isinstance(message, dict) else ''
    text_content = _extract_chat_message_text(content).strip()
    if not text_content:
        raise RuntimeError('AI 服务未返回可用文本。')
    return text_content, request_payload['model']


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
    detail_refs_raw = db.Column('detail_refs', db.Text)  # 参考信息集合（字符串）
    category = db.Column(db.String(50))                 # 类别/危险级别
    stock_status = db.Column(db.String(50))        # ✅ 新增字段：库存状态
    features = db.Column(db.String(200))           # ✅ 多选：用逗号分隔
    value = db.Column(db.Float)                    # ✅ 价值（数字）
    quantity = db.Column(db.Float)                 # ✅ 数量
    unit = db.Column(db.String(20))                # ✅ 单位（例如：瓶、包）
    purchase_date = db.Column(db.Date)             # ✅ 购入时间
    primary_attachment = db.Column(db.String(200))          # 附件主文件名
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

    attachments = db.relationship(
        'ItemAttachment',
        backref='item',
        lazy='select',
        cascade='all, delete-orphan',
        order_by='ItemAttachment.created_at'
    )

    @property
    def detail_refs(self):
        return _parse_item_detail_refs(self.detail_refs_raw)

    def set_detail_refs(self, entries):
        serialized, trimmed = _serialize_item_detail_refs(entries)
        self.detail_refs_raw = serialized
        return trimmed

    @property
    def attachment_filenames(self):
        filenames = []
        seen = set()
        for att in self.attachments:
            if att.filename and att.filename not in seen:
                filenames.append(att.filename)
                seen.add(att.filename)
        if self.primary_attachment and self.primary_attachment not in seen:
            filenames.insert(0, self.primary_attachment)
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
    status = db.Column(db.String(20))
    latitude = db.Column(db.Float, index=True)
    longitude = db.Column(db.Float, index=True)
    coordinate_source = db.Column(db.String(20))
    children = db.relationship('Location',
                            backref=db.backref('parent', remote_side=[id]),
                            cascade='all, delete-orphan')
    # 多对多负责人
    # responsible_members 关系由 Member.responsible_locations 的 backref 提供
    primary_attachment = db.Column(db.String(200))          # 位置附件主文件名
    notes = db.Column(db.Text)                          # 备注（纯文本）
    is_public = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    detail_refs_raw = db.Column('detail_refs', db.Text)  # 参考信息集合（字符串）
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    logs = db.relationship('Log', backref='location', lazy=True)     # 操作日志
    detail_link = db.Column(db.String(200))

    attachments = db.relationship(
        'LocationAttachment',
        backref='location',
        lazy='select',
        cascade='all, delete-orphan',
        order_by='LocationAttachment.created_at'
    )

    @property
    def attachment_filenames(self):
        filenames = []
        seen = set()
        for att in self.attachments:
            if att.filename and att.filename not in seen:
                filenames.append(att.filename)
                seen.add(att.filename)
        if self.primary_attachment and self.primary_attachment not in seen:
            filenames.insert(0, self.primary_attachment)
        return filenames

    @property
    def detail_refs(self):
        return _parse_item_detail_refs(self.detail_refs_raw)

    def set_detail_refs(self, entries):
        serialized, trimmed = _serialize_item_detail_refs(entries)
        self.detail_refs_raw = serialized
        return trimmed

    @property
    def usage_tags(self):
        return _extract_usage_tags_from_detail_refs(self.detail_refs)

    @property
    def detail_refs_without_usage_tags(self):
        return _strip_usage_tag_refs(self.detail_refs)

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
    attachments = db.relationship(
        'EventAttachment',
        backref='event',
        lazy='select',
        cascade='all, delete-orphan',
        order_by='EventAttachment.created_at'
    )
    logs = db.relationship('Log', backref='event', lazy=True)

    @property
    def attachment_filenames(self):
        return [att.filename for att in self.attachments if att.filename]

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


def add_event_attachments(event, file_storages):
    for attachment_file in file_storages:
        stored_name = save_uploaded_media(attachment_file)
        if stored_name:
            event.attachments.append(EventAttachment(filename=stored_name))


class ItemAttachment(db.Model):
    __tablename__ = 'item_images'
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<ItemAttachment {self.filename}>'


class LocationAttachment(db.Model):
    __tablename__ = 'location_images'
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<LocationAttachment {self.filename}>'


class EventAttachment(db.Model):
    __tablename__ = 'event_images'
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<EventAttachment {self.filename}>'

# 初始化数据库并创建默认用户
with app.app_context():
    if app.config.get('DIRECT_OSS_UPLOAD_ENABLED') and not _oss_direct_upload_ready():
        app.config['DIRECT_OSS_UPLOAD_ENABLED'] = False
    db.create_all()
    try:
        inspector = inspect(db.engine)
        table_names = inspector.get_table_names()
    except Exception:
        table_names = []
    if 'locations' in table_names:
        existing_cols = {col['name'] for col in inspector.get_columns('locations')}
        alter_statements = []
        has_location_status = 'status' in existing_cols
        has_legacy_status_column = 'clean_status' in existing_cols
        has_location_primary_attachment = 'primary_attachment' in existing_cols
        has_legacy_location_image_column = 'image' in existing_cols
        drop_legacy_location_image_column = has_location_primary_attachment and has_legacy_location_image_column
        if not has_location_status:
            if has_legacy_status_column:
                try:
                    with db.engine.begin() as conn:
                        conn.execute(text('ALTER TABLE locations RENAME COLUMN clean_status TO status'))
                    existing_cols.discard('clean_status')
                    existing_cols.add('status')
                    has_legacy_status_column = False
                    has_location_status = True
                except Exception:
                    alter_statements.append('ALTER TABLE locations ADD COLUMN status VARCHAR(20)')
                    has_location_status = True
            else:
                alter_statements.append('ALTER TABLE locations ADD COLUMN status VARCHAR(20)')
                has_location_status = True
        if not has_location_primary_attachment:
            if has_legacy_location_image_column:
                try:
                    with db.engine.begin() as conn:
                        conn.execute(text('ALTER TABLE locations RENAME COLUMN image TO primary_attachment'))
                    existing_cols.discard('image')
                    existing_cols.add('primary_attachment')
                    has_location_primary_attachment = True
                    has_legacy_location_image_column = False
                except Exception:
                    alter_statements.append('ALTER TABLE locations ADD COLUMN primary_attachment VARCHAR(200)')
                    has_location_primary_attachment = True
            else:
                alter_statements.append('ALTER TABLE locations ADD COLUMN primary_attachment VARCHAR(200)')
                has_location_primary_attachment = True
        if 'latitude' not in existing_cols:
            alter_statements.append('ALTER TABLE locations ADD COLUMN latitude REAL')
        if 'longitude' not in existing_cols:
            alter_statements.append('ALTER TABLE locations ADD COLUMN longitude REAL')
        if 'coordinate_source' not in existing_cols:
            alter_statements.append('ALTER TABLE locations ADD COLUMN coordinate_source VARCHAR(20)')
        if 'is_public' not in existing_cols:
            alter_statements.append('ALTER TABLE locations ADD COLUMN is_public BOOLEAN DEFAULT 0')
        if 'detail_refs' not in existing_cols:
            alter_statements.append('ALTER TABLE locations ADD COLUMN detail_refs TEXT')
        if alter_statements:
            with db.engine.begin() as conn:
                for stmt in alter_statements:
                    conn.execute(text(stmt))
        if has_legacy_status_column and has_location_status:
            with db.engine.begin() as conn:
                try:
                    conn.execute(text('ALTER TABLE locations DROP COLUMN clean_status'))
                except Exception:
                    pass
        if drop_legacy_location_image_column:
            with db.engine.begin() as conn:
                try:
                    conn.execute(text('ALTER TABLE locations DROP COLUMN image'))
                except Exception:
                    pass
        migrated_locations = False
        legacy_locations = (
            Location.query
            .filter(Location.notes.isnot(None), Location.notes != '')
            .all()
        )
        for loc in legacy_locations:
            try:
                payload = json.loads(loc.notes)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if not any(key in payload for key in ('description', 'usage_tags', 'is_public')):
                continue
            loc.notes = _ensure_string(payload.get('description'))
            if 'is_public' in payload:
                loc.is_public = bool(payload.get('is_public'))
            usage_tags = []
            usage_raw = payload.get('usage_tags') or []
            if isinstance(usage_raw, list):
                for tag in usage_raw:
                    tag = _ensure_string(tag)
                    if tag in _LOCATION_USAGE_KEYS and tag not in usage_tags:
                        usage_tags.append(tag)
            loc.set_detail_refs(_merge_usage_tags_into_detail_refs(loc.detail_refs, usage_tags))
            migrated_locations = True
        if migrated_locations:
            db.session.commit()
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
    if 'items' in table_names:
        item_cols = {col['name'] for col in inspector.get_columns('items')}
        item_alter_statements = []
        has_item_primary_attachment = 'primary_attachment' in item_cols
        has_legacy_item_image_column = 'image' in item_cols
        if not has_item_primary_attachment:
            if has_legacy_item_image_column:
                try:
                    with db.engine.begin() as conn:
                        conn.execute(text('ALTER TABLE items RENAME COLUMN image TO primary_attachment'))
                    item_cols.discard('image')
                    item_cols.add('primary_attachment')
                    has_legacy_item_image_column = False
                except Exception:
                    item_alter_statements.append('ALTER TABLE items ADD COLUMN primary_attachment VARCHAR(200)')
            else:
                item_alter_statements.append('ALTER TABLE items ADD COLUMN primary_attachment VARCHAR(200)')
        elif has_legacy_item_image_column:
            item_alter_statements.append('ALTER TABLE items DROP COLUMN image')
        if 'detail_refs' not in item_cols:
            item_alter_statements.append('ALTER TABLE items ADD COLUMN detail_refs TEXT')
        if 'responsible_id' in item_cols:
            item_alter_statements.append('ALTER TABLE items DROP COLUMN responsible_id')
        if 'detail_links' in item_cols:
            item_alter_statements.append('ALTER TABLE items DROP COLUMN detail_links')
        if item_alter_statements:
            with db.engine.begin() as conn:
                for stmt in item_alter_statements:
                    try:
                        conn.execute(text(stmt))
                    except Exception:
                        pass
    if Member.query.count() == 0:
        default_user = Member(name="Admin User", username="admin", contact="admin@example.com", notes="Default admin user")
        default_user.set_password("admin")
        db.session.add(default_user)
        db.session.commit()

_start_attachment_housekeeping()
_start_db_backup_worker()

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


def _send_local_attachment(filename):
    root, rel_path = _resolve_local_attachment_ref(filename)
    if not root or not rel_path:
        abort(404)
    return send_from_directory(root, rel_path)


@app.route('/attachments/<path:filename>')
def uploaded_attachment(filename):
    return _send_local_attachment(filename)


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
    upload_url = _finalize_signed_upload_url(upload_url)
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


@app.route('/pages/<int:page_id>')
def temporary_page(page_id):
    """Serve numeric HTML pages stored directly under instance/."""
    filename = f"{page_id}.html"
    return send_from_directory(TEMP_PAGE_DIR, filename)


@app.route('/events')
@login_required
def events_overview():
    events_query = Event.query.options(
        selectinload(Event.owner),
        selectinload(Event.locations),
        selectinload(Event.attachments),
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
        'external_attachment_urls': '',
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
            'external_attachment_urls': request.form.get('external_event_attachment_urls', ''),
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

        uploaded_event_files = request.files.getlist('event_attachments')
        cleaned_files = [f for f in uploaded_event_files if f and getattr(f, 'filename', '')]
        if cleaned_files:
            add_event_attachments(event, cleaned_files)
        remote_event_refs = _collect_remote_object_keys('event_attachments')
        if remote_event_refs:
            _append_media_records(event.attachments, EventAttachment, remote_event_refs)
        external_urls = _extract_external_urls(request.form.get('external_event_attachment_urls'))
        if external_urls:
            existing_refs = {att.filename for att in event.attachments}
            for url in external_urls:
                if url not in existing_refs:
                    event.attachments.append(EventAttachment(filename=url))
                    existing_refs.add(url)
        event.touch()
        db.session.commit()
        _sync_attachments_async(event.attachment_filenames)

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
    _sync_attachments_async(event.attachment_filenames)

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


def _build_archive_name(ref, used_names, index):
    base_name, ext = _safe_media_basename(ref, index)
    candidate = f"{base_name}{ext}"
    counter = 2
    while candidate in used_names:
        candidate = f"{base_name}-{counter}{ext}"
        counter += 1
    used_names.add(candidate)
    return candidate


@app.route('/events/<int:event_id>/media/archive')
@login_required
def download_event_media_archive(event_id):
    event = _load_event_for_edit(event_id)
    if not event.can_view(current_user):
        abort(403)
    media_refs = _collect_event_media_refs(event, allow_external=True)
    if not media_refs:
        abort(404, description='该事项暂无可下载的媒体文件')
    archive_path, success_count = _build_archive_file(media_refs)

    @after_this_request
    def cleanup(response):  # pragma: no cover - cleanup helper
        try:
            os.remove(archive_path)
        except OSError:
            pass
        return response

    if success_count == 0:
        try:
            os.remove(archive_path)
        except OSError:
            pass
        abort(404, description='媒体文件暂不可用，请稍后重试')

    filename = f"event-{event.id}-media.zip"
    return send_file(
        archive_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=filename
    )


@app.route('/events/<int:event_id>/media/archive/oss')
@login_required
def download_event_media_archive_oss(event_id):
    if not app.config.get('USE_OSS'):
        abort(404)
    event = _load_event_for_edit(event_id)
    if not event.can_view(current_user):
        abort(403)
    media_refs = _collect_event_media_refs(event, allow_external=False)
    if not media_refs:
        abort(404, description='该事项暂无存储在 OSS 的媒体文件')

    archive_path, success_count = _build_archive_file(media_refs)

    @after_this_request
    def cleanup(response):  # pragma: no cover - cleanup helper
        try:
            os.remove(archive_path)
        except OSError:
            pass
        return response

    if success_count == 0:
        try:
            os.remove(archive_path)
        except OSError:
            pass
        abort(404, description='媒体文件暂不可用，请稍后重试')

    signed_url, _ = _upload_archive_to_oss(archive_path, event_id)
    if not signed_url:
        abort(503, description='OSS 存储未启用或不可用，无法生成直链下载')
    return redirect(signed_url)


@app.route('/events/<int:event_id>/poster.png')
def event_share_poster(event_id):
    event = Event.query.options(
        selectinload(Event.owner),
        selectinload(Event.locations),
        selectinload(Event.attachments)
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
            'external_attachment_urls': request.form.get('external_event_attachment_urls', ''),
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

        remove_attachment_ids_raw = request.form.getlist('remove_event_attachment_ids')
        remove_attachment_ids = {int(x) for x in remove_attachment_ids_raw if x.isdigit()}
        if remove_attachment_ids:
            for att in list(event.attachments):
                if att.id in remove_attachment_ids:
                    remove_uploaded_file(att.filename)
                    event.attachments.remove(att)
                    db.session.delete(att)

        uploaded_event_files = request.files.getlist('event_attachments')
        cleaned_files = [f for f in uploaded_event_files if f and getattr(f, 'filename', '')]
        if cleaned_files:
            add_event_attachments(event, cleaned_files)
        remote_event_refs = _collect_remote_object_keys('event_attachments')
        if remote_event_refs:
            _append_media_records(event.attachments, EventAttachment, remote_event_refs)
        external_urls = _extract_external_urls(request.form.get('external_event_attachment_urls'))
        if external_urls:
            existing_refs = {att.filename for att in event.attachments}
            for url in external_urls:
                if url not in existing_refs:
                    event.attachments.append(EventAttachment(filename=url))
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
        'external_attachment_urls': '',
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
    for att in list(event.attachments):
        remove_uploaded_file(att.filename)
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


def _build_item_category_payload(items):
    category_map = {}
    uncategorized_bucket = []
    for item in items:
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
    return categories, category_payload, uncategorized_payload


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
    event_counts = dict(
        db.session.query(
            event_items.c.item_id,
            func.count(event_items.c.event_id)
        ).group_by(event_items.c.item_id).all()
    )
    categories, category_payload, uncategorized_payload = _build_item_category_payload(items_list)
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
    _sync_attachments_async(item.attachment_filenames)
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
    category_seed = (
        Item.query.options(load_only(Item.id, Item.name, Item.category))
        .order_by(func.lower(Item.name))
        .all()
    )
    categories, category_payload, uncategorized_payload = _build_item_category_payload(category_seed)
    return render_template(
        'item_detail.html',
        item=item,
        detail_refs=item.detail_refs,
        event_summary=event_bundle['summary'],
        ongoing_events=event_bundle['ongoing'],
        upcoming_events=event_bundle['upcoming'],
        unscheduled_events=event_bundle['unscheduled'],
        recent_past_events=event_bundle['recent_past'],
        past_events_total=event_bundle['past_total'],
        interest_summary=members_interest_summary,
        interest_total=interest_total,
        interest_relation_lookup=interest_relation_lookup,
        categories=categories,
        category_payload=category_payload,
        uncategorized_items=uncategorized_payload
    )

@app.route('/items/add', methods=['GET', 'POST'])
@login_required
def add_item():
    default_loc_id = request.args.get('loc_id', type=int)
    if request.method == 'POST':
        # 获取表单数据
        name = request.form.get('name')
        category = request.form.get('category')
        stock_status = _normalize_item_stock_status(request.form.get('stock_status'))
        if not stock_status:
            flash('请选择有效的物品状态。', 'danger')
            return redirect(request.url)
        feature_raw = request.form.get('features')
        features_str = _normalize_item_feature(feature_raw)  # 统一为公共/私人
        if not features_str:
            flash('请选择物品归属（公共或私人）。', 'danger')
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
        detail_refs = _collect_detail_refs_from_form(request.form)

        # 附件处理（支持多选）
        uploaded_files = request.files.getlist('attachments')

        saved_refs = []
        saved_ref_seen = set()
        for attachment_file in uploaded_files:
            stored_name = save_uploaded_media(attachment_file)
            if stored_name and stored_name not in saved_ref_seen:
                saved_refs.append(stored_name)
                saved_ref_seen.add(stored_name)
        remote_refs = _collect_remote_object_keys('attachments')
        for remote_ref in remote_refs:
            if remote_ref and remote_ref not in saved_ref_seen:
                saved_refs.append(remote_ref)
                saved_ref_seen.add(remote_ref)

        external_urls = _extract_external_urls(request.form.get('external_attachment_urls'))
        primary_attachment = saved_refs[0] if saved_refs else (external_urls[0] if external_urls else None)

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
            notes=notes,
            purchase_link=purchase_link,
            primary_attachment=primary_attachment
        )
        assigned_members = new_item.assign_responsible_members(responsible_members)
        if features_str == '私人' and not assigned_members:
            new_item.assign_responsible_members([current_user])
        trimmed_refs = new_item.set_detail_refs(detail_refs)
        if trimmed_refs:
            flash('部分参考信息过长，已保留前几条，请确认长度。', 'warning')
        # 绑定多个位置（若前端未选择则为空列表）
        loc_ids = [int(x) for x in location_ids] if location_ids else []
        if not loc_ids and default_loc_id:
            loc_ids = [default_loc_id]
        if loc_ids:
            new_item.locations = Location.query.filter(Location.id.in_(loc_ids)).all()
        db.session.add(new_item)
        existing_refs = set()
        for fname in saved_refs:
            if fname not in existing_refs:
                new_item.attachments.append(ItemAttachment(filename=fname))
                existing_refs.add(fname)
        for url in external_urls:
            if url not in existing_refs:
                new_item.attachments.append(ItemAttachment(filename=url))
                existing_refs.add(url)
        db.session.commit()
        _sync_attachments_async(new_item.attachment_filenames)

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
        categories=categories,
        item_stock_status_choices=_ITEM_STOCK_STATUS_CHOICES
    )

@app.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_item(item_id):
    item = Item.query.get_or_404(item_id)
    if item.features == '私人' and current_user not in item.responsible_members:
        abort(403)
    if request.method == 'POST':
        # 更新物品信息
        item.name = request.form.get('name')
        item.category = request.form.get('category')
        detail_refs = _collect_detail_refs_from_form(request.form)
        trimmed_refs = item.set_detail_refs(detail_refs)
        if trimmed_refs:
            flash('部分参考信息过长，已保留前几条，请确认长度。', 'warning')

        item.stock_status = _normalize_item_stock_status(request.form.get('stock_status'))
        if not item.stock_status:
            flash('请选择有效的物品状态。', 'danger')
            return redirect(request.url)
        feature_raw = request.form.get('features')
        features_str = _normalize_item_feature(feature_raw)          # 单选物品归属
        if not features_str:
            flash('请选择物品归属（公共或私人）。', 'danger')
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

        if item.primary_attachment and not any(att.filename == item.primary_attachment for att in item.attachments):
            item.attachments.append(ItemAttachment(filename=item.primary_attachment))

        # 删除勾选的旧附件
        remove_attachment_ids_raw = request.form.getlist('remove_attachment_ids')
        remove_attachment_ids = {int(x) for x in remove_attachment_ids_raw if x.isdigit()}
        if remove_attachment_ids:
            for att in list(item.attachments):
                if att.id in remove_attachment_ids:
                    remove_uploaded_file(att.filename)
                    item.attachments.remove(att)
                    db.session.delete(att)

        remove_primary = request.form.get('remove_primary_attachment') in {'1', 'on', 'true'}
        if remove_primary and item.primary_attachment:
            if not any(att.filename == item.primary_attachment for att in item.attachments):
                remove_uploaded_file(item.primary_attachment)
            item.primary_attachment = None

        # 处理新增上传（支持多选）
        uploaded_files = request.files.getlist('attachments')
        for attachment_file in uploaded_files:
            stored_name = save_uploaded_media(attachment_file)
            if stored_name:
                item.attachments.append(ItemAttachment(filename=stored_name))
        remote_refs = _collect_remote_object_keys('attachments')
        if remote_refs:
            _append_media_records(item.attachments, ItemAttachment, remote_refs)

        external_urls = _extract_external_urls(request.form.get('external_attachment_urls'))
        if external_urls:
            existing_refs = {att.filename for att in item.attachments}
            if item.primary_attachment:
                existing_refs.add(item.primary_attachment)
            for url in external_urls:
                if url not in existing_refs:
                    item.attachments.append(ItemAttachment(filename=url))
                    existing_refs.add(url)

        if item.attachments:
            item.primary_attachment = item.attachments[0].filename
        elif remove_primary:
            item.primary_attachment = None

        item.last_modified = datetime.utcnow()
        db.session.commit()
        _sync_attachments_async(item.attachment_filenames)

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
        categories=categories,
        item_stock_status_choices=_ITEM_STOCK_STATUS_CHOICES
    )

@app.route('/items/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)
    if item.features == '私人' and current_user not in item.responsible_members:
        abort(403)
    for fname in set(item.attachment_filenames):
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
    origin_item_id = request.form.get('origin_item_id', type=int)

    def redirect_target():
        if origin_item_id:
            return redirect(url_for('item_detail', item_id=origin_item_id))
        return redirect(url_for('items'))

    if not category_name:
        flash('请输入要管理的类别名称', 'warning')
        return redirect_target()

    def parse_ids(key):
        raw = request.form.getlist(key)
        return {int(val) for val in raw if val.isdigit()}

    add_ids = parse_ids('add_item_ids')
    remove_ids = parse_ids('remove_item_ids')

    if not add_ids and not remove_ids:
        flash('未选择任何需要调整的物品', 'info')
        return redirect_target()

    added_items = []
    removed_items = []
    now = datetime.utcnow()

    if add_ids:
        candidates = Item.query.filter(Item.id.in_(add_ids)).all()
        for item in candidates:
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
        flash('没有物品符合调整条件', 'info')
        return redirect_target()

    db.session.commit()
    summary = []
    if added_items:
        summary.append(f"新增 {len(added_items)} 个物品")
    if removed_items:
        summary.append(f"取消 {len(removed_items)} 个物品")
    flash('，'.join(summary) + f' 于类别 {category_name}', 'success')
    return redirect_target()

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
    return render_template('locations.html',
                           locations=locations,
                           event_counts=event_counts,
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
    filters = [
        Item.name.ilike(like_pattern),
        Item.notes.ilike(like_pattern),
        Item.detail_refs_raw.ilike(like_pattern)
    ]
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


@app.route('/api/forms/ai-autofill', methods=['POST'])
@login_required
def ai_form_autofill():
    form_type = (request.form.get('form_type') or '').strip().lower()
    if form_type not in _AI_AUTOFILL_FORM_TYPES:
        return jsonify({'error': 'unsupported_form_type', 'message': '仅支持物品和空间表单自动填写。'}), 400

    context = {}
    context_raw = request.form.get('context_json') or request.form.get('context')
    if context_raw:
        try:
            parsed_context = json.loads(context_raw)
            if isinstance(parsed_context, dict):
                context = parsed_context
        except json.JSONDecodeError:
            context = {}

    raw_refs = []
    raw_refs.extend(request.form.getlist('uploaded_refs'))
    raw_refs.extend(request.form.getlist('attachments_remote_keys'))
    raw_refs.extend(_extract_external_urls(request.form.get('external_attachment_urls')))
    uploaded_refs = _normalize_ai_uploaded_refs(raw_refs)
    uploaded_files = request.files.getlist('attachments')
    image_inputs = _collect_ai_image_inputs(uploaded_files, uploaded_refs)
    if not image_inputs:
        return jsonify({'error': 'no_images', 'message': '请先拍照或上传至少一张图片后再试。'}), 400

    messages = _build_ai_autofill_messages(form_type, context, image_inputs)
    try:
        content, model_name = _chatanywhere_chat_completion(messages, max_tokens=900)
    except ValueError as exc:
        return jsonify({'error': 'config_missing', 'message': str(exc)}), 503
    except RuntimeError as exc:
        app.logger.warning('AI 自动填写调用失败 form=%s user=%s error=%s', form_type, current_user.id, exc)
        return jsonify({'error': 'upstream_failure', 'message': str(exc)}), 502

    raw_payload = _extract_json_object_from_text(content)
    if not isinstance(raw_payload, dict):
        app.logger.warning('AI 自动填写响应无法解析为 JSON form=%s user=%s content=%s', form_type, current_user.id, content[:300])
        return jsonify({'error': 'invalid_response', 'message': 'AI 返回格式异常，请稍后重试。'}), 502

    suggestion = _normalize_ai_suggestion(form_type, raw_payload)
    return jsonify({
        'ok': True,
        'form_type': form_type,
        'model': model_name,
        'image_count': len(image_inputs),
        'suggestion': suggestion
    })


@app.route('/locations/add', methods=['GET', 'POST'])
@login_required
def add_location():
    if request.method == 'POST':
        # 获取并保存新的位置记录
        name = request.form.get('name')
        status = _normalize_location_status(request.form.get('status')) or '正常'
        parent_id = request.form.get('parent_id')
        responsible_ids = request.form.getlist('responsible_ids')
        detail_link = request.form.get('detail_link')
        notes = request.form.get('notes') or ''
        is_public = request.form.get('is_public') in {'1', 'on', 'true', 'yes'}
        usage_tags = [tag for tag in request.form.getlist('usage_tags') if tag in _LOCATION_USAGE_KEYS]
        detail_refs = _collect_detail_refs_from_form(request.form)
        detail_refs = _merge_usage_tags_into_detail_refs(detail_refs, usage_tags)
        latitude = _parse_coordinate(request.form.get('latitude'))
        longitude = _parse_coordinate(request.form.get('longitude'))
        coordinate_source = request.form.get('coordinate_source') or None
        if latitude is None or longitude is None:
            latitude = None
            longitude = None
            coordinate_source = None

        uploaded_files = request.files.getlist('attachments')

        saved_refs = []
        for attachment_file in uploaded_files:
            stored_name = save_uploaded_media(attachment_file)
            if stored_name:
                saved_refs.append(stored_name)
        saved_refs.extend(_collect_remote_object_keys('attachments'))
        external_urls = _extract_external_urls(request.form.get('external_attachment_urls'))
        primary_attachment = saved_refs[0] if saved_refs else (external_urls[0] if external_urls else None)
        # 先创建 Location
        new_loc = Location(
            name=name,
            parent_id=parent_id if parent_id else None,
            notes=notes,
            is_public=is_public,
            primary_attachment=primary_attachment,
            status=status,
            detail_link=detail_link,
            latitude=latitude,
            longitude=longitude,
            coordinate_source=coordinate_source
        )
        trimmed_refs = new_loc.set_detail_refs(detail_refs)
        db.session.add(new_loc)
        existing_refs = set()
        for fname in saved_refs:
            if fname not in existing_refs:
                new_loc.attachments.append(LocationAttachment(filename=fname))
                existing_refs.add(fname)
        for url in external_urls:
            if url not in existing_refs:
                new_loc.attachments.append(LocationAttachment(filename=url))
                existing_refs.add(url)
        # 多负责人
        member_objs = []
        if responsible_ids:
            member_objs = Member.query.filter(Member.id.in_([int(mid) for mid in responsible_ids])).all()
        if not member_objs and not is_public:
            member_objs = [current_user]
        new_loc.responsible_members = member_objs
        db.session.commit()
        _sync_attachments_async(new_loc.attachment_filenames)
        # 记录日志
        log = Log(user_id=current_user.id, location_id=new_loc.id, action_type="新增位置", details=f"Added location {new_loc.name}")
        db.session.add(log)
        db.session.commit()

        if trimmed_refs:
            flash('部分参考信息过长，已保留前几条，请确认长度。', 'warning')
        flash('社区空间已添加', 'success')
        return redirect(url_for('locations_list'))
    
    members = Member.query.all()
    parents = Location.query.all()
    return render_template('location_form.html',
                           members=members,
                           location=None,
                           parents=parents,
                           selected_usage_tags=[],
                           editable_detail_refs=[],
                           location_usage_choices=_LOCATION_USAGE_CHOICES,
                           location_status_choices=_LOCATION_STATUS_CHOICES)

@app.route('/locations/<int:loc_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    if location.responsible_members and current_user not in location.responsible_members:
        abort(403)
    if request.method == 'POST':
        # 更新位置信息
        location.name = request.form.get('name')
        raw_parent_id = request.form.get('parent_id')
        parent_id = None
        if raw_parent_id:
            try:
                candidate_id = int(raw_parent_id)
            except (TypeError, ValueError):
                candidate_id = None
            if candidate_id and candidate_id != location.id and Location.query.get(candidate_id):
                parent_id = candidate_id
        location.parent_id = parent_id
        responsible_ids = request.form.getlist('responsible_ids')
        detail_link = request.form.get('detail_link')
        location.notes = request.form.get('notes') or ''
        location.is_public = request.form.get('is_public') in {'1', 'on', 'true', 'yes'}
        usage_tags = [tag for tag in request.form.getlist('usage_tags') if tag in _LOCATION_USAGE_KEYS]
        detail_refs = _collect_detail_refs_from_form(request.form)
        detail_refs = _merge_usage_tags_into_detail_refs(detail_refs, usage_tags)
        location.status = _normalize_location_status(request.form.get('status')) or '正常'
        latitude = _parse_coordinate(request.form.get('latitude'))
        longitude = _parse_coordinate(request.form.get('longitude'))
        coordinate_source = request.form.get('coordinate_source') or None
        if latitude is None or longitude is None:
            latitude = None
            longitude = None
            coordinate_source = None

        if location.primary_attachment and not any(att.filename == location.primary_attachment for att in location.attachments):
            location.attachments.append(LocationAttachment(filename=location.primary_attachment))

        remove_attachment_ids_raw = request.form.getlist('remove_attachment_ids')
        remove_attachment_ids = {int(x) for x in remove_attachment_ids_raw if x.isdigit()}
        if remove_attachment_ids:
            for att in list(location.attachments):
                if att.id in remove_attachment_ids:
                    remove_uploaded_file(att.filename)
                    location.attachments.remove(att)
                    db.session.delete(att)

        remove_primary = request.form.get('remove_primary_attachment') in {'1', 'on', 'true'}
        if remove_primary and location.primary_attachment:
            if not any(att.filename == location.primary_attachment for att in location.attachments):
                remove_uploaded_file(location.primary_attachment)
            location.primary_attachment = None

        uploaded_files = request.files.getlist('attachments')
        for attachment_file in uploaded_files:
            stored_name = save_uploaded_media(attachment_file)
            if stored_name:
                location.attachments.append(LocationAttachment(filename=stored_name))
        remote_refs = _collect_remote_object_keys('attachments')
        if remote_refs:
            _append_media_records(location.attachments, LocationAttachment, remote_refs)

        external_urls = _extract_external_urls(request.form.get('external_attachment_urls'))
        if external_urls:
            existing_refs = {att.filename for att in location.attachments}
            if location.primary_attachment:
                existing_refs.add(location.primary_attachment)
            for url in external_urls:
                if url not in existing_refs:
                    location.attachments.append(LocationAttachment(filename=url))
                    existing_refs.add(url)

        if location.attachments:
            location.primary_attachment = location.attachments[0].filename
        elif remove_primary:
            location.primary_attachment = None
        trimmed_refs = location.set_detail_refs(detail_refs)
        # 多负责人
        member_objs = []
        if responsible_ids:
            member_objs = Member.query.filter(Member.id.in_([int(mid) for mid in responsible_ids])).all()
        if not member_objs and not location.is_public:
            member_objs = [current_user]
        location.responsible_members = member_objs
        location.detail_link = detail_link
        location.latitude = latitude
        location.longitude = longitude
        location.coordinate_source = coordinate_source
        location.last_modified = datetime.utcnow()
        db.session.commit()
        _sync_attachments_async(location.attachment_filenames)
        # 记录日志
        log = Log(user_id=current_user.id, location_id=location.id, action_type="修改位置", details=f"Edited location {location.name}")
        db.session.add(log)
        db.session.commit()
        if trimmed_refs:
            flash('部分参考信息过长，已保留前几条，请确认长度。', 'warning')
        flash('空间信息已更新', 'success')
        return redirect(url_for('locations_list'))
    members = Member.query.all()
    parents = Location.query.filter(Location.id != location.id).all()
    return render_template('location_form.html',
                           members=members,
                           location=location,
                           parents=parents,
                           selected_usage_tags=location.usage_tags,
                           editable_detail_refs=location.detail_refs_without_usage_tags,
                           location_usage_choices=_LOCATION_USAGE_CHOICES,
                           location_status_choices=_LOCATION_STATUS_CHOICES)

@app.route('/locations/<int:loc_id>/delete', methods=['POST'])
@login_required
def delete_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    if location.responsible_members and current_user not in location.responsible_members:
        abort(403)
    for fname in set(location.attachment_filenames):
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
    _sync_attachments_async(location.attachment_filenames)
    # 获取该位置包含的所有物品（多对多）
    items_at_location = sorted(location.items, key=lambda item: item.name.lower())
    # 分类统计状态标签（如：用完、少量、借出）
    status_counter = Counter()
    category_counter = Counter()
    feature_counter = Counter()
    responsible_counter = Counter()
    responsible_map = {}
    for item in items_at_location:
        item_status = _normalize_item_stock_status(item.stock_status)
        if item_status:
            status_counter[item_status] += 1
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

    usage_badges = [
        {'key': tag, 'label': _LOCATION_USAGE_LABELS.get(tag, tag)}
        for tag in location.usage_tags
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
    # 负责的位置（多对多）
    all_locations = list(member.responsible_locations)

    # 分开“告警”和“正常”
    critical_items = [it for it in all_items if _is_item_alert_status(it.stock_status)]
    critical_items.sort(
        key=lambda it: (
            _ITEM_ALERT_STATUS_PRIORITY.get(_normalize_item_stock_status(it.stock_status), 99),
            (it.name or '').lower()
        )
    )
    normal_items = [it for it in all_items if not _is_item_alert_status(it.stock_status)]
    normal_items.sort(key=lambda it: (it.name or '').lower())
    items_resp = critical_items + normal_items  # 告警状态的置顶

    critical_locs = [loc for loc in all_locations if _is_location_alert_status(loc.status)]
    critical_locs.sort(
        key=lambda loc: (
            _LOCATION_ALERT_STATUS_PRIORITY.get(_normalize_location_status(loc.status), 99),
            (loc.name or '').lower()
        )
    )
    normal_locs = [loc for loc in all_locations if not _is_location_alert_status(loc.status)]
    normal_locs.sort(key=lambda loc: (loc.name or '').lower())
    locations_resp = critical_locs + normal_locs  # 告警状态的置顶

    item_alert_counts = {status: 0 for status in _ITEM_ALERT_STOCK_STATUSES}
    item_alert_samples = {status: [] for status in _ITEM_ALERT_STOCK_STATUSES}
    for item in critical_items:
        status = _normalize_item_stock_status(item.stock_status)
        if not status:
            continue
        item_alert_counts[status] += 1
        if len(item_alert_samples[status]) < 3:
            item_alert_samples[status].append(item)
    item_alerts = []
    for status in _ITEM_ALERT_STOCK_STATUSES:
        count = item_alert_counts.get(status, 0)
        if count <= 0:
            continue
        item_alerts.append({
            'status': status,
            'count': count,
            'level': _item_alert_level(status),
            'action_label': _item_alert_action_label(status),
            'message': _item_alert_message(status, count),
            'sample_items': item_alert_samples.get(status, [])
        })
    location_alert_counts = {status: 0 for status in _LOCATION_ALERT_STATUSES}
    location_alert_samples = {status: [] for status in _LOCATION_ALERT_STATUSES}
    for location in critical_locs:
        status = _normalize_location_status(location.status)
        if not status:
            continue
        location_alert_counts[status] += 1
        if len(location_alert_samples[status]) < 3:
            location_alert_samples[status].append(location)
    location_alerts = []
    for status in _LOCATION_ALERT_STATUSES:
        count = location_alert_counts.get(status, 0)
        if count <= 0:
            continue
        location_alerts.append({
            'status': status,
            'count': count,
            'level': _location_alert_level(status),
            'action_label': _location_alert_action_label(status),
            'message': _location_alert_message(status, count),
            'sample_locations': location_alert_samples.get(status, [])
        })
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
                           item_alerts=item_alerts,
                           location_alerts=location_alerts,
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
        _sync_attachments_async([member.photo])
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
def inject_attachment_helpers():
    def _resolve_media_entry(ref):
        if not ref:
            return None, None
        if _is_external_media(ref):
            return _normalize_external_url(ref), 'external'
        root, rel_path = _resolve_local_attachment_ref(ref)
        if root and rel_path:
            return url_for('uploaded_attachment', filename=rel_path), 'local'
        if app.config.get('USE_OSS'):
            candidates = _attachment_ref_candidates(ref, prefer_prefix=True)
            key = candidates[0] if candidates else ref.lstrip('/')
            return _build_oss_url(key), 'oss'
        return url_for('uploaded_attachment', filename=ref), 'local'

    def resolve_media_url(ref):
        resolved, _source = _resolve_media_entry(ref)
        return resolved

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
            resolved, source = _resolve_media_entry(fname)
            if not resolved:
                continue
            entries.append({
                'url': resolved,
                'kind': determine_media_kind(fname),
                'filename': fname,
                'display_name': media_display_name(fname),
                'is_remote': _is_external_media(fname),
                'source': source
            })
        return entries

    def item_media_entries(item):
        if not item:
            return []
        return _build_media_entries(getattr(item, 'attachment_filenames', []) or [])

    def location_media_entries(location):
        if not location:
            return []
        return _build_media_entries(getattr(location, 'attachment_filenames', []) or [])

    def event_media_entries(event):
        if not event:
            return []
        filenames = []
        for att in getattr(event, 'attachments', []) or []:
            if att.filename:
                filenames.append(att.filename)
        return _build_media_entries(filenames)

    def uploaded_media_url(filename):
        return resolve_media_url(filename)

    def item_attachment_urls(item):
        return [entry['url'] for entry in item_media_entries(item) if entry['kind'] == 'image']

    def location_attachment_urls(location):
        return [entry['url'] for entry in location_media_entries(location) if entry['kind'] == 'image']

    def uploaded_attachment_url(filename):
        return uploaded_media_url(filename)

    return dict(
        item_media_entries=item_media_entries,
        location_media_entries=location_media_entries,
        event_media_entries=event_media_entries,
        uploaded_media_url=uploaded_media_url,
        uploaded_attachment_url=uploaded_attachment_url,
        item_attachment_urls=item_attachment_urls,
        location_attachment_urls=location_attachment_urls,
        media_kind=determine_media_kind,
        media_kind_labels=MEDIA_KIND_LABELS,
        media_display_name=media_display_name,
        direct_upload_config=_build_direct_upload_config(),
        feature_intent=_feature_intent,
        normalize_item_stock_status=_normalize_item_stock_status,
        stock_status_intent=_stock_status_intent,
        is_item_alert_status=_is_item_alert_status,
        item_alert_action_label=_item_alert_action_label,
        item_alert_level=_item_alert_level,
        normalize_location_status=_normalize_location_status,
        location_status_intent=_location_status_intent,
        is_location_dirty=_is_location_dirty_status,
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
