"""SQLite backed shared service catalogue for Bale + Telegram (no cache, no network)."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
import config
import sys

def defaults():
    return sys.modules["config"].PLANS

CATEGORIES = ('طلایی', 'بین‌الملل', 'الترا', 'تک کاربره نامحدود')
FIELDS = ('display', 'type', 'size', 'price', 'users', 'duration')


def db_path():
    return str(Path(os.getenv('BOT_DB_PATH') or (Path(__file__).resolve().parent / 'bot_data.db')).expanduser().resolve())


def ensure_catalog_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS service_catalog (
        plan_id TEXT PRIMARY KEY,
        display TEXT NOT NULL, type TEXT NOT NULL,
        size INTEGER NOT NULL CHECK(size BETWEEN 100 AND 1048576),
        price INTEGER NOT NULL CHECK(price BETWEEN 1000 AND 100000000),
        users TEXT NOT NULL, duration TEXT NOT NULL DEFAULT '',
        available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0,1)),
        archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
        deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1,
        create_request TEXT UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    existing_columns={c[1] for c in conn.execute('PRAGMA table_info(service_catalog)')}
    if 'deleted' not in existing_columns:
        conn.execute('ALTER TABLE service_catalog ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0')
    conn.execute('''CREATE TABLE IF NOT EXISTS service_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS service_category_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        operation TEXT NOT NULL,
        before_json TEXT, after_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS service_catalog_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL, plan_id TEXT NOT NULL,
        operation TEXT NOT NULL, before_json TEXT, after_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS plan_status (
        plan_id TEXT PRIMARY KEY, available INTEGER DEFAULT 1, out_of_stock_message TEXT)''')
    # One-time seeding; never overwrite prices/stock set by an administrator.
    for pid, p in defaults().items():
        conn.execute('''INSERT OR IGNORE INTO service_catalog
            (plan_id,display,type,size,price,users,duration,available)
            VALUES (?,?,?,?,?,?,?,COALESCE((SELECT available FROM plan_status WHERE plan_id=?),1))''',
            (pid, p['display'], p['type'], int(p['size']), int(p['price']),
             p.get('users', 'بدون محدودیت کاربر'), str(p.get('duration', '') or p.get('validity', '')),pid))
    # Seed categories once. Deleted categories remain tombstoned after a restart.
    for category in CATEGORIES:
        conn.execute('INSERT OR IGNORE INTO service_categories(name) VALUES(?)',(category,))
    conn.execute('''INSERT OR IGNORE INTO service_categories(name)
                    SELECT DISTINCT type FROM service_catalog''')
    # For historical compatibility Bale's availability API still consults plan_status.
    conn.execute('''INSERT OR IGNORE INTO plan_status(plan_id,available)
        SELECT plan_id,available FROM service_catalog''')
    conn.execute('''CREATE TABLE IF NOT EXISTS service_order_titles (
        platform TEXT NOT NULL, order_id INTEGER NOT NULL, display TEXT NOT NULL,
        PRIMARY KEY(platform,order_id))''')
    for platform, table, field in (('telegram','tg_orders','plan_id'),('bale','orders','plan_name')):
        table_exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
        columns={x[1] for x in conn.execute(f'PRAGMA table_info({table})')} if table_exists else set()
        if table_exists and field in columns:
            conn.execute(f"""INSERT OR IGNORE INTO service_order_titles(platform,order_id,display)
                SELECT ?,o.id,COALESCE((SELECT display FROM service_catalog WHERE plan_id=o.{field}),o.{field})
                FROM {table} AS o""",(platform,))


def read_catalog(path=None, include_archived=False):
    path = path or db_path()
    result = dict(defaults())
    if not Path(path).is_file():
        return result
    with closing(sqlite3.connect(path, timeout=5)) as conn:
        table=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_catalog'").fetchone()
        if not table:return result
        columns={r[1] for r in conn.execute('PRAGMA table_info(service_catalog)')}
        # Startup compatibility: Bale seeds plan_status before running additive migrations.
        deleted_column='deleted' if 'deleted' in columns else '0'
        rows=conn.execute(f'''SELECT plan_id,display,type,size,price,users,duration,archived,{deleted_column}
                                    FROM service_catalog ORDER BY rowid''').fetchall()
        category_table=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_categories'").fetchone()
        hidden_types={r[0] for r in conn.execute('SELECT name FROM service_categories WHERE deleted=1')} if category_table else set()
        for pid, display, typ, size, price, users, duration, archived, deleted in rows:
            if deleted or typ in hidden_types or (archived and not include_archived):
                result.pop(pid, None)
            else:
                p = {'display':display,'type':typ,'size':size,'price':price,'users':users}
                if duration:p['duration']=duration
                result[pid]=p
    return result


class LivePlans(Mapping):
    def snapshot(self):return read_catalog()
    def __getitem__(self, k):return self.snapshot()[k]
    def __iter__(self):return iter(self.snapshot())
    def __len__(self):return len(self.snapshot())
    def get(self,k,default=None):return self.snapshot().get(k,default)
    def items(self):return self.snapshot().items()
    def values(self):return self.snapshot().values()
    def keys(self):return self.snapshot().keys()
    def __contains__(self,k):return k in self.snapshot()
    # Compatibility with old offline tests that mutate the stub config catalogue.
    def __setitem__(self,k,v):defaults()[k]=v
    def __delitem__(self,k):del defaults()[k]

PLANS=LivePlans()


def catalog_title(conn,platform,oid,pid):
    row=conn.execute('SELECT display FROM service_order_titles WHERE platform=? AND order_id=?',(platform,oid)).fetchone()
    if row:return row[0]
    row=conn.execute('SELECT display FROM service_catalog WHERE plan_id=?',(pid,)).fetchone()
    return row[0] if row else str(pid)


def capture_order_title(conn,platform,oid,pid):
    conn.execute('INSERT OR IGNORE INTO service_order_titles(platform,order_id,display) VALUES (?,?,?)',
                 (platform,oid,catalog_title(conn,platform,oid,pid)))


def stock_available(conn, plan_id):
    """Fail-closed, unified availability check on an existing SQLite transaction.

    Checks both admin catalogue and legacy Bale availability so callbacks or receipt
    uploads cannot circumvent the admin's unavailable/archived setting.
    """
    item = conn.execute('SELECT available,archived,deleted FROM service_catalog WHERE plan_id=?',
                        (plan_id,)).fetchone()
    if item and (not bool(item[0]) or bool(item[1]) or bool(item[2])):
        return False
    if not item and plan_id not in defaults():
        return False
    if item:
        active_type=conn.execute('''SELECT deleted FROM service_categories WHERE name=(SELECT type FROM service_catalog WHERE plan_id=?)''',(plan_id,)).fetchone()
        if active_type and active_type[0]:return False
    legacy = conn.execute('SELECT available FROM plan_status WHERE plan_id=?',
                          (plan_id,)).fetchone()
    return legacy is None or bool(legacy[0])


class CatalogError(ValueError):pass


def validate(name, value):
    if name == 'price':
        raw = str(value).replace(',', '').strip()
        if not raw.isascii() or not raw.isdecimal():raise CatalogError('قیمت باید عدد لاتین باشد.')
        value=int(raw)
        if not 1000 <= value <= 100000000:raise CatalogError('قیمت باید بین 1,000 و 100,000,000 تومان باشد.')
        return value
    if name=='size':
        raw=str(value).strip()
        if not raw.isascii() or not raw.isdecimal():raise CatalogError('حجم را به گیگابایت و با عدد لاتین بنویسید.')
        value=int(raw)
        if not 1 <= value <= 1024:raise CatalogError('حجم باید بین 1 تا 1024 گیگابایت باشد.')
        return value*1024
    if name=='type':
        value=str(value).strip()
        if not 2<=len(value)<=45 or any(ord(ch)<32 for ch in value):
            raise CatalogError('نام دسته معتبر نیست.')
        return value
    value=str(value).strip()
    limits={'display':(3,80),'users':(3,60),'duration':(0,40)}
    if name not in limits:raise CatalogError('این فیلد قابل ویرایش نیست.')
    low,high=limits[name]
    if not low <= len(value) <= high or any(ord(ch)<32 for ch in value):
        raise CatalogError('طول یا محتوای ورودی معتبر نیست.')
    return value


class CatalogStore:
    def __init__(self,path=None):self.path=str(Path(path or db_path()).resolve())
    def connection(self):
        conn=sqlite3.connect(self.path, timeout=10)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA busy_timeout=10000')
        return conn
    def initialize(self):
        with closing(self.connection()) as conn,conn:
            ensure_catalog_schema(conn)
    def all(self, include_archived=False):
        with closing(self.connection()) as conn:
            rows=conn.execute('SELECT * FROM service_catalog WHERE deleted=0 '+('' if include_archived else 'AND archived=0 ')+'ORDER BY rowid').fetchall()
            return [dict(row) for row in rows]
    def get(self,pid):
        with closing(self.connection()) as conn:
            row=conn.execute('SELECT * FROM service_catalog WHERE plan_id=?',(pid,)).fetchone()
            return dict(row) if row else None
    def categories(self, include_deleted=False):
        with closing(self.connection()) as conn:
            return [dict(r) for r in conn.execute(
                'SELECT * FROM service_categories '+('' if include_deleted else 'WHERE deleted=0 ')+ 'ORDER BY id')]
    def category(self, category_id):
        with closing(self.connection()) as conn:
            row=conn.execute('SELECT * FROM service_categories WHERE id=?',(category_id,)).fetchone()
            return dict(row) if row else None
    @staticmethod
    def _check_category(conn, name):
        row=conn.execute('SELECT id,deleted FROM service_categories WHERE name=?',(name,)).fetchone()
        if not row or row['deleted']:raise CatalogError('این دسته وجود ندارد یا حذف شده است.')
        return row['id']
    @staticmethod
    def _cat_audit(conn,admin,category_id,action,before=None,after=None):
        conn.execute('''INSERT INTO service_category_audit(admin_id,category_id,operation,before_json,after_json)
            VALUES(?,?,?,?,?)''',(int(admin),category_id,action,
                 json.dumps(before,ensure_ascii=False) if before else None,
                 json.dumps(after,ensure_ascii=False) if after else None))
    def create_category(self,admin,name):
        name=validate('type',name)
        with closing(self.connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            exists=conn.execute('SELECT * FROM service_categories WHERE name=?',(name,)).fetchone()
            if exists and not exists['deleted']:raise CatalogError('این دسته از قبل وجود دارد.')
            if exists:
                conn.execute('UPDATE service_categories SET deleted=0,revision=revision+1 WHERE id=?',(exists['id'],))
                cid=exists['id']
            else:
                cid=conn.execute('INSERT INTO service_categories(name) VALUES(?)',(name,)).lastrowid
            self._cat_audit(conn,admin,cid,'create_category',dict(exists) if exists else None,{'name':name})
            return cid
    def delete_category(self,admin,category_id,revision):
        # Remove category and its products from sale while preserving order history.
        with closing(self.connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM service_categories WHERE id=?',(category_id,)).fetchone()
            if not row or row['deleted']:raise CatalogError('دسته پیدا نشد یا قبلاً حذف شده است.')
            if row['revision']!=int(revision):raise CatalogError('دسته تغییر کرده است؛ دوباره تلاش کنید.')
            plans=conn.execute('SELECT * FROM service_catalog WHERE type=? AND deleted=0',(row['name'],)).fetchall()
            conn.execute('UPDATE service_categories SET deleted=1,revision=revision+1 WHERE id=?',(category_id,))
            for plan in plans:
                pid=plan['plan_id']
                conn.execute('''UPDATE service_catalog SET deleted=1, available=0, revision=revision+1,
                    updated_at=CURRENT_TIMESTAMP WHERE plan_id=?''',(pid,))
                conn.execute('''INSERT INTO plan_status(plan_id,available) VALUES(?,0)
                    ON CONFLICT(plan_id) DO UPDATE SET available=0''',(pid,))
                self._audit(conn,admin,pid,'delete_by_category',dict(plan),{'deleted':1})
            self._cat_audit(conn,admin,category_id,'delete_category',dict(row),{'deleted':1,'plans':len(plans)})
            return len(plans)
    def delete_service(self,admin,pid,revision):
        # Permanent removal from storefront; historical orders remain.
        with closing(self.connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            before=conn.execute('SELECT * FROM service_catalog WHERE plan_id=?',(pid,)).fetchone()
            if not before or before['deleted']:raise CatalogError('سرویس قبلاً حذف شده یا وجود ندارد.')
            if before['revision']!=int(revision):raise CatalogError('سرویس تغییر کرده؛ دوباره صفحه را باز کنید.')
            conn.execute('''UPDATE service_catalog SET deleted=1,available=0,revision=revision+1,
                updated_at=CURRENT_TIMESTAMP WHERE plan_id=?''',(pid,))
            conn.execute('''INSERT INTO plan_status(plan_id,available) VALUES(?,0)
                ON CONFLICT(plan_id) DO UPDATE SET available=0''',(pid,))
            self._audit(conn,admin,pid,'delete',dict(before),{'deleted':1})
            conn.execute('UPDATE service_categories SET revision=revision+1 WHERE name=?',(before['type'],))
    def _audit(self,conn,admin,pid,op,before,after):
        conn.execute('INSERT INTO service_catalog_audit(admin_id,plan_id,operation,before_json,after_json) VALUES(?,?,?,?,?)',
            (int(admin),pid,op,json.dumps(before,ensure_ascii=False) if before else None,
             json.dumps(after,ensure_ascii=False) if after else None))
    def create(self,admin,fields,request_id=None):
        data={k:validate(k,fields.get(k,'')) for k in FIELDS if k!='size'}
        size=int(fields.get('size',0))
        if size%1024 or not 1024<=size<=1048576:
            raise CatalogError('حجم نامعتبر است.')
        data['size']=size
        if data['type']=='تک کاربره نامحدود':data['users']='تک کاربره'
        if request_id is not None and (not isinstance(request_id,str) or len(request_id)>64 or len(request_id)<16):
            raise CatalogError('شناسه ثبت نامعتبر است.')
        pid='CUSTOM_'+secrets.token_hex(5).upper()
        with closing(self.connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            category_id=self._check_category(conn,data['type'])
            if request_id:
                existing=conn.execute('SELECT plan_id FROM service_catalog WHERE create_request=?',(request_id,)).fetchone()
                if existing:return existing[0]
            conn.execute('''INSERT INTO service_catalog (plan_id,display,type,size,price,users,duration,create_request)
                VALUES(?,?,?,?,?,?,?,?)''', (pid,*(data[k] for k in FIELDS),request_id))
            conn.execute('INSERT INTO plan_status(plan_id,available) VALUES(?,1)',(pid,))
            self._audit(conn,admin,pid,'create',None,dict(data))
            conn.execute('UPDATE service_categories SET revision=revision+1 WHERE id=?',(category_id,))
        return pid
    def change(self,admin,pid,field,value,revision):
        if field not in FIELDS+('available','archived'):raise CatalogError('عملیات نامعتبر.')
        if field in FIELDS:
            value=validate(field,value)
        else:
            if type(value) is not bool:raise CatalogError('وضعیت معتبر نیست.')
            value=int(value)
        with closing(self.connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            existing=conn.execute('SELECT * FROM service_catalog WHERE plan_id=?',(pid,)).fetchone()
            if not existing or existing['deleted']:raise CatalogError('این سرویس پیدا نشد.')
            original=dict(existing)
            if int(revision)!=original['revision']:raise CatalogError('سرویس توسط مدیر دیگری تغییر کرده؛ صفحه را تازه کنید.')
            if original['archived'] and field!='archived':raise CatalogError('سرویس آرشیوشده را ابتدا فعال کنید.')
            if field=='type':self._check_category(conn,value)
            if field=='archived' and pid in defaults() and value:
                raise CatalogError('پلن‌های اصلی را ناموجود کنید؛ آرشیو آن‌ها مجاز نیست.')
            changed=conn.execute(f'''UPDATE service_catalog SET {field}=?,revision=revision+1,
                updated_at=CURRENT_TIMESTAMP WHERE plan_id=? AND revision=?''',(value,pid,revision))
            if changed.rowcount!=1:raise CatalogError('تغییر هم‌زمان ثبت شد؛ دوباره تلاش کنید.')
            if field=='type' and value!=original['type']:
                conn.execute('UPDATE service_categories SET revision=revision+1 WHERE name IN (?,?)',(original['type'],value))
            if field=='type' and value=='تک کاربره نامحدود':
                conn.execute('UPDATE service_catalog SET users=? WHERE plan_id=?',('تک کاربره',pid))
            if field=='users' and original['type']=='تک کاربره نامحدود':
                raise CatalogError('سرویس تک‌کاربره باید برای یک نفر باقی بماند.')
            if field in ('available','archived'):
                avail=0 if field=='archived' and value else int(value if field=='available' else original['available'])
                conn.execute('''INSERT INTO plan_status(plan_id,available) VALUES(?,?) ON CONFLICT(plan_id)
                    DO UPDATE SET available=excluded.available''',(pid,avail))
            after=conn.execute('SELECT * FROM service_catalog WHERE plan_id=?',(pid,)).fetchone()
            self._audit(conn,admin,pid,'set_'+field,original,dict(after))
        return dict(after)
