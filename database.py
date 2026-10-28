import sqlite3
import json
import logging
import os
from pathlib import Path
from contextlib import closing
from config import MASTER_ADMIN_ID
from service_catalog import PLANS, ensure_catalog_schema, capture_order_title, catalog_title, stock_available
from customer_experience import ensure_payment_schema, current_card_index, record_successful_approval

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_name="bot_data.db"):
        # Use a stable absolute file path, independent of the current working directory.
        self.db_name = str(Path(os.getenv("BOT_DB_PATH") or db_name).expanduser().resolve())
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA journal_mode=WAL')
        cursor = conn.cursor()
        # جدول سفارشات (بدون فیلدهای حجم)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                receipt_photo TEXT,
                service_link TEXT
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")

        # جدول کاربران
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_joined ON users(joined_at)")

        # جدول وضعیت‌های کاربری
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # جدول کاربران بلاک‌کننده ربات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # جدول کاربران بلاک شده توسط ادمین
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reason TEXT
            )
        ''')

        # جدول اطلاعیه سنجاق‌شده
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1
            )
        ''')

        # جدول وضعیت موجودی پلن‌ها
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plan_status (
                plan_id TEXT PRIMARY KEY,
                available BOOLEAN DEFAULT 1,
                out_of_stock_message TEXT
            )
        ''')
        for plan_id in PLANS.keys():
            cursor.execute('INSERT OR IGNORE INTO plan_status (plan_id, available) VALUES (?, ?)', (plan_id, 1))

        # جدول ادمین‌ها
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)', (MASTER_ADMIN_ID, MASTER_ADMIN_ID))

        ensure_payment_schema(conn)
        ensure_catalog_schema(conn)
        conn.commit()
        conn.close()

    # ========== متدهای سفارش ==========
    def create_order(self, user_id, plan_id, amount):
        with closing(sqlite3.connect(self.db_name, timeout=10)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            catalogue=conn.execute('SELECT price,available,archived FROM service_catalog WHERE plan_id=?',(plan_id,)).fetchone()
            if catalogue:
                status=conn.execute('SELECT available FROM plan_status WHERE plan_id=?',(plan_id,)).fetchone()
                if not catalogue[1] or catalogue[2] or (status and not status[0]):
                    raise ValueError('سرویس ناموجود است.')
                amount=int(catalogue[0])
            card_index = current_card_index(conn)
            cursor = conn.execute('''INSERT INTO orders(user_id,plan_name,amount,payment_card_index)
                VALUES(?,?,?,?)''', (user_id, plan_id, amount, card_index))
            capture_order_title(conn,'bale',cursor.lastrowid,plan_id)
            return cursor.lastrowid

    def receipt_service_available(self,order_id,user_id):
        with closing(sqlite3.connect(self.db_name,timeout=10)) as conn:
            order=conn.execute("""SELECT plan_name FROM orders WHERE id=? AND user_id=?
                AND status='pending' AND receipt_photo IS NULL""",(order_id,user_id)).fetchone()
            return bool(order and stock_available(conn,order[0]))

    def save_receipt(self, order_id, photo_id, user_id):
        """Atomic owner, stock and duplicate-check on the same transaction."""
        if not photo_id:return False
        with closing(sqlite3.connect(self.db_name,timeout=10)) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            order=conn.execute("""SELECT plan_name FROM orders WHERE id=? AND user_id=?
                AND status='pending' AND receipt_photo IS NULL""",(order_id,user_id)).fetchone()
            if not order or not stock_available(conn,order[0]):return False
            result=conn.execute("""UPDATE orders SET receipt_photo=? WHERE id=? AND user_id=?
                AND status='pending' AND receipt_photo IS NULL""",(photo_id,order_id,user_id))
            return result.rowcount==1

    def get_pending_orders(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE status = "pending" AND receipt_photo IS NOT NULL ORDER BY created_at DESC')
        orders = cursor.fetchall()
        conn.close()
        return orders

    def get_order_by_id(self, order_id):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = cursor.fetchone()
        conn.close()
        return order

    def get_user_orders(self, user_id):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
        orders = cursor.fetchall()
        conn.close()
        return orders

    def approve_order(self, order_id, service_link):
        """Exactly one pending order with receipt can be approved."""
        link = str(service_link or '').strip()
        if not link or len(link) > 4096:
            return False
        with closing(sqlite3.connect(self.db_name, timeout=10)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            cur = conn.execute('''
                UPDATE orders SET status = 'approved', service_link = ?
                WHERE id = ? AND status = 'pending' AND receipt_photo IS NOT NULL
            ''', (link, order_id))
            if cur.rowcount == 1:
                record_successful_approval(conn)
            return cur.rowcount == 1

    def reject_order(self, order_id):
        with closing(sqlite3.connect(self.db_name, timeout=10)) as conn, conn:
            cur = conn.execute('''
                UPDATE orders SET status = 'rejected'
                WHERE id = ? AND status = 'pending' AND receipt_photo IS NOT NULL
            ''', (order_id,))
            return cur.rowcount == 1

    def cancel_order(self, order_id, user_id):
        """Never cancel an order not owned by the callback sender."""
        with closing(sqlite3.connect(self.db_name, timeout=10)) as conn, conn:
            cur = conn.execute('''
                UPDATE orders SET status = 'cancelled'
                WHERE id = ? AND user_id = ? AND status = 'pending'
            ''', (order_id, user_id))
            return cur.rowcount == 1

    def order_title(self, order_id, plan_id):
        with closing(sqlite3.connect(self.db_name,timeout=10)) as conn:
            return catalog_title(conn,'bale',order_id,plan_id)

    # ========== متدهای کاربر ==========
    def save_user(self, user_id, username, first_name, last_name=None):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name))
        conn.commit()
        conn.close()
        if hasattr(self, '_user_cache') and user_id in self._user_cache:
            del self._user_cache[user_id]

    def get_user_info(self, user_id, use_cache=True):
        if use_cache:
            if not hasattr(self, '_user_cache'):
                self._user_cache = {}
                self._user_cache_time = {}
            import time
            now = time.time()
            if user_id in self._user_cache and (now - self._user_cache_time.get(user_id, 0)) < 300:
                return self._user_cache[user_id]
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, first_name, last_name, joined_at FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        conn.close()
        if user:
            data = {
                "user_id": user[0],
                "username": user[1] or "ندارد",
                "first_name": user[2] or "کاربر",
                "last_name": user[3] or "",
                "joined_at": user[4]
            }
        else:
            data = None
        if use_cache and data:
            self._user_cache[user_id] = data
            self._user_cache_time[user_id] = time.time()
        return data

    def get_all_users(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = [row[0] for row in cursor.fetchall()]
        conn.close()
        return users

    # ========== متدهای state ==========
    def set_user_state(self, user_id, state, data=None):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        json_data = json.dumps(data) if data else None
        cursor.execute('''
            INSERT OR REPLACE INTO user_states (user_id, state, data, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, state, json_data))
        conn.commit()
        conn.close()

    def get_user_state(self, user_id):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT state, data FROM user_states WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            state = row[0]
            data = json.loads(row[1]) if row[1] else None
            return state, data
        return None, None

    def delete_user_state(self, user_id):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_states WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()

    # ========== آمار ==========
    def get_stats(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM orders')
        total_orders = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'approved'")
        approved_orders = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
        pending_orders = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'rejected'")
        rejected_orders = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(amount) FROM orders WHERE status = 'approved'")
        total_revenue = cursor.fetchone()[0] or 0
        conn.close()
        return {
            'total_users': total_users,
            'total_orders': total_orders,
            'approved_orders': approved_orders,
            'pending_orders': pending_orders,
            'rejected_orders': rejected_orders,
            'total_revenue': total_revenue
        }

    # ========== متدهای بلاک ==========
    def add_blocked_user(self, user_id):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)', (user_id,))
        conn.commit()
        conn.close()

    def get_blocked_users(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, blocked_at FROM blocked_users ORDER BY blocked_at DESC')
        users = cursor.fetchall()
        conn.close()
        return users

    def add_admin_block(self, user_id, reason=""):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO admin_blocked_users (user_id, reason) VALUES (?, ?)', (user_id, reason))
        conn.commit()
        conn.close()

    def remove_admin_block(self, user_id):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM admin_blocked_users WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()

    def is_user_blocked_by_admin(self, user_id):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM admin_blocked_users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None

    def get_admin_blocked_list(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, blocked_at, reason FROM admin_blocked_users ORDER BY blocked_at DESC')
        users = cursor.fetchall()
        conn.close()
        return users

    # ========== متدهای اطلاعیه ==========
    def get_active_announcement(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT message FROM announcements WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def set_announcement(self, message):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('UPDATE announcements SET is_active = 0 WHERE is_active = 1')
        cursor.execute('INSERT INTO announcements (message, is_active) VALUES (?, 1)', (message,))
        conn.commit()
        conn.close()

    def clear_announcement(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('UPDATE announcements SET is_active = 0 WHERE is_active = 1')
        conn.commit()
        conn.close()

    # ========== متدهای وضعیت پلن ==========
    def get_plan_status(self,plan_id):
        with closing(sqlite3.connect(self.db_name,timeout=10)) as conn:
            valid=stock_available(conn,plan_id)
            row=conn.execute('SELECT available,out_of_stock_message FROM plan_status WHERE plan_id=?',
                             (plan_id,)).fetchone()
            # Preserve the legacy notice flow for the historical single-plan alias.
            # Receipt acceptance still uses the strict atomic stock_available gate.
            if not row and plan_id=='UNLIMITED_SINGLE':
                valid=True
            return valid,(row[1] if row else None)

    def set_plan_availability(self, plan_id, available, message=None, admin_id=0):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO plan_status (plan_id, available, out_of_stock_message)
            VALUES (?, ?, ?)
        ''', (plan_id, available, message))
        existing=conn.execute('SELECT available,revision FROM service_catalog WHERE plan_id=?',(plan_id,)).fetchone()
        conn.execute('''UPDATE service_catalog SET available=?,revision=revision+1,
            updated_at=CURRENT_TIMESTAMP WHERE plan_id=? AND available<>?''',
            (int(bool(available)),plan_id,int(bool(available))))
        if existing and bool(existing[0])!=bool(available):
            conn.execute('''INSERT INTO service_catalog_audit
                (admin_id,plan_id,operation,before_json,after_json)
                VALUES(?,?,?,?,?)''', (int(admin_id),plan_id,'bale_stock',
                json.dumps({'available':bool(existing[0])}),json.dumps({'available':bool(available)})))
        conn.commit()
        conn.close()

    def get_all_plans_status(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT plan_id, available, out_of_stock_message FROM plan_status')
        rows = cursor.fetchall()
        conn.close()
        return {row[0]: {"available": row[1], "message": row[2]} for row in rows}

    # ========== متدهای مدیریت ادمین ==========
    def is_admin(self, user_id):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None

    def add_admin(self, user_id, added_by):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)', (user_id, added_by))
        conn.commit()
        conn.close()
        return cursor.rowcount > 0

    def remove_admin(self, user_id):
        if user_id == MASTER_ADMIN_ID:
            return False
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return cursor.rowcount > 0

    def get_all_admins(self):
        conn = sqlite3.connect(self.db_name, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, added_by, added_at FROM admins ORDER BY added_at ASC')
        admins = cursor.fetchall()
        conn.close()
        return admins

    def block_all_blocked_users(self, admin_id):
        blocked_users = self.get_blocked_users()
        count = 0
        for user_id, _ in blocked_users:
            if self.add_admin_block(user_id, reason="بلاک خودکار به دلیل بلاک کردن ربات"):
                count += 1
        return count