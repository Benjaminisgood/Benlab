"""Pure helpers for bulk inventory entry, location paths, and declutter matching.

These functions do not touch Flask or the database so they can be unit-tested
without starting the app.
"""
import csv
import re
from difflib import SequenceMatcher
from io import StringIO


BULK_MAX_ITEMS = 500
BULK_FIELD_ORDER = (
    'name',
    'quantity_desc',
    'category',
    'tags',
    'status',
    'disposition',
    'notes',
)

DISPOSITION_CHOICES = ('在用', '闲置', '转让', '收藏', '想买', '已处理')

# Personal-inventory aliases. Stock status (正常/少量/用完/借出/舍弃) stays separate.
_DISPOSITION_ALIASES = {
    '在用': '在用',
    '拥有': '在用',
    '持有': '在用',
    '使用中': '在用',
    '闲置': '闲置',
    '闲置中': '闲置',
    '转让': '转让',
    '转手': '转让',
    '送人': '转让',
    '出售': '转让',
    '收藏': '收藏',
    '想买': '想买',
    '待购': '想买',
    '已处理': '已处理',
    '处理完': '已处理',
    '丢掉': '已处理',
    '扔掉': '已处理',
    '丢弃': '已处理',
    '卖出': '已处理',
}

COLUMN_ALIASES = {
    'name': {'name', 'item_name', 'item', '物品名', '物品名称', '名称'},
    'quantity_desc': {'quantity_desc', 'quantity', 'qty', '数量', '数量描述', '规格', 'unit'},
    'category': {'category', '类别', '分类'},
    'tags': {'tags', 'tag', '标签'},
    'status': {'status', 'stock_status', '库存状态', '状态'},
    'disposition': {'disposition', '处置', '处置状态', '去留'},
    'features': {'features', '归属', '可见性'},
    'notes': {'notes', '备注', '说明'},
    'location': {'location', 'location_name', '位置', '存放位置', '地点', '位置名', '空间名'},
    'value': {'value', '价值', '价格'},
    'purchase_link': {'purchase_link', '采购链接', '购买链接'},
    'purchase_date': {'purchase_date', '购入日期', '购入时间', '购买日期'},
}

_TAG_SPLIT = re.compile(r'[,，、;；]+')
_NAME_NOISE = re.compile(r'[\s\-_/\\.,，。、;；:：·~～!！?？（）()\[\]【】"\'“”]+')
_HEADER_FOLD = re.compile(r'[\s_]+')


def normalize_disposition(value):
    """Return a canonical disposition, or None when empty/unknown."""
    if value is None:
        return None
    token = str(value).strip()
    if not token:
        return None
    if token in DISPOSITION_CHOICES:
        return token
    return _DISPOSITION_ALIASES.get(token)


def split_tags(value):
    """Split a tag string or list into a de-duplicated list."""
    if value is None:
        return []
    parts = []
    if isinstance(value, (list, tuple)):
        for item in value:
            parts.extend(_TAG_SPLIT.split(str(item)))
    else:
        parts = _TAG_SPLIT.split(str(value))
    tags = []
    seen = set()
    for part in parts:
        token = part.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        tags.append(token[:30])
        if len(tags) >= 12:
            break
    return tags


def join_tags(value):
    return ','.join(split_tags(value))


def _canonicalize_header(cell):
    token = _HEADER_FOLD.sub('', str(cell or '').strip().lower())
    if not token:
        return None
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            folded = _HEADER_FOLD.sub('', alias.strip().lower())
            if token == folded:
                return field
    return None


def _finalize_payload(payload, line_no, errors):
    name = str(payload.get('name') or '').strip()
    if not name:
        errors.append(f'第 {line_no} 行缺少名称。')
        return None
    payload = dict(payload)
    payload['name'] = name[:100]
    if payload.get('quantity_desc'):
        payload['quantity_desc'] = str(payload['quantity_desc']).strip()[:120]
    if payload.get('category'):
        payload['category'] = str(payload['category']).strip()[:50]
    payload['tags'] = split_tags(payload.get('tags'))
    return payload


def parse_bulk_lines(text, max_items=BULK_MAX_ITEMS):
    """Parse a quick-add box.

    Each non-empty line is one item. ``|`` or tab separates
    名称 | 数量 | 类别 | 标签 | 库存状态 | 处置 | 备注.
    A line without a separator is just the name. ``#`` starts a comment.
    A first line whose cells are known column names is treated as a header.
    """
    payloads = []
    errors = []
    if not text:
        return payloads, errors

    parsed = []
    for lineno, raw in enumerate(str(text).splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '|' in line:
            parts = [part.strip() for part in line.split('|')]
        elif '\t' in line:
            parts = [part.strip() for part in line.split('\t')]
        else:
            parts = [line]
        parsed.append((lineno, parts))

    if not parsed:
        return payloads, errors

    header = [_canonicalize_header(part) for part in parsed[0][1]]
    recognized = [field for field in header if field]
    has_header = 'name' in recognized and len(recognized) >= 2 and len(parsed[0][1]) >= 2
    data_lines = parsed[1:] if has_header else parsed

    for lineno, parts in data_lines:
        if len(payloads) >= max_items:
            errors.append(f'超过 {max_items} 条，已忽略第 {lineno} 行及之后的内容。')
            break
        payload = {}
        if has_header:
            for field, value in zip(header, parts):
                if field and value:
                    payload[field] = value
        else:
            if len(parts) > len(BULK_FIELD_ORDER):
                errors.append(f'第 {lineno} 行字段过多，已忽略。')
                continue
            for field, value in zip(BULK_FIELD_ORDER, parts):
                if value:
                    payload[field] = value
        finalized = _finalize_payload(payload, lineno, errors)
        if finalized:
            payloads.append(finalized)
    return payloads, errors


def parse_csv_text(text, max_items=BULK_MAX_ITEMS):
    """Parse CSV text using the readme column aliases.

    A header row is detected when it contains 名称/name plus at least one
    other known column. Otherwise columns follow
    名称,数量,类别,标签,库存状态,处置,备注.
    """
    payloads = []
    errors = []
    if not text or not str(text).strip():
        return payloads, errors

    sample = str(text).lstrip('\ufeff')
    reader = csv.reader(StringIO(sample))
    raw_rows = []
    for row in reader:
        if not row or not any(str(cell).strip() for cell in row):
            continue
        raw_rows.append([str(cell).strip() for cell in row])
    if not raw_rows:
        return payloads, errors

    header = [_canonicalize_header(cell) for cell in raw_rows[0]]
    recognized = [field for field in header if field]
    has_header = 'name' in recognized and len(recognized) >= 2
    data_rows = raw_rows[1:] if has_header else raw_rows
    if not has_header:
        width = max(len(row) for row in raw_rows)
        if width == 1:
            header = ['name']
        else:
            header = list(BULK_FIELD_ORDER[:width])

    start_no = 2 if has_header else 1
    for offset, cells in enumerate(data_rows, start=start_no):
        if len(payloads) >= max_items:
            errors.append(f'超过 {max_items} 条，其余行已忽略。')
            break
        payload = {}
        for index, cell in enumerate(cells):
            if index >= len(header):
                break
            field = header[index]
            if field and cell:
                payload[field] = cell
        finalized = _finalize_payload(payload, offset, errors)
        if finalized:
            payloads.append(finalized)
    return payloads, errors


def build_location_paths(records):
    """Return ``{id: '宿舍 > 书桌 > 第二个抽屉'}`` for location records.

    Each record needs ``id``, ``name``, and ``parent_id``.
    """
    by_id = {}
    for record in records or []:
        try:
            loc_id = int(record.get('id'))
        except (TypeError, ValueError, AttributeError):
            continue
        by_id[loc_id] = record

    cache = {}

    def path_of(loc_id):
        if loc_id in cache:
            return cache[loc_id]
        names = []
        seen = set()
        current = loc_id
        while current and current not in seen:
            seen.add(current)
            node = by_id.get(current)
            if not node:
                break
            name = str(node.get('name') or '').strip()
            if name:
                names.append(name)
            parent_id = node.get('parent_id')
            try:
                current = int(parent_id) if parent_id not in (None, '') else None
            except (TypeError, ValueError):
                current = None
        names.reverse()
        text = ' > '.join(names)
        cache[loc_id] = text
        return text

    return {loc_id: path_of(loc_id) for loc_id in by_id}


def match_location_id(raw, paths):
    """Resolve a location id from an id, a name, or a full path.

    Returns ``(location_id, error_message)``. ``error_message`` is None on success.
    Empty input returns ``(None, None)``.
    """
    if raw is None:
        return None, None
    token = str(raw).strip()
    if not token:
        return None, None
    if not paths:
        return None, f'找不到位置：{token}'

    if token.isdigit():
        loc_id = int(token)
        if loc_id in paths:
            return loc_id, None
        return None, f'找不到位置 id={token}'

    normalized = token.replace('＞', '>').replace('/', '>')
    parts = [part.strip() for part in normalized.split('>') if part.strip()]
    target = ' > '.join(parts)
    exact = [loc_id for loc_id, path in paths.items() if path == target]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f'位置路径不唯一：{target}'

    leaf = parts[-1] if parts else token
    name_hits = []
    for loc_id, path in paths.items():
        leaf_name = path.split(' > ')[-1] if path else ''
        if leaf_name == leaf:
            name_hits.append(loc_id)
    if len(name_hits) == 1:
        return name_hits[0], None
    if len(name_hits) > 1:
        return None, f'位置名称不唯一：{leaf}，请写成完整路径（例如 宿舍 > 书桌）'
    return None, f'找不到位置：{token}'


def normalize_item_name(name):
    text = str(name or '').strip().lower()
    return _NAME_NOISE.sub('', text)


def cluster_near_duplicates(records, ratio=0.86):
    """Group near-duplicate names inside the same category.

    ``records`` are dicts with ``id``, ``name``, ``category``, and optionally
    ``disposition`` / ``status``. Items already marked 已处理 or 舍弃 are skipped.
    """
    grouped = {}
    for record in records or []:
        disposition = str(record.get('disposition') or '').strip()
        status = str(record.get('status') or '').strip()
        if disposition == '已处理' or status == '舍弃':
            continue
        category = str(record.get('category') or '').strip().lower()
        grouped.setdefault(category, []).append(record)

    clusters = []
    for members in grouped.values():
        count = len(members)
        if count < 2:
            continue
        parent = list(range(count))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left, right):
            root_left = find(left)
            root_right = find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        norms = [normalize_item_name(member.get('name')) for member in members]
        for left in range(count):
            if not norms[left]:
                continue
            for right in range(left + 1, count):
                if not norms[right]:
                    continue
                name_left = norms[left]
                name_right = norms[right]
                if name_left == name_right:
                    union(left, right)
                    continue
                short, long = (name_left, name_right) if len(name_left) <= len(name_right) else (name_right, name_left)
                if len(short) >= 2 and short in long and (len(long) - len(short)) <= 4:
                    union(left, right)
                    continue
                if len(name_left) >= 2 and len(name_right) >= 2:
                    if SequenceMatcher(None, name_left, name_right).ratio() >= ratio:
                        union(left, right)

        buckets = {}
        for index in range(count):
            if not norms[index]:
                continue
            buckets.setdefault(find(index), []).append(members[index])
        for bucket in buckets.values():
            if len(bucket) >= 2:
                clusters.append(bucket)
    return clusters
