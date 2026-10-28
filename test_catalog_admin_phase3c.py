"""Offline tests: catalogue administration, persistence, audit and cross-platform visibility."""
import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from telegram_store import TelegramStore
from telegram_v2 import Storefront
from service_catalog import CatalogStore, CatalogError, PLANS, read_catalog

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.oldpath=os.environ.get('BOT_DB_PATH')
        self.path=Path(self.temp.name)/'bot_data.db'
        os.environ['BOT_DB_PATH']=str(self.path)
        self.addCleanup(self.restore)
        self.db=TelegramStore(self.path);self.db.initialize()
        self.admin=123456
        self.api=Mock()
        self.api.send.return_value={'message_id':1}
        self.api.edit.return_value={'message_id':1}
        self.api.answer.return_value=True
        self.app=Storefront(self.db,self.api,admin_ids={self.admin},card_number='',card_holder='',support_contact='')
        self.db.upsert_user(self.admin,'admin','ادمین')
        self.db.upsert_user(9988,'buyer','خریدار')
        self.cat=CatalogStore(self.path)
    def restore(self):
        if self.oldpath is None:os.environ.pop('BOT_DB_PATH',None)
        else:os.environ['BOT_DB_PATH']=self.oldpath
        self.temp.cleanup()
    def click(self,user,cb):
        self.app.callback({'from':{'id':user,'first_name':'Tester'},'message':{'chat':{'id':user,'type':'private'},'message_id':1},'data':cb,'id':'cb'})
    def msg(self,user,txt):
        self.app.message({'from':{'id':user,'first_name':'Tester'},'chat':{'id':user,'type':'private'},'text':txt})
    def new(self):
        p={'display':'جدید طلایی 25 گیگ','type':'طلایی','size':25600,'price':335000,'users':'بدون محدودیت کاربر','duration':'30 روز'}
        return self.cat.create(self.admin,p)
    def test_defaults_seed_once_preserves_changes_after_restart(self):
        ids=self.cat.all()
        self.assertTrue(ids)
        original=ids[0]
        changed=self.cat.change(self.admin,original['plan_id'],'price','123000',original['revision'])
        self.assertEqual(PLANS[original['plan_id']]['price'],123000)
        self.db.initialize()
        self.assertEqual(CatalogStore(self.path).get(original['plan_id'])['price'],123000)
        self.assertEqual(changed['price'],123000)
    def test_new_plan_in_customer_catalog_and_purchase(self):
        pid=self.new()
        self.assertTrue(pid.startswith('CUSTOM_'))
        self.assertIn(pid,PLANS)
        self.click(9988,'plan:'+pid)
        self.assertIn('335,000',str(self.api.edit.call_args))
        self.click(9988,'buy:'+pid)
        o=self.db.user_orders(9988)[0]
        self.assertEqual(o['amount'],335000)
        self.assertEqual(self.db.order_title(o['id'],pid),'جدید طلایی 25 گیگ')
    def test_price_change_does_not_reprice_old_order_or_title(self):
        pid=self.new()
        self.db.open_order(9988,pid,335000)
        old=self.db.user_orders(9988)[0]
        row=self.cat.get(pid)
        self.cat.change(self.admin,pid,'price','445000',row['revision'])
        row=self.cat.get(pid)
        self.cat.change(self.admin,pid,'display','نام جدید سرویس',row['revision'])
        self.assertEqual(self.db.user_orders(9988)[0]['amount'],335000)
        self.assertEqual(self.db.order_title(old['id'],pid),'جدید طلایی 25 گیگ')
        self.assertEqual(PLANS[pid]['display'],'نام جدید سرویس')
    def test_out_of_stock_and_restore_shared_status(self):
        pid=self.new()
        row=self.cat.get(pid)
        self.cat.change(self.admin,pid,'available',False,row['revision'])
        self.assertEqual(self.db.plan_status(pid)[0],False)
        self.click(9988,'buy:'+pid)
        self.assertEqual(self.db.user_orders(9988),[])
        row=self.cat.get(pid)
        self.cat.change(self.admin,pid,'available',True,row['revision'])
        self.assertTrue(self.db.plan_status(pid)[0])
    def test_soft_archive_preserves_old_order_and_hides_catalog(self):
        pid=self.new()
        self.db.open_order(9988,pid,335000)
        row=self.cat.get(pid)
        self.cat.change(self.admin,pid,'archived',True,row['revision'])
        self.assertNotIn(pid,PLANS)
        self.assertEqual(self.db.user_orders(9988)[0]['plan_id'],pid)
        self.assertEqual(self.db.order_title(1,pid),'جدید طلایی 25 گیگ')
        self.assertFalse(self.db.plan_status(pid)[0])
    def test_stale_revision_rejected_and_audit_logged(self):
        pid=self.new()
        row=self.cat.get(pid)
        self.cat.change(self.admin,pid,'price','444000',row['revision'])
        with self.assertRaises(CatalogError):
            self.cat.change(self.admin,pid,'price','555000',row['revision'])
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM service_catalog_audit WHERE plan_id=?',(pid,)).fetchone()[0],2)
    def test_plain_user_forged_callbacks_and_state_denied(self):
        self.click(9988,'adm:catalog')
        self.click(9988,'adm:catnew')
        self.click(9988,'adm:catcreate')
        self.assertNotEqual(self.db.get_state(9988)[0],'catalog_new')
        self.assertEqual(len(self.cat.all()),len(read_catalog(self.path)))
    def test_create_wizard_ui_and_only_one_confirmation(self):
        before=len(self.cat.all())
        self.click(self.admin,'adm:catnew')
        self.msg(self.admin,'طلایی آلترا')
        self.click(self.admin,'adm:catkind:0')
        self.msg(self.admin,'20')
        self.msg(self.admin,'349000')
        self.msg(self.admin,'تا 3 کاربر')
        self.msg(self.admin,'30 روز')
        self.assertEqual(self.db.get_state(self.admin)[0],'catalog_new_confirm')
        self.click(self.admin,'adm:catcreate')
        self.click(self.admin,'adm:catcreate')
        self.assertEqual(len(self.cat.all()),before+1)
    def test_edit_confirmation_guards_forged_and_stale_callback(self):
        pid=self.new()
        self.click(self.admin,f'adm:catedit:{pid}:price')
        self.msg(self.admin,'449000')
        self.click(self.admin,'adm:catsave:UNKNOWN')
        self.assertEqual(self.cat.get(pid)['price'],335000)
        self.click(self.admin,f'adm:catsave:{pid}')
        self.assertEqual(self.cat.get(pid)['price'],449000)
        self.click(self.admin,f'adm:catsave:{pid}')
        self.assertEqual(self.cat.get(pid)['price'],449000)
    def test_invalid_data_rejected(self):
        with self.assertRaises(CatalogError):self.cat.create(self.admin,{'display':'bad','type':'طلایی','size':-1,'price':250000,'users':'test','duration':''})
        pid=self.new()
        r=self.cat.get(pid)
        with self.assertRaises(CatalogError):self.cat.change(self.admin,pid,'price','-1',r['revision'])
        with self.assertRaises(CatalogError):self.cat.change(self.admin,pid,'type','گمنام',r['revision'])
        with self.assertRaises(CatalogError):self.cat.change(self.admin,pid,'display','abc\ndef',r['revision'])

if __name__=='__main__':unittest.main()
