"""Regression: the message sent by the admin must be delivered without appended copy."""
import tempfile
import unittest
from pathlib import Path
from telegram_broadcast_admin import BroadcastAdminMixin
from telegram_broadcast import BroadcastEngine
from telegram_store import TelegramStore
from test_broadcast_phase3b import FakeApi

class NoFooterTests(unittest.TestCase):
    def test_previews_and_test_messages_do_not_append_any_footer(self):
        for kind in ('text', 'photo', 'document'):
            for category in ('marketing', 'service'):
                with self.subTest(kind=kind, category=category):
                    api=FakeApi()
                    BroadcastAdminMixin.bc_send_payload(api, 10, {
                        'kind':kind, 'category':category, 'body':'تست',
                        'file_id': 'photo-id' if kind!='text' else None,
                    })
                    delivered=api.calls[0][2] if kind=='text' else api.calls[0][3]
                    self.assertEqual(delivered, 'تست')
                    self.assertNotIn('/unsubscribe', delivered)

    def test_worker_sends_exact_admin_body_without_footer(self):
        for kind in ('text', 'photo', 'document'):
            for category in ('marketing', 'service'):
                with self.subTest(kind=kind, category=category):
                    with tempfile.TemporaryDirectory() as folder:
                        store=TelegramStore(Path(folder)/'temp.db')
                        store.initialize()
                        store.upsert_user(10,'Admin','admin')
                        store.upsert_user(101,'Customer','customer')
                        store.bc_save_draft(10, kind, 'تست',
                             file_id='photo-id' if kind!='text' else None, category=category)
                        job=store.bc_create_job(10,{10})
                        self.assertTrue(store.bc_change_job(job,'running'))
                        row=store.bc_claim()
                        self.assertIsNotNone(row)
                        api=FakeApi()
                        BroadcastEngine(store,lambda:api).process_claim(api,row)
                        self.assertEqual(len(api.calls),1)
                        delivered=api.calls[0][2] if kind=='text' else api.calls[0][3]
                        self.assertEqual(delivered,'تست')
                        self.assertEqual(store.bc_stats(job)['counts']['sent'],1)

    def test_html_escaping_preserved(self):
        api=FakeApi()
        BroadcastAdminMixin.bc_send_payload(api,10,{
            'kind':'text','category':'marketing','body':'<b>تست & نمونه</b>', 'file_id':None})
        self.assertEqual(api.calls[0][2],'&lt;b&gt;تست &amp; نمونه&lt;/b&gt;')

if __name__=='__main__': unittest.main()
