"""Phase 3E: database-backed dynamic categories, no-op sold-out buttons and safe deletion."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import closing
from service_catalog import CatalogStore,CatalogError,PLANS,stock_available
from telegram_store import TelegramStore
from telegram_v2 import Storefront
from bale_catalog_admin import BaleCatalogAdmin
from test_bale_catalog_phase3c import FakeBaleDB


class DynamicCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'shop.db'
        previous=os.environ.get('BOT_DB_PATH')
        os.environ['BOT_DB_PATH']=str(self.path)
        self.addCleanup(lambda: os.environ.pop('BOT_DB_PATH',None) if previous is None else os.environ.__setitem__('BOT_DB_PATH',previous))
        self.store=TelegramStore(self.path)
        self.store.initialize()
        self.cat=CatalogStore(self.path)
        self.api=Mock()
        self.api.answer.return_value=True
        self.api.send.return_value={'message_id':1}
        self.api.edit.return_value={'message_id':1}
        self.app=Storefront(self.store,self.api,admin_ids={900},card_number='',card_holder='',support_contact='')
        self.store.upsert_user(900,'admin','manager')
        self.store.upsert_user(200,'buyer','buyer')
        self.baledb=FakeBaleDB(self.path)
        self.sent=[]
        self.balemgr=BaleCatalogAdmin(self.baledb,lambda uid,text,reply_markup=None:self.sent.append((uid,text,reply_markup)))

    def new_plan(self,category='پنل نقره‌ای'):
        return self.cat.create(900,dict(display='نقره‌ای 35 گیگ',type=category,size=35*1024,
                                        price=290000,users='2 نفر',duration='30 روز'))

    def click(self,who,action):
        self.app.callback({'from':{'id':who,'first_name':'test'},'message':{'chat':{'id':who,'type':'private'},'message_id':1},'data':action,'id':'callback-test'})

    def msg(self,who,content):
        self.app.message({'from':{'id':who,'first_name':'test'},'chat':{'id':who,'type':'private'},'text':content})

    def test_category_creation_and_plan_visible_in_both_bots(self):
        cid=self.cat.create_category(900,'پنل نقره‌ای')
        pid=self.new_plan()
        self.assertIn(pid,PLANS)
        self.assertIn('پنل نقره‌ای',[c['name'] for c in self.cat.categories()])
        self.app.catalog(200)
        self.assertIn(f'categoryid:{cid}',str(self.api.send.call_args))
        self.click(200,f'categoryid:{cid}')
        self.assertIn(f'plan:{pid}',str(self.api.edit.call_args))
        import bot
        with patch.object(bot,'db',self._bale_db()),patch.object(bot,'PLANS',PLANS):
            self.assertIn(f'panel_group:{cid}',str(bot.panel_type_menu()))
            self.assertIn(f'plan_{pid}',str(bot.plans_menu('custom:پنل نقره‌ای')))

    def _bale_db(self):
        from database import Database
        return Database(str(self.path))

    def test_soldout_button_replaces_price_without_order(self):
        cid=self.cat.create_category(900,'پنل نقره‌ای')
        pid=self.new_plan()
        r=self.cat.get(pid)
        self.cat.change(900,pid,'available',False,r['revision'])
        self.app.category_by_id(200,cid)
        buttons=str(self.api.send.call_args)
        self.assertIn(f'soldout:{pid}',buttons)
        self.assertIn('ناموجود',buttons)
        self.assertNotIn('290,000',buttons)
        self.click(200,f'soldout:{pid}')
        self.assertEqual(self.store.user_orders(200),[])
        import bot
        with patch.object(bot,'db',self._bale_db()):
            keyboard=str(bot.plans_menu('custom:پنل نقره‌ای'))
            self.assertIn('ناموجود',keyboard)
            self.assertIn(f'unavailable_{pid}',keyboard)
            self.assertNotIn('290,000',keyboard)

    def test_service_delete_is_not_unavailable_and_not_resurrected(self):
        self.cat.create_category(900,'پنل نقره‌ای')
        pid=self.new_plan()
        order,_=self.store.open_order(200,pid,290000)
        self.cat.delete_service(900,pid,self.cat.get(pid)['revision'])
        self.assertNotIn(pid,PLANS)
        self.assertNotIn(pid,[r['plan_id'] for r in self.cat.all(include_archived=True)])
        self.assertEqual(self.cat.get(pid)['deleted'],1)
        self.assertEqual(self.store.order_title(order['id'],pid),'نقره‌ای 35 گیگ')
        self.assertFalse(self.store.submit_receipt(order['id'],200,'fake'))
        self.store.initialize()
        self.assertNotIn(pid,PLANS)
        with self.assertRaises(CatalogError):self.cat.delete_service(900,pid,self.cat.get(pid)['revision'])

    def test_built_in_delete_is_not_reseeded(self):
        original=self.cat.all()[0]
        pid=original['plan_id']
        self.cat.delete_service(900,pid,original['revision'])
        self.store.initialize()
        self.assertNotIn(pid,PLANS)
        self.assertEqual(self.cat.get(pid)['deleted'],1)

    def test_delete_category_with_all_services_preserves_orders(self):
        cid=self.cat.create_category(900,'پنل نقره‌ای')
        pid=self.new_plan()
        old,_=self.store.open_order(200,pid,290000)
        revision=self.cat.category(cid)['revision']
        self.assertEqual(self.cat.delete_category(900,cid,revision),1)
        self.assertNotIn('پنل نقره‌ای',[r['name'] for r in self.cat.categories()])
        self.assertNotIn(pid,PLANS)
        self.assertFalse(self.store.submit_receipt(old['id'],200,'fake'))
        self.assertEqual(self.store.order_title(old['id'],pid),'نقره‌ای 35 گیگ')
        self.store.initialize()
        self.assertNotIn(pid,PLANS)
        self.assertEqual(self.cat.category(cid)['deleted'],1)
        with self.assertRaises(CatalogError):self.cat.delete_category(900,cid,revision)

    def test_new_category_wizard_and_destroy_ui(self):
        self.click(900,'adm:ctnew')
        self.msg(900,'پنل نقره‌ای')
        cats=self.cat.categories()
        cid=[c['id'] for c in cats if c['name']=='پنل نقره‌ای'][0]
        self.click(900,'adm:catnew')
        self.msg(900,'نقره‌ای 35 گیگ')
        _,draft=self.store.get_state(900)
        import json
        options=json.loads(draft)['category_options']
        self.click(900,f'adm:catkind:{options.index("پنل نقره‌ای")}')
        for content in ['35','290000','2 نفر','30 روز']:
            self.msg(900,content)
        self.click(900,'adm:catcreate')
        plan=[x for x in self.cat.all() if x['type']=='پنل نقره‌ای'][0]
        self.click(900,f'adm:catdelask:{plan["plan_id"]}')
        self.click(900,f'adm:catdelsave:{plan["plan_id"]}')
        self.assertNotIn(plan['plan_id'],PLANS)
        self.click(900,f'adm:ctask:{cid}')
        self.click(900,f'adm:ctsave:{cid}')
        self.assertEqual(self.cat.category(cid)['deleted'],1)

    def test_delete_confirmation_is_admin_only_and_stale_rejected(self):
        pid=self.cat.all()[0]['plan_id']
        self.click(200,f'adm:catdelask:{pid}')
        self.click(200,f'adm:catdelsave:{pid}')
        self.assertFalse(self.cat.get(pid)['deleted'])
        self.click(900,f'adm:catdelask:{pid}')
        self.cat.change(900,pid,'price','450000',self.cat.get(pid)['revision'])
        self.click(900,f'adm:catdelsave:{pid}')
        self.assertFalse(self.cat.get(pid)['deleted'])

    def test_category_duplicate_reject_and_reuse_after_deletion(self):
        cid=self.cat.create_category(900,'پنل نقره‌ای')
        with self.assertRaises(CatalogError):self.cat.create_category(900,'پنل نقره‌ای')
        self.cat.delete_category(900,cid,self.cat.category(cid)['revision'])
        cid2=self.cat.create_category(900,'پنل نقره‌ای')
        self.assertEqual(cid,cid2)
        self.assertEqual(self.cat.category(cid)['deleted'],0)

    def test_no_unknown_category_or_malicious_data(self):
        with self.assertRaises(CatalogError):self.new_plan('دسته نامعلوم')
        with self.assertRaises(CatalogError):self.cat.create_category(900,'bad\nvalue')
        cid=self.cat.create_category(900,'پنل نقره‌ای')
        pid=self.new_plan()
        with self.assertRaises(CatalogError):self.cat.change(900,pid,'type','دسته نامعلوم',self.cat.get(pid)['revision'])
        self.assertEqual(self.cat.get(pid)['type'],'پنل نقره‌ای')

    def test_category_revision_conflict_when_plan_added(self):
        cid=self.cat.create_category(900,'پنل نقره‌ای')
        old_revision=self.cat.category(cid)['revision']
        self.new_plan()
        with self.assertRaises(CatalogError):self.cat.delete_category(900,cid,old_revision)

    def test_bale_admin_category_workflow(self):
        self.balemgr.catalog_callback(900,'adm:ctnew')
        self.balemgr.catalog_text(900,'پنل نقره‌ای')
        category=[x for x in self.cat.categories() if x['name']=='پنل نقره‌ای'][0]
        self.balemgr.catalog_callback(900,f'adm:ctask:{category["id"]}')
        self.balemgr.catalog_callback(900,f'adm:ctsave:{category["id"]}')
        self.assertEqual(self.cat.category(category['id'])['deleted'],1)
        self.assertFalse(self.balemgr.catalog_callback(1000,'adm:ctnew'))

if __name__=='__main__':unittest.main()
