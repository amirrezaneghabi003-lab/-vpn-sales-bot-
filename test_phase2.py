"""Payment rotation regression, independent of network and production credentials."""
import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from customer_experience import (
    ensure_payment_schema, current_card_index, record_successful_approval,
    order_ref, jalali_datetime, money, card_for_order, plan_volume,
)


class FormatTests(unittest.TestCase):
    def test_id_is_display_only(self):
        self.assertEqual(order_ref(1),'pro121')
        self.assertEqual(order_ref(12),'pro1212')

    def test_jalali_utc_to_tehran(self):
        self.assertEqual(jalali_datetime('2026-10-14 07:30:00'),'1405/07/22 | 11:00 (تهران)')
        self.assertEqual(jalali_datetime('2025-03-20 00:00:00'),'1403/12/30 | 03:30 (تهران)')

    def test_user_plan_volume(self):
        self.assertEqual(plan_volume({'display':'بین‌الملل - 25 گیگابایت','size':25600}), '25 گیگابایت')
        self.assertIn('100 گیگابایت', plan_volume({'display':'نامحدود','size':102400}))

    def test_cards_exact_and_static(self):
        self.assertEqual(card_for_order(1)[0],os.getenv('PAYMENT_CARD_1','6104338960830384'))
        self.assertEqual(card_for_order(2)[0],os.getenv('PAYMENT_CARD_2','6219861473813961'))
        self.assertNotEqual(card_for_order(1)[0],card_for_order(2)[0])


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.db=Path(self.t.name)/'shop.db'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT)')
            conn.execute('CREATE TABLE tg_orders(id INTEGER PRIMARY KEY, status TEXT)')
            ensure_payment_schema(conn)

    def test_approvals_only_rotate_not_creation_or_rejection(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(current_card_index(conn),1)
            conn.execute('INSERT INTO orders(id,status,payment_card_index) VALUES(1,?,?)',('pending',1))
            self.assertEqual(current_card_index(conn),1)
            conn.execute("UPDATE orders SET status='rejected' WHERE id=1")
            self.assertEqual(current_card_index(conn),1)
            # Rejections and cancellation must never move the cursor.
            conn.execute("INSERT INTO tg_orders(id,status,payment_card_index) VALUES(2,'under_review',1)")
            n=conn.execute("UPDATE tg_orders SET status='approved' WHERE id=2 AND status='under_review'").rowcount
            if n:
                record_successful_approval(conn)
            self.assertEqual(current_card_index(conn),2)
            # Repeat approval does nothing.
            n=conn.execute("UPDATE tg_orders SET status='approved' WHERE id=2 AND status='under_review'").rowcount
            if n:
                record_successful_approval(conn)
            self.assertEqual(current_card_index(conn),2)
            # Persisted card stays at card 1, regardless of new active card.
            self.assertEqual(conn.execute('SELECT payment_card_index FROM tg_orders WHERE id=2').fetchone()[0],1)
            n=conn.execute("INSERT INTO orders(id,status,payment_card_index) VALUES(3,'approved',2)").rowcount
            record_successful_approval(conn)
            self.assertEqual(current_card_index(conn),1)

    def test_idempotent_migration(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            ensure_payment_schema(conn)
            ensure_payment_schema(conn)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM payment_rotation').fetchone()[0],1)
            self.assertIn('payment_card_index',[r[1] for r in conn.execute('PRAGMA table_info(orders)')])
            self.assertIn('payment_card_index',[r[1] for r in conn.execute('PRAGMA table_info(tg_orders)')])
