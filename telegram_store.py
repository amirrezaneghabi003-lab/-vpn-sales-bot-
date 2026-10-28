"""Telegram-only records in the SAME SQLite file as the Bale bot.

No changes to legacy tables, and no access to Bale user or order namespaces.
Importing this module does not open/write the database.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from customer_experience import ensure_payment_schema, current_card_index, record_successful_approval
from telegram_broadcast import BroadcastStoreMixin
from service_catalog import ensure_catalog_schema, capture_order_title, catalog_title, stock_available


class TelegramStore(BroadcastStoreMixin):
    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=10000')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        # Additive migration only. Never drop, rewrite, or modify any Bale table.
        with self.connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS tg_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tg_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    plan_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'awaiting_receipt'
                        CHECK(status IN ('awaiting_receipt','under_review','approved','rejected','cancelled')),
                    receipt_file_id TEXT,
                    service_link TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    delivered_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES tg_users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tg_orders_user ON tg_orders(user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_tg_orders_status ON tg_orders(status);
                CREATE TABLE IF NOT EXISTS tg_admin_blocks (
                    user_id INTEGER PRIMARY KEY,
                    blocked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tg_admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    target_id INTEGER,
                    result TEXT NOT NULL DEFAULT 'ok',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_tg_users_last_seen ON tg_users(last_seen);
                CREATE TABLE IF NOT EXISTS tg_states (
                    user_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    data TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            ensure_payment_schema(conn)
            ensure_catalog_schema(conn)
        self.initialize_broadcast()

    def upsert_user(self, user_id, username, first_name):
        with self.connection() as conn:
            conn.execute('''
                INSERT INTO tg_users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_seen=CURRENT_TIMESTAMP
            ''', (user_id, username, first_name))
            # Any new private interaction proves the bot has been unblocked.
            # Promotional opt-out stays unchanged.
            conn.execute('''UPDATE tg_bc_prefs SET bot_blocked=0,updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND bot_blocked=1''',(user_id,))

    def user_count(self):
        with self.connection() as conn:
            return conn.execute('SELECT COUNT(*) FROM tg_users').fetchone()[0]

    def get_user(self, user_id):
        with self.connection() as conn:
            r = conn.execute('SELECT * FROM tg_users WHERE user_id=?', (user_id,)).fetchone()
            return dict(r) if r else None

    def all_user_ids(self):
        with self.connection() as conn:
            return [r[0] for r in conn.execute('SELECT user_id FROM tg_users')]

    def get_state(self, user_id):
        with self.connection() as conn:
            r = conn.execute('SELECT state, data FROM tg_states WHERE user_id=?', (user_id,)).fetchone()
            return (r['state'], r['data']) if r else (None, None)

    def set_state(self, user_id, state, data=None):
        with self.connection() as conn:
            conn.execute('''
                INSERT INTO tg_states(user_id,state,data) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                state=excluded.state,data=excluded.data,updated_at=CURRENT_TIMESTAMP
            ''', (user_id, state, str(data) if data is not None else None))

    def clear_state(self, user_id):
        with self.connection() as conn:
            conn.execute('DELETE FROM tg_states WHERE user_id=?', (user_id,))

    def plan_status(self, plan_id):
        with self.connection() as conn:
            valid = stock_available(conn,plan_id)
            message=conn.execute('SELECT out_of_stock_message FROM plan_status WHERE plan_id=?',(plan_id,)).fetchone()
            return valid, message[0] if message else None

    def plan_statuses(self, plan_ids):
        """Batch availability read for fast product listing."""
        ids=list(dict.fromkeys(plan_ids))
        if not ids:return {}
        marks=','.join('?' for _ in ids)
        with self.connection() as conn:
            rows=conn.execute(f"""SELECT c.plan_id,c.available,c.archived,p.available AS legacy,
                p.out_of_stock_message AS reason FROM service_catalog c
                LEFT JOIN plan_status p ON c.plan_id=p.plan_id
                WHERE c.plan_id IN ({marks})""",ids).fetchall()
            from service_catalog import defaults
            result={pid:(pid in defaults(),None) for pid in ids}
            for r in rows:
                result[r['plan_id']]=(bool(r['available']) and not bool(r['archived'])
                    and (r['legacy'] is None or bool(r['legacy'])),r['reason'])
            return result

    def announcement(self):
        with self.connection() as conn:
            try:
                r = conn.execute('SELECT message FROM announcements WHERE is_active=1 ORDER BY id DESC LIMIT 1').fetchone()
            except sqlite3.OperationalError as exc:
                if 'no such table' in str(exc):
                    return None
                raise
            return r[0] if r else None

    def open_order(self, user_id, plan_id, amount):
        """Idempotent: reuse user's unfinished receipt for this plan.

        BEGIN IMMEDIATE serializes the check and insert across processes.
        """
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            r = conn.execute('''
                SELECT * FROM tg_orders WHERE user_id=? AND plan_id=? AND status='awaiting_receipt'
                ORDER BY id DESC LIMIT 1
            ''', (user_id, plan_id)).fetchone()
            if r:
                return dict(r), False
            catalogue=conn.execute('SELECT price,available,archived FROM service_catalog WHERE plan_id=?',(plan_id,)).fetchone()
            if catalogue:
                stock=conn.execute('SELECT available FROM plan_status WHERE plan_id=?',(plan_id,)).fetchone()
                if not catalogue['available'] or catalogue['archived'] or (stock and not stock[0]):
                    raise ValueError('این سرویس اکنون ناموجود است.')
                amount=int(catalogue['price'])  # Never trust a stale callback or passed amount.
            card_index = current_card_index(conn)
            cursor = conn.execute('''
                INSERT INTO tg_orders(user_id,plan_id,amount,payment_card_index) VALUES(?,?,?,?)
            ''', (user_id, plan_id, amount, card_index))
            capture_order_title(conn,'telegram',cursor.lastrowid,plan_id)
            order = conn.execute('SELECT * FROM tg_orders WHERE id=?', (cursor.lastrowid,)).fetchone()
            return dict(order), True

    def order_title(self, order_id, plan_id):
        with self.connection() as conn:
            return catalog_title(conn,'telegram',order_id,plan_id)

    def get_order(self, order_id):
        with self.connection() as conn:
            r = conn.execute('SELECT * FROM tg_orders WHERE id=?', (order_id,)).fetchone()
            return dict(r) if r else None

    def get_user_order(self, order_id, user_id):
        with self.connection() as conn:
            r = conn.execute('SELECT * FROM tg_orders WHERE id=? AND user_id=?', (order_id, user_id)).fetchone()
            return dict(r) if r else None

    def user_orders(self, user_id, limit=8, offset=0):
        with self.connection() as conn:
            rows = conn.execute('SELECT * FROM tg_orders WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
                                (user_id, limit, offset)).fetchall()
            return [dict(r) for r in rows]

    def user_stats(self, user_id):
        with self.connection() as conn:
            r = conn.execute('''
                SELECT COUNT(*) total, COALESCE(SUM(status='approved'),0) approved,
                       COALESCE(SUM(status='under_review'),0) reviewing
                FROM tg_orders WHERE user_id=?
            ''', (user_id,)).fetchone()
            return dict(r)

    def popular_plan_id(self, minimum=3):
        """Only show the bestseller badge after at least 3 approved sales."""
        with self.connection() as conn:
            row = conn.execute("""
                SELECT plan_id,COUNT(*) sales FROM tg_orders
                WHERE status='approved' GROUP BY plan_id
                HAVING COUNT(*)>=? ORDER BY sales DESC, plan_id LIMIT 1
            """, (minimum,)).fetchone()
            return row['plan_id'] if row else None

    def pending_orders(self, limit=15):
        with self.connection() as conn:
            rows = conn.execute('SELECT * FROM tg_orders WHERE status="under_review" ORDER BY id LIMIT ?', (limit,)).fetchall()
            return [dict(r) for r in rows]

    def submit_receipt(self, order_id, user_id, file_id):
        """Race-safe receipt acceptance: stock checked within a write transaction."""
        if not file_id:return False
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order=conn.execute("""SELECT plan_id FROM tg_orders WHERE id=? AND user_id=?
                AND status='awaiting_receipt' AND receipt_file_id IS NULL""",
                (order_id,user_id)).fetchone()
            if not order or not stock_available(conn,order['plan_id']):return False
            result=conn.execute("""UPDATE tg_orders SET status='under_review',
                receipt_file_id=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND user_id=? AND status='awaiting_receipt' AND receipt_file_id IS NULL""",
                (file_id,order_id,user_id))
            return result.rowcount==1

    def cancel_order(self, order_id, user_id):
        with self.connection() as conn:
            r = conn.execute('''
                UPDATE tg_orders SET status='cancelled',updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND user_id=? AND status='awaiting_receipt'
            ''', (order_id, user_id))
            return r.rowcount == 1

    def approve_order(self, order_id, service_link):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            r = conn.execute('''
                UPDATE tg_orders SET status='approved',service_link=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='under_review'
            ''', (service_link, order_id))
            if r.rowcount == 1:
                record_successful_approval(conn)
            return r.rowcount == 1

    def reject_order(self, order_id):
        with self.connection() as conn:
            r = conn.execute('''
                UPDATE tg_orders SET status='rejected',updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='under_review'
            ''', (order_id,))
            return r.rowcount == 1

    def mark_delivered(self, order_id):
        with self.connection() as conn:
            conn.execute('UPDATE tg_orders SET delivered_at=CURRENT_TIMESTAMP WHERE id=? AND status="approved"', (order_id,))

    def admin_stats(self):
        with self.connection() as conn:
            return dict(conn.execute('''
                SELECT COUNT(*) orders,
                       COALESCE(SUM(status='under_review'),0) review,
                       COALESCE(SUM(status='approved'),0) approved,
                       COALESCE(SUM(CASE WHEN status='approved' THEN amount ELSE 0 END),0) revenue
                FROM tg_orders
            ''').fetchone())


    def admin_is_blocked(self, user_id):
        with self.connection() as conn:
            return conn.execute('SELECT 1 FROM tg_admin_blocks WHERE user_id=?', (user_id,)).fetchone() is not None

    def admin_set_block(self, user_id, blocked):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if blocked:
                conn.execute('INSERT OR IGNORE INTO tg_admin_blocks(user_id) VALUES(?)', (user_id,))
            else:
                conn.execute('DELETE FROM tg_admin_blocks WHERE user_id=?', (user_id,))

    def admin_audit(self, admin_id, action, target_id=None, result='ok'):
        with self.connection() as conn:
            conn.execute('INSERT INTO tg_admin_audit(admin_id,action,target_id,result) VALUES(?,?,?,?)',
                         (admin_id, action, target_id, result))

    def search_users(self, search, limit=12):
        limit = max(1, min(int(limit), 20))
        search = (search or '').strip().lstrip('@')[:80]
        if not search:
            return []
        with self.connection() as conn:
            if search.isdecimal() and len(search) <= 15:
                rows = conn.execute('SELECT * FROM tg_users WHERE user_id=? LIMIT ?', (int(search), limit)).fetchall()
            else:
                # Literal search: escape LIKE wildcard characters in untrusted input.
                term = search.replace('!', '!!').replace('%', '!%').replace('_', '!_')
                pattern = '%'+term+'%'
                rows = conn.execute("""SELECT * FROM tg_users
                    WHERE username LIKE ? ESCAPE '!' COLLATE NOCASE
                       OR first_name LIKE ? ESCAPE '!' COLLATE NOCASE
                    ORDER BY last_seen DESC LIMIT ?""", (pattern, pattern, limit)).fetchall()
            return [dict(r) for r in rows]

    def detailed_admin_stats(self):
        with self.connection() as conn:
            u = conn.execute("""SELECT COUNT(*) users,
                COALESCE(SUM(last_seen >= datetime('now','-7 days')),0) active_7,
                COALESCE(SUM(last_seen >= datetime('now','-30 days')),0) active_30
                FROM tg_users""").fetchone()
            blocked = conn.execute('SELECT COUNT(*) FROM tg_admin_blocks').fetchone()[0]
            orders = self._order_admin_stats(conn)
            return {**dict(u), 'blocked': blocked, **orders}

    @staticmethod
    def _order_admin_stats(conn):
        row = conn.execute("""
            SELECT COUNT(*) orders,
                   COALESCE(SUM(status='under_review'),0) review,
                   COALESCE(SUM(status='approved'),0) approved,
                   COALESCE(SUM(CASE WHEN status='approved' THEN amount ELSE 0 END),0) revenue
            FROM tg_orders
        """).fetchone()
        return dict(row)
