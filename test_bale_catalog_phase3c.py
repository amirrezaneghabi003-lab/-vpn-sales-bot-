"""Bale admin in-chat UX and shared SQLite management (no network or tokens)."""
import os
import tempfile
import unittest
from pathlib import Path
from telegram_store import TelegramStore
from bale_catalog_admin import BaleCatalogAdmin
from service_catalog import CatalogStore, PLANS

class FakeBaleDB:
    def __init__(self,path):
        self.db_name=str(path)
        self.states={}
    def is_admin(self,user_id):return user_id==900
    def get_user_state(self,user_id):return self.states.get(user_id,(None,None))
    def set_user_state(self,user_id,state,data=None):self.states[user_id]=(state,data)
    def delete_user_state(self,user_id):self.states.pop(user_id,None)

class BaleCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old=os.environ.get('BOT_DB_PATH')
        self.path=Path(self.tmp.name)/'shop.db'
        os.environ['BOT_DB_PATH']=str(self.path)
        TelegramStore(self.path).initialize()
        self.sent=[]
        self.db=FakeBaleDB(self.path)
        self.manager=BaleCatalogAdmin(self.db,lambda uid,text,reply_markup=None:self.sent.append((uid,text,reply_markup)))
        self.cat=CatalogStore(self.path)
    def tearDown(self):
        if self.old is None:os.environ.pop('BOT_DB_PATH',None)
        else:os.environ['BOT_DB_PATH']=self.old
        self.tmp.cleanup()
    def test_admin_only(self):
        self.assertFalse(self.manager.catalog_callback(901,'adm:catnew'))
        self.assertFalse(self.manager.catalog_text(901,'any'))
        self.assertFalse(self.db.states)
    def test_service_create_with_reusable_wizard(self):
        initial=len(self.cat.all())
        self.manager.catalog_callback(900,'adm:catnew')
        self.manager.catalog_text(900,'بین‌الملل 33 گیگ')
        self.manager.catalog_callback(900,'adm:catkind:1')
        self.manager.catalog_text(900,'33')
        self.manager.catalog_text(900,'370000')
        self.manager.catalog_text(900,'برای 2 نفر')
        self.manager.catalog_text(900,'30 روز')
        self.manager.catalog_callback(900,'adm:catcreate')
        self.assertEqual(len(self.cat.all()),initial+1)
        self.assertTrue(any('بین‌الملل 33 گیگ' in x['display'] for x in self.cat.all()))
        self.assertEqual(self.sent[-1][0],900)
        self.assertNotIn('<b>',self.sent[-1][1])
    def test_bale_can_toggle_availability(self):
        pid=self.cat.all()[0]['plan_id']
        self.manager.catalog_callback(900,f'adm:catask:{pid}:available')
        self.assertEqual(self.cat.get(pid)['available'],1)
        self.manager.catalog_callback(900,f'adm:catsave:{pid}')
        self.assertEqual(self.cat.get(pid)['available'],0)
        self.assertFalse(TelegramStore(self.path).plan_status(pid)[0])
    def test_same_plan_change_updates_both_interfaces(self):
        pid=self.cat.all()[0]['plan_id']
        self.manager.catalog_callback(900,f'adm:catedit:{pid}:price')
        self.manager.catalog_text(900,'830000')
        self.manager.catalog_callback(900,f'adm:catsave:{pid}')
        self.assertEqual(self.cat.get(pid)['price'],830000)
        self.assertEqual(PLANS[pid]['price'],830000)
    def test_custom_single_user_category_requires_notice(self):
        pid=self.cat.create(900,{'display':'نامحدود اختصاصی ویژه','type':'تک کاربره نامحدود',
                 'size':102400,'price':500000,'users':'نامحدود','duration':'30 روز'})
        self.assertEqual(self.cat.get(pid)['users'],'تک کاربره')
        self.assertIn(pid,PLANS)
    def test_idempotent_create_retry(self):
        data={'display':'test service','type':'طلایی','size':10240,'price':250000,
              'users':'two users','duration':'30 days'}
        a=self.cat.create(900,data,request_id='a'*24)
        b=self.cat.create(900,data,request_id='a'*24)
        self.assertEqual(a,b)
        self.assertEqual(sum(r['plan_id']==a for r in self.cat.all()),1)

if __name__=='__main__':unittest.main()
