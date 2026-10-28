"""Offline regression tests for Telegram bulk announcement queue and admin controls."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from telegram_broadcast import BroadcastEngine, RateGate
from telegram_store import TelegramStore
from telegram_v2 import Storefront, TelegramAPIError


class FakeApi:
    def __init__(self,failures=None):
        self.failures=dict(failures or {})
        self.calls=[]
        self.session=Mock()
    def send(self,user,text,markup=None):
        self.calls.append(('text',user,text))
        if user in self.failures: raise self.failures.pop(user)
        return {'message_id':100+len(self.calls)}
    def photo(self,user,file_id,caption=None,markup=None):
        self.calls.append(('photo',user,file_id,caption))
        if user in self.failures: raise self.failures.pop(user)
        return {'message_id':200+len(self.calls)}
    def document(self,user,file_id,caption=None,markup=None):
        self.calls.append(('document',user,file_id,caption))
        if user in self.failures: raise self.failures.pop(user)
        return {'message_id':300+len(self.calls)}


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=TelegramStore(Path(self.temp.name)/'test.sqlite')
        self.store.initialize()
        for uid in (10,20,21,22,23,24):
            self.store.upsert_user(uid,str(uid),'user')
        self.api=Mock()
        self.api.send.return_value={'message_id':1}
        self.api.edit.return_value={'message_id':2}
        self.api.answer.return_value=True
        self.api.photo.return_value={'message_id':3}
        self.app=Storefront(self.store,self.api,admin_ids={10},card_number='',card_holder='',support_contact='')

    def tearDown(self):
        self.temp.cleanup()

    def event(self,who,action):
        return self.app.callback({'id':'callback','from':{'id':who},'data':action,
                 'message':{'message_id':3,'chat':{'id':who,'type':'private'}}})
    def text(self,who,body):
        return self.app.message({'chat':{'id':who,'type':'private'},'from':{'id':who},'text':body})
    def image(self,who,caption=''):
        return self.app.message({'chat':{'id':who,'type':'private'},'from':{'id':who},
                     'photo':[{'file_id':'tg_image_valid'}],'caption':caption})

    def create(self,kind='text'):
        self.store.bc_save_draft(10,kind,'سلام همه', 'image-id' if kind=='photo' else None)
        return self.store.bc_create_job(10,{10})

    def test_additive_migration_idempotent(self):
        self.store.initialize()
        self.assertEqual(self.store.user_count(),6)
        with self.store.connection() as c:
            self.assertIsNotNone(c.execute("SELECT name FROM sqlite_master WHERE name='tg_bc_targets'").fetchone())

    def test_snapshot_excludes_admin_optouts_and_blocked(self):
        self.store.bc_subscription(20,False)
        self.store.admin_set_block(21,True)
        self.store.bc_mark_blocked(22)
        job=self.create()
        with self.store.connection() as c:
            ids=[r[0] for r in c.execute('SELECT user_id FROM tg_bc_targets WHERE job_id=? ORDER BY user_id',(job,))]
        self.assertEqual(ids,[23,24])

    def test_snapshot_doesnt_add_future_signups(self):
        job=self.create()
        self.store.upsert_user(999,'late','late')
        self.assertEqual(self.store.bc_stats(job)['total'],5)

    def test_duplicate_click_does_not_duplicate_campaign(self):
        one=self.create()
        two=self.store.bc_create_job(10,{10})
        self.assertEqual(one,two)
        self.assertEqual(self.store.bc_stats(one)['total'],5)

    def test_regular_user_cannot_compose_or_start(self):
        self.event(20,'adm:bc:begin')
        self.assertNotEqual(self.store.get_state(20)[0],'bc_compose')
        self.event(10,'adm:bc:begin')
        self.assertEqual(self.store.get_state(10)[0],'bc_compose')
        self.event(20,'adm:bc:prepare')
        self.assertIsNone(self.store.bc_last_job())

    def test_admin_text_photo_and_caption_preview(self):
        self.event(10,'adm:bc:begin')
        self.text(10,'سلام <b> مخاطبان')
        d=self.store.bc_draft(10)
        self.assertEqual(d['kind'],'text')
        self.assertTrue(any('سلام &lt;b&gt;' in str(x) for x in self.api.send.call_args_list))
        self.event(10,'adm:bc:begin')
        self.image(10,'معرفی جدید')
        self.assertEqual(self.store.bc_draft(10)['kind'],'photo')
        self.assertTrue(self.api.photo.called)
        self.assertIsNone(self.store.get_state(10)[0])

    def test_photo_uses_file_id_not_reupload(self):
        job=self.create('photo')
        self.store.bc_change_job(job,'running')
        row=self.store.bc_claim()
        self.assertEqual(row['file_id'],'image-id')
        fake=FakeApi()
        engine=BroadcastEngine(self.store,lambda:fake)
        engine.gate.wait=lambda:None
        # Explicit simulation; no background threads launched.
        from telegram_broadcast_admin import BroadcastAdminMixin
        BroadcastAdminMixin.bc_send_payload(fake,row['user_id'],row)
        self.assertEqual(fake.calls[0][0],'photo')
        self.assertEqual(fake.calls[0][2],'image-id')
        self.store.bc_resolve(job,row['user_id'],'sent',message_id=9)

    def test_pause_resume_and_cancel_no_duplicate(self):
        job=self.create()
        self.assertTrue(self.store.bc_change_job(job,'running'))
        r=self.store.bc_claim()
        self.assertEqual(r['user_id'],20)
        self.assertTrue(self.store.bc_change_job(job,'paused'))
        self.assertIsNone(self.store.bc_claim())
        self.store.bc_resolve(job,20,'sent',message_id=90)
        self.assertTrue(self.store.bc_change_job(job,'running'))
        self.assertEqual(self.store.bc_claim()['user_id'],21)
        self.assertTrue(self.store.bc_change_job(job,'cancelled'))
        self.assertFalse(self.store.bc_change_job(job,'running'))
        s=self.store.bc_stats(job)
        self.assertEqual(s['counts']['skipped'],3)

    def test_rate_limit_requeue_retry_after(self):
        job=self.create()
        self.store.bc_change_job(job,'running')
        row=self.store.bc_claim()
        self.store.bc_resolve(job,row['user_id'],'retry',retry_after=60,error='429')
        self.assertEqual(self.store.bc_stats(job)['counts']['retry'],1)
        # Earliest next recipient remains processable while retry delayed.
        self.assertEqual(self.store.bc_claim()['user_id'],21)

    def test_403_block_is_reported_and_excluded_next_job(self):
        job=self.create();self.store.bc_change_job(job,'running')
        row=self.store.bc_claim()
        self.store.bc_resolve(job,row['user_id'],'blocked',error='403 bot blocked')
        self.assertEqual(self.store.bc_stats(job)['counts']['blocked'],1)
        self.store.bc_save_draft(10,'text','new')
        other=self.store.bc_create_job(10,{10})
        self.assertEqual(self.store.bc_stats(other)['total'],4)

    def test_ambiguous_on_crash_not_auto_retried(self):
        job=self.create();self.store.bc_change_job(job,'running')
        row=self.store.bc_claim()
        self.assertEqual(self.store.bc_recover_interrupted(),1)
        self.assertEqual(self.store.bc_stats(job)['counts']['uncertain'],1)
        self.assertNotEqual(self.store.bc_claim()['user_id'],row['user_id'])

    def test_optout_and_reenable_commands(self):
        self.text(20,'/unsubscribe')
        self.assertEqual(self.store.bc_create_job(10,{10}),None)
        self.store.bc_save_draft(10,'text','msg')
        job=self.store.bc_create_job(10,{10})
        self.assertEqual(self.store.bc_stats(job)['total'],4)
        self.text(20,'/subscribe')
        self.store.bc_save_draft(10,'text','second')
        job2=self.store.bc_create_job(10,{10})
        self.assertEqual(self.store.bc_stats(job2)['total'],5)

    def test_report_counts_correct(self):
        job=self.create();self.store.bc_change_job(job,'running')
        self.store.bc_resolve(job,20,'sent',message_id=1) if False else None
        u=self.store.bc_claim();self.store.bc_resolve(job,u['user_id'],'sent',message_id=3)
        u=self.store.bc_claim();self.store.bc_resolve(job,u['user_id'],'unreachable',error='400')
        u=self.store.bc_claim();self.store.bc_resolve(job,u['user_id'],'uncertain',error='timeout')
        s=self.store.bc_stats(job)
        self.assertEqual(s['total'],5)
        self.assertEqual(s['counts']['sent'],1)
        self.assertEqual(s['counts']['unreachable'],1)
        self.assertEqual(s['counts']['uncertain'],1)
        self.assertEqual(s['counts']['pending'],2)

    def test_optout_during_active_campaign_skips_queued_and_inflight(self):
        job=self.create();self.store.bc_change_job(job,'running')
        claimed=self.store.bc_claim()
        self.store.bc_subscription(20,False)
        self.store.bc_subscription(21,False)
        fake=FakeApi()
        BroadcastEngine(self.store,lambda:fake).process_claim(fake,claimed)
        self.assertEqual(fake.calls,[])
        summary=self.store.bc_stats(job)
        self.assertEqual(summary['counts']['skipped'],2)
        self.assertEqual(summary['counts']['pending'],3)

    def test_admin_block_during_campaign_prevents_dispatch(self):
        job=self.create();self.store.bc_change_job(job,'running')
        row=self.store.bc_claim()
        self.store.admin_set_block(20,True)
        fake=FakeApi()
        BroadcastEngine(self.store,lambda:fake).process_claim(fake,row)
        self.assertEqual(fake.calls,[])
        self.assertEqual(self.store.bc_stats(job)['counts']['skipped'],1)

    def test_reconnecting_user_clears_blocked_delivery_flag_not_optout(self):
        self.store.bc_mark_blocked(20)
        self.store.bc_subscription(20,False)
        self.text(20,'/myid')
        with self.store.connection() as c:
            row=c.execute('SELECT * FROM tg_bc_prefs WHERE user_id=20').fetchone()
        self.assertEqual(row['bot_blocked'],0)
        self.assertEqual(row['unsubscribed'],1)

    def test_pause_before_network_does_not_exhaust_attempts(self):
        job=self.create()
        for _ in range(9):
            self.store.bc_change_job(job,'running')
            row=self.store.bc_claim()
            self.store.bc_change_job(job,'paused')
            BroadcastEngine(self.store,lambda:FakeApi()).process_claim(FakeApi(),row)
        with self.store.connection() as c:
            attempts=c.execute('SELECT attempts FROM tg_bc_targets WHERE job_id=? AND user_id=20',(job,)).fetchone()[0]
        self.assertEqual(attempts,0)

    def test_lease_enforces_single_broadcaster(self):
        self.assertTrue(self.store.bc_lease_acquire('one'))
        self.assertFalse(self.store.bc_lease_acquire('two'))
        self.assertTrue(self.store.bc_lease_renew('one'))
        self.store.bc_lease_release('one')
        self.assertTrue(self.store.bc_lease_acquire('two'))

    def test_worker_success_and_blocked(self):
        job=self.create()
        self.store.bc_change_job(job,'running')
        fake=FakeApi({20:TelegramAPIError(403,'Forbidden: bot was blocked by the user')})
        engine=BroadcastEngine(self.store,lambda:fake)
        first=self.store.bc_claim()
        engine.process_claim(fake,first)
        self.assertEqual(self.store.bc_stats(job)['counts']['blocked'],1)
        second=self.store.bc_claim()
        engine.process_claim(fake,second)
        self.assertEqual(self.store.bc_stats(job)['counts']['sent'],1)
        self.assertNotIn('/unsubscribe',fake.calls[-1][2])

    def test_worker_429_retry_after_and_recovery(self):
        job=self.create()
        self.store.bc_change_job(job,'running')
        first=self.store.bc_claim()
        fake=FakeApi({20:TelegramAPIError(429,'Too Many Requests',retry_after=15)})
        engine=BroadcastEngine(self.store,lambda:fake)
        engine.process_claim(fake,first)
        self.assertEqual(self.store.bc_stats(job)['counts']['retry'],1)
        with self.store.connection() as c:
            wait=c.execute('SELECT due_at FROM tg_bc_targets WHERE job_id=? AND user_id=20',(job,)).fetchone()[0]
        import time
        self.assertGreater(wait,time.time()+14)
        self.assertGreater(engine.gate.cooldown_until,0)

    def test_worker_timeout_uncertain_without_retry(self):
        job=self.create();self.store.bc_change_job(job,'running')
        first=self.store.bc_claim()
        engine=BroadcastEngine(self.store,lambda:None)
        fake=FakeApi({20:TelegramAPIError(0,'ReadTimeout')})
        engine.process_claim(fake,first)
        self.assertEqual(self.store.bc_stats(job)['counts']['uncertain'],1)
        self.assertEqual(self.store.bc_claim()['user_id'],21)

    def test_pause_while_queued_prevents_send(self):
        job=self.create();self.store.bc_change_job(job,'running')
        first=self.store.bc_claim()
        self.store.bc_change_job(job,'paused')
        fake=FakeApi()
        BroadcastEngine(self.store,lambda:fake).process_claim(fake,first)
        self.assertEqual(fake.calls,[])
        self.assertEqual(self.store.bc_stats(job)['counts']['pending'],5)

    def test_empty_campaign_finishes_when_started(self):
        for uid in (20,21,22,23,24):self.store.bc_subscription(uid,False)
        job=self.create()
        self.assertEqual(self.store.bc_stats(job)['total'],0)
        self.assertTrue(self.store.bc_change_job(job,'running'))
        self.assertEqual(self.store.bc_stats(job)['status'],'completed')

    def test_rate_gate_caps_speed(self):
        timeline=[0.0]
        delays=[]
        gate=RateGate(20,clock=lambda: timeline[0],sleeper=lambda d:(delays.append(d),timeline.__setitem__(0,timeline[0]+d)))
        gate.wait();gate.wait();gate.wait()
        self.assertAlmostEqual(timeline[0],0.1)
        gate.cooldown(3)
        gate.wait()
        self.assertGreaterEqual(timeline[0],3.1-1e-5)

    def test_content_length_and_escaping(self):
        with self.assertRaises(ValueError):self.store.bc_save_draft(10,'text','x'*3901)
        with self.assertRaises(ValueError):self.store.bc_save_draft(10,'photo','x'*851,'id')
        self.store.bc_save_draft(10,'text','<script>')
        d=self.store.bc_draft(10)
        fake=FakeApi()
        self.app.bc_send_payload(fake,20,d)
        self.assertIn('&lt;script&gt;',fake.calls[0][2])
        self.assertNotIn('/unsubscribe',fake.calls[0][2])


if __name__=='__main__':unittest.main()
