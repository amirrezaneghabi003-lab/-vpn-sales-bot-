"""Security and workflow regressions, entirely offline, disposable SQLite database."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from telegram_store import TelegramStore
from telegram_v2 import Storefront


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.db'
        self.db = TelegramStore(self.path)
        self.db.initialize()
        self.api = Mock()
        self.api.send.return_value = {'message_id': 1}
        self.api.edit.return_value = {'message_id': 1}
        self.api.answer.return_value = True
        self.app = Storefront(self.db,self.api,admin_ids={100},card_number='',card_holder='',support_contact='')
        self.db.upsert_user(100,'owner','مدیر')
        self.db.upsert_user(200,'user200','مشتری')
        self.db.upsert_user(201,'another','مشتری دوم')

    def tearDown(self):
        self.temp.cleanup()

    def event(self, user_id, data):
        self.app.callback({'from':{'id':user_id, 'first_name':'person'},
                           'message':{'chat':{'id':user_id, 'type':'private'},'message_id':4},
                           'data':data,'id':'q'})

    def message(self, user_id, text):
        return self.app.message({'chat':{'id':user_id,'type':'private'},'from':{'id':user_id},'text':text})

    def test_migration_repeat_and_user_tables_preserved(self):
        self.db.initialize()
        with self.db.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_users').fetchone()[0],3)
            self.assertTrue(conn.execute("SELECT name FROM sqlite_master WHERE name='tg_admin_audit'").fetchone())

    def test_admin_menu_only_authorized(self):
        self.event(200,'adm:home')
        self.assertFalse(self.api.edit.called)
        self.api.reset_mock()
        self.event(100,'adm:home')
        self.assertTrue(self.api.edit.called)

    def test_no_admin_buttons_for_regular_user(self):
        self.message(200,'/start')
        markup=self.api.send.call_args.args[2]
        labels=str(markup)
        self.assertNotIn('adm:home', labels)
        self.api.reset_mock()
        self.message(100,'/start')
        self.assertIn('adm:home',str(self.api.send.call_args.args[2]))

    def test_admin_search_id_username_and_literal_wildcards(self):
        self.assertEqual(self.db.search_users('200')[0]['user_id'],200)
        self.assertEqual(self.db.search_users('@user200')[0]['user_id'],200)
        self.assertEqual(self.db.search_users('%'),[])
        self.assertEqual(self.db.search_users('_'),[])

    def test_search_prompt_handles_input(self):
        self.event(100,'adm:finduser')
        self.assertEqual(self.db.get_state(100)[0],'adm_finduser')
        self.message(100,'user200')
        self.assertIsNone(self.db.get_state(100)[0])
        self.assertIn('adm:user:200', str(self.api.send.call_args))

    def test_block_and_unblock(self):
        self.event(100,'adm:block:200')
        self.assertTrue(self.db.admin_is_blocked(200))
        self.event(100,'adm:unblock:200')
        self.assertFalse(self.db.admin_is_blocked(200))
        with self.db.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_admin_audit').fetchone()[0],2)

    def test_admin_cannot_be_blocked(self):
        self.event(100,'adm:block:100')
        self.assertFalse(self.db.admin_is_blocked(100))

    def test_blocked_user_cannot_order_or_use_callbacks(self):
        self.db.admin_set_block(200,True)
        self.api.reset_mock()
        self.event(200,'catalog')
        self.assertFalse(self.api.edit.called)
        self.api.reset_mock()
        self.message(200,'/buy')
        self.assertNotIn('catalog',str(self.api.send.call_args))

    def test_direct_message_only_admin_and_known_user(self):
        self.event(200,'adm:dm:201')
        self.assertNotEqual(self.db.get_state(200)[0],'adm_message')
        self.event(100,'adm:dm:201')
        self.assertEqual(self.db.get_state(100),('adm_message','201'))
        self.api.reset_mock()
        self.message(100,'متن <b> تست')
        self.assertEqual(self.api.send.call_args_list[0].args[0],201)
        self.assertIn('&lt;b&gt;', self.api.send.call_args_list[0].args[1])
        self.assertIsNone(self.db.get_state(100)[0])

    def test_direct_message_refuses_blocked(self):
        self.db.admin_set_block(201,True)
        self.event(100,'adm:dm:201')
        self.assertNotEqual(self.db.get_state(100)[0],'adm_message')

    def test_stats_for_active_users(self):
        s=self.db.detailed_admin_stats()
        self.assertEqual(s['users'],3)
        self.assertEqual(s['active_7'],3)
        self.assertEqual(s['active_30'],3)

    def test_order_ref_parser_and_unauthorized_denied(self):
        order,_=self.db.open_order(200, 'sample', 120000)
        self.assertEqual(order['id'],1)
        self.app.admin_order_search(100,'pro121')
        self.assertIn('pro121',str(self.api.send.call_args))
        self.api.reset_mock()
        self.event(200,'adm:order:1')
        self.assertFalse(self.api.edit.called)

    def test_no_duplicates_approval(self):
        self.db.open_order(200,'sample', 1000)
        self.assertFalse(self.db.approve_order(1,'vless://test'))


if __name__=='__main__':
    unittest.main()
