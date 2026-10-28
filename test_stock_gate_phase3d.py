"""Regression tests for sold-out listing and race-safe receipt upload. Offline."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest import mock

from test_telegram_v2 import config  # offline-only stub; never reads real tokens
config.MASTER_ADMIN_ID = getattr(config,'MASTER_ADMIN_ID',999)
config.BOT_TOKEN = getattr(config,'BOT_TOKEN','OFFLINE_ONLY')
from telegram_store import TelegramStore
from telegram_v2 import Storefront
from service_catalog import CatalogStore
from database import Database


class SoldOutFlows(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.path = Path(self.t.name) / 'case.db'
        old = os.environ.get('BOT_DB_PATH')
        os.environ['BOT_DB_PATH'] = str(self.path)
        self.addCleanup(lambda: os.environ.pop('BOT_DB_PATH',None) if old is None
                        else os.environ.__setitem__('BOT_DB_PATH',old))
        self.tg = TelegramStore(self.path)
        self.tg.initialize()
        self.bale = Database(str(self.path))
        self.catalog = CatalogStore(self.path)
        self.pid = self.catalog.create(900, dict(display='آزمایشی 30 گیگ',type='طلایی',
                size=30720,price=275000,users='دو کاربر',duration='30 روز'))
        self.tg.upsert_user(42,'tester','Tester')
        self.api = Mock()
        self.api.send.return_value = {'message_id':1}
        self.api.edit.return_value = {'message_id':1}
        self.api.answer.return_value = True
        self.app=Storefront(self.tg,self.api,admin_ids={900},card_number='',card_holder='',support_contact='')
    def disable(self):
        plan=self.catalog.get(self.pid)
        self.catalog.change(900,self.pid,'available',False,plan['revision'])
    def enable(self):
        plan=self.catalog.get(self.pid)
        self.catalog.change(900,self.pid,'available',True,plan['revision'])

    def test_telegram_soldout_stays_noop_button(self):
        self.disable()
        self.app.category(42,self.app.category_index('طلایی'),{'chat':{'id':42},'message_id':14})
        args=self.api.edit.call_args.args
        self.assertNotIn('آزمایشی 30 گیگ — ناموجود',args[2])
        self.assertIn('ناموجود',str(args[3]))
        self.assertIn(f'soldout:{self.pid}',str(args[3]))
        self.assertNotIn(f'plan:{self.pid}',str(args[3]))
        self.enable()
        self.app.category(42,self.app.category_index('طلایی'),{'chat':{'id':42},'message_id':14})
        self.assertIn(f'plan:{self.pid}',str(self.api.edit.call_args.args[3]))

    def test_telegram_disabled_at_receipt_button(self):
        order,_=self.tg.open_order(42,self.pid,275000)
        self.disable()
        self.app.receipt_prompt(42,order['id'])
        self.assertIn('ناموجود',self.api.send.call_args.args[1])
        self.assertNotEqual(self.tg.get_state(42)[0],'waiting_receipt')
        self.assertFalse(self.tg.submit_receipt(order['id'],42,'file-1'))
        self.assertEqual(self.tg.get_order(order['id'])['status'],'awaiting_receipt')

    def test_telegram_disable_during_image_upload(self):
        order,_=self.tg.open_order(42,self.pid,275000)
        self.app.receipt_prompt(42,order['id'])
        self.assertEqual(self.tg.get_state(42)[0],'waiting_receipt')
        self.disable()
        self.app.on_photo(42,'file-sent-after-disable')
        self.assertIn('ناموجود',self.api.send.call_args.args[1])
        self.assertIsNone(self.tg.get_order(order['id'])['receipt_file_id'])
        self.assertNotEqual(self.tg.get_state(42)[0],'waiting_receipt')

    def test_telegram_when_restored_receipt_accepted_only_once(self):
        order,_=self.tg.open_order(42,self.pid,275000)
        self.disable();self.enable()
        self.assertTrue(self.tg.submit_receipt(order['id'],42,'file-first'))
        self.assertFalse(self.tg.submit_receipt(order['id'],42,'file-second'))
        self.assertEqual(self.tg.get_order(order['id'])['receipt_file_id'],'file-first')

    def test_bale_gate_at_recording_receipt(self):
        oid=self.bale.create_order(42,self.pid,275000)
        self.assertFalse(self.bale.receipt_service_available(oid,43))
        self.assertFalse(self.bale.save_receipt(oid,'forged',43))
        self.disable()
        self.assertFalse(self.bale.receipt_service_available(oid,42))
        self.assertFalse(self.bale.save_receipt(oid,'photo',42))
        self.assertIsNone(self.bale.get_order_by_id(oid)[6])
        self.enable()
        self.assertTrue(self.bale.save_receipt(oid,'photo',42))
        self.assertFalse(self.bale.save_receipt(oid,'again',42))

    def test_bale_disabled_product_list_has_no_callback(self):
        self.disable()
        # Import Bale adapter only after routing it to the disposable DB.
        import bot
        with mock.patch.object(bot,'db',self.bale):
            keyboard=bot.plans_menu('gold')
            description=bot.plans_description('gold','سرویس‌های طلایی')
        self.assertNotIn('ناموجود',description)
        self.assertIn('ناموجود',str(keyboard))
        self.assertIn('آزمایشی 30 گیگ',str(keyboard))
        self.assertNotIn(f'plan_{self.pid}',str(keyboard))
        self.enable()
        with mock.patch.object(bot,'db',self.bale):
            keyboard=bot.plans_menu('gold')
        self.assertIn(f'plan_{self.pid}',str(keyboard))

    def test_archived_is_not_eligible_for_receipt(self):
        oid=self.bale.create_order(42,self.pid,275000)
        order,_=self.tg.open_order(42,self.pid,275000)
        p=self.catalog.get(self.pid)
        self.catalog.change(900,self.pid,'archived',True,p['revision'])
        self.assertFalse(self.bale.save_receipt(oid,'photo',42))
        self.assertFalse(self.tg.submit_receipt(order['id'],42,'photo'))

if __name__=='__main__':unittest.main()
