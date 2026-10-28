"""Offline regression tests. No Telegram network calls; no real Bale database touched."""
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Stub config so tests don't load any real .env or secrets.
config = types.ModuleType('config')
config.TELEGRAM_BOT_TOKEN = '123456:FAKE_TOKEN_FOR_TESTS'
config.CARD_NUMBER = '0000000000000000'
config.CARD_HOLDER = 'Test Account'
config.PLANS = {
    'GOLD_25GB': {'size':25600, 'price':450000, 'type':'طلایی', 'users':'بدون محدودیت کاربر', 'display':'طلایی - 25 گیگابایت'},
    'ULTRA_10GB': {'size':10240, 'price':200000, 'type':'الترا', 'users':'بدون محدودیت کاربر', 'display':'الترا - 10 گیگابایت'}
}
sys.modules['config'] = config
from telegram_store import TelegramStore
from telegram_v2 import Storefront, Runner, escape, TelegramAPI, inline


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.photos = []
        self.edits = []
        self.answers = []
    def send(self, cid, text, markup=None):
        self.messages.append((cid, text, markup))
        return {'message_id': len(self.messages)}
    def photo(self, cid, file_id, caption=None, markup=None):
        self.photos.append((cid, file_id, caption, markup))
        return {'message_id': 100}
    def document(self, cid, file_id, caption=None, markup=None):
        self.photos.append((cid, file_id, caption, markup))
        return {'message_id': 101}
    def edit(self, cid, mid, text, markup):
        self.edits.append((cid, mid, text, markup))
        return {'message_id': mid}
    def answer(self, cid):
        self.answers.append(cid)
        return True


class TelegramV2Test(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / 'bot_data.db'
        conn = sqlite3.connect(self.path)
        conn.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT)')
        conn.execute("INSERT INTO orders VALUES(1, 222, 'pending')")
        conn.execute('CREATE TABLE users(user_id INTEGER PRIMARY KEY,first_name TEXT)')
        conn.execute("INSERT INTO users VALUES(222, 'BALE_USER')")
        conn.execute('CREATE TABLE announcements (id INTEGER PRIMARY KEY,message TEXT,is_active INTEGER)')
        conn.execute("INSERT INTO announcements VALUES(1, 'سلام',1)")
        conn.execute('CREATE TABLE plan_status(plan_id TEXT PRIMARY KEY,available INTEGER,out_of_stock_message TEXT)')
        conn.execute("INSERT INTO plan_status VALUES('GOLD_25GB',1,NULL)")
        conn.commit()
        conn.close()
        self.store = TelegramStore(self.path)
        self.store.initialize()
        self.api = FakeAPI()
        self.app = Storefront(self.store, self.api, admin_ids={999}, card_number=config.CARD_NUMBER,
                              card_holder=config.CARD_HOLDER, support_contact='@test_support')

    def msg(self, user, text=None, photo=None):
        obj = {'chat': {'id':user, 'type':'private'}, 'from':{'id':user,'first_name':'Test'}}
        if text is not None:
            obj['text'] = text
        if photo:
            obj['photo'] = [{'file_id':photo}]
        self.app.message(obj)

    def cb(self, user, data):
        self.app.callback({'id':'cbi', 'from':{'id':user, 'first_name':'Test'},
                           'message':{'chat':{'id':user,'type':'private'},'message_id':100},
                           'data':data})

    def test_shared_database_isolated_from_bale(self):
        self.msg(222, '/start')
        self.cb(222, 'buy:GOLD_25GB')
        self.assertEqual(self.store.user_stats(222)['total'], 1)
        c = sqlite3.connect(self.path)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT first_name FROM users WHERE user_id=222').fetchone()[0], 'BALE_USER')
        self.assertEqual(self.store.announcement(), 'سلام')
        c.close()

    def test_double_click_does_not_double_order(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        self.cb(111, 'buy:GOLD_25GB')
        self.assertEqual(self.store.user_stats(111)['total'], 1)

    def test_single_unlimited_requires_ack_and_is_not_bypassable(self):
        config.PLANS['UNLIMITED_SINGLE'] = {'type':'تک کاربره نامحدود','size':102400,
              'price':500000,'users':'تک کاربره','display':'نامحدود تک کاربره'}
        try:
            self.msg(111,'/start')
            self.cb(111,'ack:UNLIMITED_SINGLE')
            self.assertEqual(self.store.user_stats(111)['total'],0)
            self.cb(111,'buy:UNLIMITED_SINGLE')
            self.assertEqual(self.store.user_stats(111)['total'],0)
            self.assertIn('100 گیگابایت',self.api.edits[-1][2])
            self.cb(111,'ack:UNLIMITED_SINGLE')
            self.assertEqual(self.store.user_stats(111)['total'],1)
            self.assertIn('pro12',self.api.edits[-1][2])
        finally:
            del config.PLANS['UNLIMITED_SINGLE']

    def test_customer_support_and_jalali(self):
        self.msg(111,'/start')
        self.cb(111,'support')
        self.assertIn('@amiir_vip12', self.api.edits[-1][2])
        self.cb(111,'buy:GOLD_25GB')
        self.assertIn('10 دقیقه', self.api.edits[-1][2])
        oid=self.store.user_orders(111)[0]['id']
        self.cb(111,f'order:{oid}')
        self.assertIn('140',self.api.edits[-1][2])
        self.assertNotIn('طبق تعریف پلن',self.api.edits[-1][2])

    def test_user_cannot_cancel_others_order(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        self.msg(222, '/start')
        self.cb(222, f'cancel:{oid}')
        self.assertEqual(self.store.get_order(oid)['status'], 'awaiting_receipt')
        self.cb(111, f'cancel:{oid}')
        self.assertEqual(self.store.get_order(oid)['status'], 'cancelled')
        self.cb(111, f'cancel:{oid}')
        self.assertEqual(self.store.get_order(oid)['status'], 'cancelled')

    def test_receipt_approval_delivery_once(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        self.cb(111, f'receipt:{oid}')
        self.msg(111, photo='PHOTO_TELEGRAM_ONLY')
        self.assertEqual(self.store.get_order(oid)['status'], 'under_review')
        self.assertEqual(self.api.photos[-1][0], 999)
        self.cb(222, f'admin:approve:{oid}')
        self.assertEqual(self.store.get_order(oid)['status'], 'under_review')
        self.cb(999, f'admin:approve:{oid}')
        self.msg(999, 'vless://test_link')
        self.assertEqual(self.store.get_order(oid)['status'], 'approved')
        self.assertIsNotNone(self.store.get_order(oid)['delivered_at'])
        self.assertTrue(any(cid==111 and 'vless://' in txt for cid,txt,_ in self.api.messages))
        self.cb(999, f'admin:reject:{oid}')
        self.assertEqual(self.store.get_order(oid)['status'], 'approved')

    def test_image_document_receipt_supported(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        self.cb(111, f'receipt:{oid}')
        self.app.message({'chat':{'id':111,'type':'private'},'from':{'id':111,'first_name':'Test'},
                          'document': {'file_id': 'DOCUMENT_IMAGE', 'mime_type': 'image/png'}})
        self.assertEqual(self.api.photos[-1][1], 'DOCUMENT_IMAGE')

    def test_photo_without_order_does_not_mutate(self):
        self.msg(111, '/start')
        self.msg(111, photo='random_photo')
        self.assertEqual(self.store.user_stats(111)['total'], 0)

    def test_receipt_can_only_be_submitted_once(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        self.cb(111, f'receipt:{oid}')
        self.msg(111, photo='f1')
        self.msg(111, photo='f2')
        self.assertEqual(self.store.get_order(oid)['receipt_file_id'], 'f1')

    def test_new_welcome_only_vpn_no_legacy_announcement(self):
        self.msg(111, '/start')
        self.assertEqual(len(self.api.messages), 1)
        self.assertIn('اینترنت آزاد | پرو', self.api.messages[0][1])
        self.assertNotIn('📢 اطلاعیه:', self.api.messages[0][1])
        self.assertNotIn('طراحی وبسایت', self.api.messages[0][1])

    def test_payment_single_receipt_button_and_copyable_card(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        cid, mid, text, kb = self.api.edits[-1]
        self.assertEqual(cid, 111)
        self.assertIn('<code>6104338960830384</code>', text)
        self.assertIn('pro12', text)
        self.assertIn('10 دقیقه', text)
        self.assertEqual(kb, {'inline_keyboard': [[{'text':'📤 ارسال رسید', 'callback_data': f'receipt:{oid}'}]]})
        self.msg(111, photo='early_receipt')
        self.assertEqual(self.store.get_order(oid)['status'], 'awaiting_receipt')
        self.cb(111, f'receipt:{oid}')
        self.assertEqual(self.store.get_state(111)[0], 'waiting_receipt')
        self.msg(111, photo='valid_receipt')
        self.assertEqual(self.store.get_order(oid)['status'], 'under_review')

    def test_other_user_cannot_activate_receipt_submission(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        self.msg(222, '/start')
        self.cb(222, f'receipt:{oid}')
        self.assertNotEqual(self.store.get_state(222)[0], 'waiting_receipt')
        self.msg(222, photo='fake')
        self.assertIsNone(self.store.get_order(oid)['receipt_file_id'])

    def test_forged_callback_does_not_leak_order(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        self.msg(222, '/start')
        self.cb(222, f'order:{oid}')
        self.assertNotIn('💳', self.api.edits[-1][2])
        self.assertNotIn('vless://', self.api.edits[-1][2])

    def test_catalog_driven_by_existing_plans(self):
        self.msg(111, '/start')
        self.cb(111, 'catalog')
        rendered = str(self.api.edits[-1][3])
        self.assertIn('طلایی', rendered)
        self.assertIn('الترا', rendered)
        self.assertNotIn('نقره‌ای', rendered)

    def test_unavailable_plan_cannot_be_purchased(self):
        with self.store.connection() as conn:
            conn.execute("UPDATE plan_status SET available=0 WHERE plan_id='GOLD_25GB'")
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        self.assertEqual(self.store.user_stats(111)['total'], 0)

    def test_html_escape(self):
        self.assertEqual(escape('<b>&'), '&lt;b&gt;&amp;')


if __name__ == '__main__':
    unittest.main(verbosity=2)
