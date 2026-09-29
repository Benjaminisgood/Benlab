#!/usr/bin/env python3
"""Smoke-test bulk entry, the token API, declutter, and location-path search.

Uses a temporary SQLite file and a temporary attachments folder. It does not
open instance/lab.db.
"""
import os
import sqlite3
import sys
import tempfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from inventory_ops import (  # noqa: E402
    build_location_paths,
    cluster_near_duplicates,
    match_location_id,
    normalize_disposition,
    parse_bulk_lines,
    parse_csv_text,
)


def check(condition, message):
    if not condition:
        raise SystemExit(f'FAIL: {message}')
    print(f'OK {message}')


def visible_html(response):
    return response.get_data(as_text=True).replace('&gt;', '>').replace('&#62;', '>')


def test_parsers():
    rows, errors = parse_bulk_lines(
        '充电器\n鼠标 | 1 | 电子 | 常用,桌面\n# 注释\n\nUSB线 | 3 | 电子 | 线材 | 正常 | 闲置'
    )
    check(not errors, f'多行解析无错误：{errors}')
    check(len(rows) == 3, '多行解析得到 3 个物品')
    check(rows[0]['name'] == '充电器', '只有名称的行能录入')
    check(rows[1]['quantity_desc'] == '1' and rows[1]['category'] == '电子', '竖线字段顺序')
    check(rows[1]['tags'] == ['常用', '桌面'], '标签按逗号拆开')
    check(rows[2]['disposition'] == '闲置', '行内处置字段')

    header_rows, header_errors = parse_bulk_lines('名称 | 数量 | 类别\n台灯 | 1 | 家居\n')
    check(not header_errors and len(header_rows) == 1 and header_rows[0]['name'] == '台灯', '多行表头被识别')

    csv_rows, csv_errors = parse_csv_text('名称,数量,类别,标签,处置,位置\n台灯,1,家居,照明,想买,宿舍 > 书桌\n')
    check(not csv_errors and csv_rows[0]['location'] == '宿舍 > 书桌', 'CSV 列名映射')
    check(csv_rows[0]['tags'] == ['照明'] and csv_rows[0]['disposition'] == '想买', 'CSV 标签和处置')
    check(normalize_disposition('拥有') == '在用', '拥有 映射为 在用')
    check(normalize_disposition('待购') == '想买', '待购 映射为 想买')
    check(normalize_disposition('卖掉了') is None, '未知处置不会被悄悄收下')

    paths = build_location_paths([
        {'id': 1, 'name': '宿舍', 'parent_id': None},
        {'id': 2, 'name': '书桌', 'parent_id': 1},
        {'id': 3, 'name': '第二个抽屉', 'parent_id': 2},
        {'id': 4, 'name': '书桌', 'parent_id': None},
        {'id': 5, 'name': '书桌', 'parent_id': None},
    ])
    check(paths[3] == '宿舍 > 书桌 > 第二个抽屉', '位置路径从父到子')
    loc_id, error = match_location_id('宿舍 > 书桌 > 第二个抽屉', paths)
    check(loc_id == 3 and error is None, '完整路径能定位到抽屉')
    loc_id, error = match_location_id('书桌', paths)
    check(loc_id is None and error and '不唯一' in error, f'重名位置要求写完整路径：{error}')
    loc_id, error = match_location_id('第二个抽屉', paths)
    check(loc_id == 3 and error is None, '唯一的短名称可以直接命中')

    clusters = cluster_near_duplicates([
        {'id': 1, 'name': '数据线', 'category': '线材', 'status': '正常', 'disposition': ''},
        {'id': 2, 'name': '数据线（黑）', 'category': '线材', 'status': '正常', 'disposition': ''},
        {'id': 3, 'name': '马克杯', 'category': '杯子', 'status': '正常', 'disposition': ''},
        {'id': 4, 'name': '旧杯子', 'category': '杯子', 'status': '舍弃', 'disposition': ''},
    ])
    names = sorted(member['name'] for member in clusters[0]) if clusters else []
    check(names == ['数据线', '数据线（黑）'], f'同类别近名并成一组：{names}')


def prepare_legacy_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE items (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          detail_refs TEXT,
          category TEXT,
          status TEXT,
          features TEXT,
          value REAL,
          quantity_desc TEXT,
          purchase_date DATE,
          notes TEXT,
          last_modified DATETIME,
          purchase_link TEXT,
          stock_status TEXT,
          quantity REAL,
          unit TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO items
          (name, category, status, stock_status, features, quantity, unit, quantity_desc, last_modified)
        VALUES ('旧台灯', '家居', '', '正常', '私人', 2, '个', '', '2020-01-01 00:00:00')
        """
    )
    conn.commit()
    conn.close()


def test_app(db_path, attachments):
    os.environ['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}?timeout=30'
    os.environ['BENLAB_STORAGE_MODE'] = 'local'
    os.environ['BENLAB_API_TOKEN'] = 'smoke-token'
    os.environ['ATTACHMENTS_FOLDER'] = str(attachments)
    os.environ['ATTACHMENTS_CLEANUP_ON_START'] = '0'
    os.environ['DB_BACKUP_ON_START'] = '0'
    os.environ['FLASK_SECRET_KEY'] = 'smoke-secret'

    import app as benlab

    benlab.app.config['TESTING'] = True
    client = benlab.app.test_client()
    auth = {'Authorization': 'Bearer smoke-token'}

    login_page = client.get('/login')
    check(login_page.status_code == 200, '登录页能打开并完成启动迁移')

    with benlab.app.app_context():
        cols = {
            row[1]
            for row in benlab.db.session.execute(benlab.text('PRAGMA table_info(items)'))
        }
    check('disposition' in cols and 'tags' in cols, '启动迁移补上 disposition 和 tags')
    check('stock_status' not in cols and 'quantity' not in cols, '旧的 stock_status/quantity 列被收敛掉')

    legacy = client.get('/api/v1/items?q=旧台灯', headers=auth)
    check(legacy.status_code == 200, '令牌可以列出物品')
    legacy_items = legacy.get_json()['items']
    check(len(legacy_items) == 1, '旧台灯还在')
    check(legacy_items[0]['status'] == '正常', f"旧 stock_status 并入 status：{legacy_items[0]['status']}")
    check('2' in (legacy_items[0]['quantity_desc'] or ''), f"数量描述保留数字：{legacy_items[0]['quantity_desc']}")

    denied = client.get('/api/v1/items')
    check(denied.status_code == 401, '没有令牌时拒绝访问')

    dorm = client.post('/api/v1/locations', json={'name': '宿舍'}, headers=auth)
    desk = client.post('/api/v1/locations', json={'name': '书桌', 'parent_id': dorm.get_json()['location']['id']}, headers=auth)
    drawer = client.post(
        '/api/v1/locations',
        json={'name': '第二个抽屉', 'parent_id': desk.get_json()['location']['id']},
        headers=auth,
    )
    check(dorm.status_code == 201 and drawer.status_code == 201, '可以按层级创建位置')
    drawer_body = drawer.get_json()['location']
    check(drawer_body['path'] == '宿舍 > 书桌 > 第二个抽屉', f"位置路径：{drawer_body['path']}")
    drawer_id = drawer_body['id']

    created = client.post('/api/v1/items', json={
        'name': '充电器',
        'category': '电子',
        'tags': ['常用', '桌面'],
        'location_id': drawer_id,
        'disposition': '拥有',
    }, headers=auth)
    check(created.status_code == 201, '创建充电器')
    charger = created.get_json()['items'][0]
    check(charger['disposition'] == '在用', '拥有 被收成 在用')
    check(charger['locations'][0]['path'] == '宿舍 > 书桌 > 第二个抽屉', '新建物品带回完整路径')

    found = client.get('/api/v1/search?q=充电器', headers=auth).get_json()
    check(found['items'] and found['items'][0]['locations'][0]['path'].endswith('第二个抽屉'), '搜索充电器能看到抽屉')
    by_place = client.get('/api/v1/search?q=' + '第二个抽屉', headers=auth).get_json()
    check(any(item['name'] == '充电器' for item in by_place['items']), '用位置路径片段也能搜到物品')

    batch = client.post('/api/v1/items', json={
        'text': '鼠标 | 1 | 电子 | 常用,桌面\n# skip\nUSB线 | 3 | 电子 | 线材 | 正常 | 闲置',
        'location_id': drawer_id,
    }, headers=auth)
    check(batch.status_code == 201 and batch.get_json()['count'] == 2, 'text 批量录入两条')

    dupes = client.post('/api/v1/items', json={'items': [
        {'name': '数据线', 'category': '线材', 'location_id': drawer_id},
        {'name': '数据线（黑）', 'category': '线材', 'location_id': drawer_id},
        {'name': '想要的键盘', 'disposition': '想买'},
        {'name': '旧杂志', 'category': '书'},
    ]}, headers=auth)
    check(dupes.status_code == 201 and dupes.get_json()['count'] == 4, '一次创建多件物品')

    with benlab.app.app_context():
        magazine = benlab.Item.query.filter_by(name='旧杂志').one()
        magazine.last_modified = benlab.datetime(2019, 1, 1)
        benlab.db.session.commit()

    report = client.get('/api/v1/declutter?days=30', headers=auth).get_json()
    no_loc = {item['name'] for item in report['no_location']}
    marked = {item['name'] for item in report['marked']}
    stale = {item['name'] for item in report['stale']}
    check('旧杂志' in no_loc and '旧台灯' in no_loc, f'无位置候选：{no_loc}')
    check('想要的键盘' not in no_loc, '想买不进无位置候选')
    check('USB线' in marked, f'闲置进入待处理：{marked}')
    check('旧杂志' in stale, '很久没更新的物品会被列出')
    dup_names = [sorted(item['name'] for item in group) for group in report['duplicates']]
    check(['数据线', '数据线（黑）'] in dup_names, f'断舍离里能看到近名：{dup_names}')

    usb = next(item for item in report['marked'] if item['name'] == 'USB线')
    updated = client.post('/api/v1/items/disposition', json={
        'ids': [usb['id']],
        'disposition': '已处理',
    }, headers=auth)
    check(updated.status_code == 200 and usb['id'] in updated.get_json()['updated'], '批量标成已处理')
    report_after = client.get('/api/v1/declutter?days=30', headers=auth).get_json()
    check(all(item['name'] != 'USB线' for item in report_after['marked']), '已处理的物品离开候选')

    patched = client.patch(f"/api/v1/items/{charger['id']}", json={'tags': '常用、出差'}, headers=auth)
    check(patched.status_code == 200 and patched.get_json()['item']['tags'] == ['常用', '出差'], 'PATCH 能改标签')

    os.environ['BENLAB_API_TOKEN'] = ''
    disabled = client.get('/api/v1/items')
    check(disabled.status_code == 404, '清空令牌后接口关闭')
    os.environ['BENLAB_API_TOKEN'] = 'smoke-token'

    login = client.post('/login', data={'username': 'admin', 'password': 'admin'}, follow_redirects=True)
    check(login.status_code == 200, 'admin/admin 可以登录')

    bulk_page = client.get(f'/items/bulk?loc_id={drawer_id}')
    bulk_html = visible_html(bulk_page)
    check(bulk_page.status_code == 200 and '快速录入' in bulk_html, '快速录入页')
    check('宿舍 > 书桌 > 第二个抽屉' in bulk_html, '快速录入页列出完整路径')

    entered = client.post('/items/bulk', data={
        'lines': '回形针 | 20 | 文具 | 抽屉\n便签',
        'location_id': str(drawer_id),
        'status': '正常',
        'features': '私人',
        'attachments': (BytesIO(b'\x89PNG\r\n\x1a\nsmoke'), 'drawer.png'),
    }, follow_redirects=True)
    entered_html = visible_html(entered)
    check('已录入 2 个物品' in entered_html, '页面批量录入成功')

    with benlab.app.app_context():
        created_items = benlab.Item.query.filter(benlab.Item.name.in_(['回形针', '便签'])).all()
        check(len(created_items) == 2, '两个物品都写入了')
        filenames = [set(item.attachment_filenames) for item in created_items]
        check(filenames[0] and filenames[0] == filenames[1], f'同一张抽屉照片挂到两件物品：{filenames}')
        shared_name = next(iter(filenames[0]))
        shared_path = attachments / shared_name
        check(shared_path.is_file(), '照片文件在本地 attachments 目录')
        clip = next(item for item in created_items if item.name == '回形针')
        note = next(item for item in created_items if item.name == '便签')
        clip_id = clip.id
        note_id = note.id

    deleted = client.post(f'/items/{clip_id}/delete', follow_redirects=True)
    check(deleted.status_code == 200, '可以删掉其中一个共用照片的物品')
    with benlab.app.app_context():
        remaining = benlab.db.session.get(benlab.Item, note_id)
        check(remaining and shared_name in remaining.attachment_filenames, '另一件物品仍引用这张照片')
        check(shared_path.is_file(), '还有引用时不删除照片文件')

    csv_body = '名称,数量,类别,标签,位置\n台灯,1,家居,照明,宿舍 > 书桌\n'
    imported = client.post('/items/bulk', data={
        'csv_file': (BytesIO(csv_body.encode('utf-8')), 'items.csv'),
        'status': '正常',
        'features': '私人',
    }, follow_redirects=True)
    check('已录入 1 个物品' in visible_html(imported), 'CSV 导入')
    placed = client.get('/api/v1/search?q=台灯', headers=auth).get_json()
    check(placed['items'] and placed['items'][0]['locations'][0]['path'] == '宿舍 > 书桌', 'CSV 里的位置路径被解析')

    find_page = client.get('/find?q=充电器')
    find_html = visible_html(find_page)
    check(find_page.status_code == 200 and '宿舍 > 书桌 > 第二个抽屉' in find_html, '找东西页面显示完整路径')

    declutter_page = client.get('/items/declutter?days=30')
    declutter_html = visible_html(declutter_page)
    check('旧杂志' in declutter_html and 'item_ids' in declutter_html, '断舍离页面列出候选')
    magazine_id = next(item['id'] for item in report_after['no_location'] if item['name'] == '旧杂志')
    marked_done = client.post('/items/declutter', data={
        'item_ids': str(magazine_id),
        'disposition': '转让',
        'days': '30',
    }, follow_redirects=True)
    check('已更新 1 个物品' in visible_html(marked_done), '页面上可以批量改成转让')

    items_page = visible_html(client.get('/items'))
    check('充电器' in items_page and '宿舍 > 书桌 > 第二个抽屉' in items_page, '物品列表显示完整路径')
    detail_page = visible_html(client.get(f"/items/{charger['id']}"))
    check('处置' in detail_page and '宿舍 > 书桌 > 第二个抽屉' in detail_page, '物品详情显示处置和路径')
    add_page = visible_html(client.get('/items/add'))
    check('处置（断舍离）' in add_page and '标签' in add_page, '单件表单可以填处置和标签')


def main():
    test_parsers()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        db_path = temp / 'legacy.db'
        attachments = temp / 'attachments'
        attachments.mkdir()
        prepare_legacy_db(db_path)
        test_app(db_path, attachments)
    print('ALL SMOKE CHECKS PASSED')


if __name__ == '__main__':
    main()
