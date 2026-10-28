"""Bale admin tests: isolated temporary database, zero network calls."""
import sqlite3
import os
from unittest.mock import patch
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from bale_admin import BaleAdmin
from database import Database


class BaleAdminPhase4ATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        local_db = str(Path(self.tmp.name) / 'bale_test.db')
        with patch.dict(os.environ, {'BOT_DB_PATH': local_db}):
            self.db = Database(local_db)
        self.sent = []
        self.photos = []
        def send(uid, text, reply_markup=None):
            self.sent.append((uid, text, reply_markup))
            return {'ok': True}
        def send_photo(uid, file_id, caption=None):
            self.photos.append((uid, file_id, caption))
            return {'ok': True}
        self.admin = BaleAdmin(self.db, send, send_photo)
        self.db.save_user(11, 'buyer_11', 'مشتری', 'تست')
        self.db.save_user(12, 'buyer_12', 'مشتری', 'دوم')
        self.db.save_user(900, 'admin900', 'مدیر', 'تست')
        self.db.save_user(901, 'admin901', 'مدیر', 'دوم')
        self.db.add_admin(900,900)
        self.db.add_admin(901,900)

    def test_admin_only_entry_and_callbacks(self):
        self.assertFalse(self.admin.home(11))
        self.assertTrue(self.admin.home(900))
        self.assertIn('مرکز مدیریت', self.sent[-1][1])
        self.sent.clear()
        self.assertTrue(self.admin.callback(11,'badmin:users'))
        self.assertFalse(self.sent)

    def test_search_by_id_name_username_and_escape(self):
        self.assertEqual(self.admin.lookup('11')[0][0], 11)
        self.assertTrue(any(r[0]==11 for r in self.admin.lookup('@buyer_11')))
        self.assertTrue(any(r[0]==11 for r in self.admin.lookup('مشتری')))
        self.assertEqual(self.admin.lookup('%'), [])

    def test_admin_search_wizard(self):
        self.admin.callback(900,'badmin:users')
        self.assertEqual(self.db.get_user_state(900)[0],'badmin_search_user')
        self.admin.text(900,'11')
        self.assertIn('پرونده کاربر',self.sent[-1][1])
        self.assertIsNone(self.db.get_user_state(900)[0])

    def test_private_message_requires_confirmation(self):
        self.admin.callback(900,'badmin:compose:11')
        self.admin.text(900,'سلام مشتری')
        self.assertFalse(any(uid==11 for uid,*_ in self.sent))
        self.admin.callback(900,'badmin:dmconfirm:11')
        self.assertEqual([uid for uid,*_ in self.sent].count(11),1)
        self.assertIn('سلام مشتری',[s for u,s,_ in self.sent if u==11][0])
        self.admin.callback(900,'badmin:dmconfirm:11')
        self.assertEqual([uid for uid,*_ in self.sent].count(11),1)

    def test_forged_dm_confirmation_cannot_switch_recipient(self):
        self.admin.callback(900,'badmin:compose:11')
        self.admin.text(900,'سلام')
        self.admin.callback(900,'badmin:dmconfirm:12')
        self.assertFalse(any(uid==12 for uid,*_ in self.sent))

    def test_admin_block_unblock_with_confirmation(self):
        self.admin.callback(900,'badmin:blockask:11')
        self.assertFalse(self.db.is_user_blocked_by_admin(11))
        self.admin.callback(900,'badmin:blockyes:11')
        self.assertTrue(self.db.is_user_blocked_by_admin(11))
        self.admin.callback(900,'badmin:blockyes:11')
        self.assertTrue(self.db.is_user_blocked_by_admin(11))
        self.admin.callback(900,'badmin:blockask:11')
        self.admin.callback(900,'badmin:blockyes:11')
        self.assertFalse(self.db.is_user_blocked_by_admin(11))

    def test_admin_cannot_block_other_admin(self):
        self.admin.callback(900,'badmin:blockask:901')
        self.admin.callback(900,'badmin:blockyes:901')
        self.assertFalse(self.db.is_user_blocked_by_admin(901))

    def test_blocked_user_not_sent_direct_message(self):
        self.admin.callback(900,'badmin:compose:11')
        self.admin.text(900,'سلام')
        self.db.add_admin_block(11)
        self.admin.callback(900,'badmin:dmconfirm:11')
        self.assertFalse(any(uid==11 for uid,*_ in self.sent))

    def test_order_lookup_and_receipt_preview(self):
        oid=self.db.create_order(11,'GOLD_25GB',450000)
        self.db.save_receipt(oid,'photo-test',11)
        self.admin.callback(900,'badmin:orders')
        self.admin.text(900,f'pro12{oid}')
        self.assertIn('pro12',self.sent[-1][1])
        self.assertEqual(self.photos[-1][1],'photo-test')

    def test_reject_order_confirmation_and_idempotence(self):
        oid=self.db.create_order(11,'GOLD_25GB',450000)
        self.db.save_receipt(oid,'photo-test',11)
        self.admin.callback(900,f'badmin:rejectask:{oid}')
        self.assertEqual(self.db.get_order_by_id(oid)[4],'pending')
        self.admin.callback(900,f'badmin:rejectyes:{oid}')
        self.assertEqual(self.db.get_order_by_id(oid)[4],'rejected')
        self.admin.callback(900,f'badmin:rejectyes:{oid}')
        self.assertEqual(self.db.get_order_by_id(oid)[4],'rejected')
        with closing(sqlite3.connect(self.db.db_name)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM bale_admin_audit WHERE action='reject'").fetchone()[0],1)

    def test_stats_activity_and_tehran_display(self):
        self.admin.touch(11)
        self.admin.callback(900,'badmin:stats')
        self.assertIn('فعال 7 روز: 1',self.sent[-1][1])
        self.admin.user(900,11)
        self.assertIn('(تهران)',self.sent[-1][1])

    def test_bad_callback_ids_never_execute(self):
        for raw in ('x','99999999999999999999999999999999','-1'):
            self.admin.callback(900,f'badmin:blockyes:{raw}')
        self.assertFalse(self.db.is_user_blocked_by_admin(11))

    def test_exit_and_cancel_wizard(self):
        self.admin.callback(900,'badmin:dm')
        self.admin.text(900,'/cancel')
        self.assertIsNone(self.db.get_user_state(900)[0])

    def test_source_integration_exists(self):
        source=(Path(__file__).parent/'bot.py').read_text(encoding='utf-8')
        self.assertIn("if data.startswith(('badmin:', 'bbr:')):",source)
        self.assertIn("actor != chat_id or not db.is_admin(actor)",source)
        self.assertIn('bale_admin.text(chat_id, text_input)',source)


if __name__=='__main__':unittest.main()
