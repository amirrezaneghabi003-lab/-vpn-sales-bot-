"""Offline Phase 1 tests: no network, no customer database, no real credentials."""
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parent

class SecurityAndAppearanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        # A brand-new isolated catalogue: tests must NEVER read the live BOT_DB_PATH.
        cls.old_bot_db_path = os.environ.get('BOT_DB_PATH')
        os.environ['BOT_DB_PATH'] = str(cls.root / 'isolated_catalog.db')
        shutil.copy2(ROOT / 'database.py', cls.root / 'database.py')
        shutil.copy2(ROOT / 'bot.py', cls.root / 'bot.py')
        cfg = types.ModuleType('config')
        cfg.BOT_TOKEN = 'offline-test-token'
        cfg.CARD_NUMBER = 'TEST_ONLY'
        cfg.CARD_HOLDER = 'TEST_ONLY'
        cfg.MASTER_ADMIN_ID = 999
        cfg.PLANS = {
            'GOLD': {'size':10240,'price':1000,'type':'طلایی','users':'1','display':'طلایی تستی'},
            'ULTRA': {'size':5120,'price':2000,'type':'الترا','users':'1','display':'الترا تستی'},
            'UNLIMITED_SINGLE': {'size':102400,'price':500000,'type':'تک کاربره نامحدود','users':'تک کاربره','display':'نامحدود تک کاربره'},
        }
        # unittest discover loads other test modules first. Their cached 'bot' can
        # point to a temp DB that has already been deleted. Import truly isolated
        # fresh copies for this class and restore previous module objects later.
        cls.module_names = ('config', 'database', 'bot', 'service_catalog',
                            'bale_catalog_admin', 'bale_admin', 'telegram_catalog_admin')
        cls.old_modules = {key: sys.modules.get(key) for key in cls.module_names}
        for key in cls.module_names:
            sys.modules.pop(key, None)
        sys.modules['config'] = cfg
        sys.path.insert(0, str(cls.root))
        import database
        import bot
        cls.dbmod = database
        cls.bot = bot
        cls.msgs = []
        bot.send_message = lambda uid, txt, reply_markup=None: cls.msgs.append((uid, txt)) or {'ok': True}
        bot.session.post = lambda *args, **kwargs: None

    @classmethod
    def tearDownClass(cls):
        for k,v in cls.old_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        sys.path.remove(str(cls.root))
        if cls.old_bot_db_path is None:
            os.environ.pop('BOT_DB_PATH', None)
        else:
            os.environ['BOT_DB_PATH'] = cls.old_bot_db_path
        cls.tmp.cleanup()

    def setUp(self):
        self.msgs.clear()

    def test_dynamic_categories_and_compact_keyboard(self):
        buttons = [button for row in self.bot.panel_type_menu()['inline_keyboard'] for button in row]
        callbacks = [b['callback_data'] for b in buttons]
        self.assertEqual(callbacks, ['panel_gold', 'panel_ultra', 'panel_unlimited_single', 'back_main'])
        main = self.bot.main_menu()['keyboard']
        self.assertEqual(main[0][0]['text'], '🛍 خرید سرویس')

    def test_only_vpn_menu_and_stale_buttons(self):
        menu=str(self.bot.main_menu())
        for old in ('طراحی وبسایت','شماره مجازی','دریافت نمایندگی','ساخت ربات'):
            self.assertNotIn(old, menu)
        self.bot.handle_message({'chat': {'id':234,'first_name':'Test'}, 'text':'🌐 سفارش طراحی وبسایت'})
        self.assertIn('فقط سرویس VPN', self.msgs[-1][1])

    def test_single_plan_needs_notice_ack(self):
        # A forged callback must not issue the card or create an order.
        self.bot.handle_callback_query({'message':{'chat':{'id':321}},'id':'cb', 'data':'unlimited_single_buy'})
        self.assertEqual(self.bot.db.get_user_orders(321), [])
        self.bot.handle_callback_query({'message':{'chat':{'id':321}},'id':'cb', 'data':'panel_unlimited_single'})
        self.assertIn('100 گیگابایت', self.msgs[-1][1])
        self.bot.handle_callback_query({'message':{'chat':{'id':321}},'id':'cb', 'data':'unlimited_single_buy'})
        orders=self.bot.db.get_user_orders(321)
        self.assertEqual(len(orders),1)
        self.assertIn('pro12', self.msgs[-1][1])

    def test_payment_screen_only_receipt_button_bale(self):
        from unittest.mock import patch
        sent = []
        def capture(uid, text, reply_markup=None):
            sent.append((uid, text, reply_markup))
            return {'ok':True}
        with patch.object(self.bot, 'send_message', side_effect=capture):
            self.bot.handle_callback_query({'message': {'chat': {'id': 890}}, 'id':'x', 'data':'plan_GOLD'})
        self.assertTrue(sent)
        _, text, markup = sent[-1]
        self.assertIn('اینترنت', self.bot.welcome_text())
        self.assertIn('pro12', text)
        self.assertIn('10 دقیقه', text)
        self.assertEqual(len(markup['inline_keyboard']), 1)
        self.assertEqual(markup['inline_keyboard'][0][0]['text'], '📤 ارسال رسید')
        self.assertTrue(markup['inline_keyboard'][0][0]['callback_data'].startswith('receipt_upload_'))

    def test_receipt_activation_requires_owner_and_button(self):
        self.bot.handle_callback_query({'message': {'chat': {'id': 456}}, 'id':'x', 'data':'plan_GOLD'})
        orders = self.bot.db.get_user_orders(456)
        self.assertTrue(orders)
        oid = orders[0][0]
        self.assertEqual(self.bot.get_user_state(456)[0], 'receipt_pending')
        self.bot.handle_callback_query({'message': {'chat': {'id': 9991}}, 'id':'x', 'data':f'receipt_upload_{oid}'})
        self.assertNotEqual(self.bot.get_user_state(9991)[0], 'waiting_receipt')
        self.bot.handle_callback_query({'message': {'chat': {'id': 456}}, 'id':'x', 'data':f'receipt_upload_{oid}'})
        self.assertEqual(self.bot.get_user_state(456)[0], 'waiting_receipt')

    def test_shared_payment_rotation_between_platforms(self):
        from telegram_store import TelegramStore
        store=TelegramStore(self.bot.db.db_name)
        store.initialize()
        bale_order=self.bot.db.create_order(567,'GOLD',100)
        bale_card=self.bot.db.get_order_by_id(bale_order)[8]
        self.assertTrue(self.bot.db.save_receipt(bale_order, 'receipt',567))
        self.assertTrue(self.bot.db.approve_order(bale_order,'vless://link'))
        self.assertFalse(self.bot.db.approve_order(bale_order,'vless://link-again'))
        store.upsert_user(678,'tester','Tester')
        tg_order,_=store.open_order(678,'GOLD',100)
        self.assertNotEqual(tg_order['payment_card_index'],bale_card)
        self.assertTrue(store.submit_receipt(tg_order['id'],678,'file'))
        self.assertTrue(store.approve_order(tg_order['id'],'vless://link'))
        self.assertFalse(store.approve_order(tg_order['id'],'vless://link-again'))
        new_bale_order=self.bot.db.create_order(789,'GOLD',100)
        self.assertEqual(self.bot.db.get_order_by_id(new_bale_order)[8],bale_card)
        # Prior orders preserve their originally assigned payment cards.
        self.assertEqual(store.get_order(tg_order['id'])['payment_card_index'],tg_order['payment_card_index'])

    def test_owner_cannot_cancel_other_users_order(self):
        oid=self.bot.db.create_order(111,'GOLD',100)
        self.bot.handle_callback_query({'message':{'chat':{'id':222}}, 'id':'fake','data':f'cancel_yes_{oid}'})
        self.assertEqual(self.bot.db.get_order_by_id(oid)[4], 'pending')
        self.bot.handle_callback_query({'message':{'chat':{'id':111}}, 'id':'fake','data':f'cancel_yes_{oid}'})
        self.assertEqual(self.bot.db.get_order_by_id(oid)[4], 'cancelled')
        self.assertFalse(self.bot.db.cancel_order(oid,111))

    def test_receipt_owner_and_forged_callback(self):
        oid = self.bot.db.create_order(333,'GOLD',100)
        self.assertFalse(self.bot.db.save_receipt(oid,'file-1',222))
        self.assertTrue(self.bot.db.save_receipt(oid,'file-1',333))
        self.assertFalse(self.bot.db.save_receipt(oid,'file-2',333))
        self.bot.handle_callback_query({'message':{'chat':{'id':222}}, 'id':'fake','data':f'send_receipt_no_desc_{oid}'})
        self.assertEqual(self.bot.db.get_order_by_id(oid)[4], 'pending')

    def test_atomic_approval_and_rejection(self):
        oid=self.bot.db.create_order(444,'ULTRA',50)
        self.assertFalse(self.bot.db.approve_order(oid,'link'))
        self.assertTrue(self.bot.db.save_receipt(oid,'receipt',444))
        self.assertTrue(self.bot.db.approve_order(oid,'link'))
        self.assertFalse(self.bot.db.approve_order(oid,'link2'))
        self.assertFalse(self.bot.db.reject_order(oid))
        self.assertFalse(self.bot.db.cancel_order(oid,444))
        self.assertEqual(self.bot.db.get_order_by_id(oid)[4],'approved')
        oid2=self.bot.db.create_order(555,'ULTRA',50)
        self.assertTrue(self.bot.db.save_receipt(oid2,'receipt',555))
        self.assertTrue(self.bot.db.reject_order(oid2))
        self.assertFalse(self.bot.db.approve_order(oid2,'link'))

if __name__ == '__main__':
    unittest.main(verbosity=2)
