"""Live-network-free tests for the 3B.1 reporting patch."""
import tempfile
import unittest
from pathlib import Path

from telegram_store import TelegramStore
from telegram_v2 import Storefront, TelegramAPIError
from telegram_broadcast import BroadcastEngine
from test_broadcast_phase3b import FakeApi


class AdminReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'reports.sqlite'
        self.store=TelegramStore(self.path)
        self.store.initialize()
        for uid in range(101,352):
            self.store.upsert_user(uid,str(uid),'tester')
        self.store.upsert_user(10,'admin','manager')

    def tearDown(self):
        self.tmp.cleanup()

    def create(self,category='marketing',kind='text'):
        self.store.bc_save_draft(10,kind,'پیام',file_id=('photo-id' if kind=='photo' else None),category=category)
        jid=self.store.bc_create_job(10,{10})
        self.assertTrue(self.store.bc_change_job(jid,'running'))
        return jid

    def complete(self,jid,n,state='sent'):
        # Complete a known fixed set; SQLite only, no sending in tests.
        with self.store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("""UPDATE tg_bc_targets SET status=? WHERE job_id=? AND
                user_id IN (SELECT user_id FROM tg_bc_targets WHERE job_id=? ORDER BY user_id LIMIT ?)""",
                (state,jid,jid,n))
            c.execute("""UPDATE tg_bc_jobs SET status='completed' WHERE id=? AND NOT EXISTS
                (SELECT 1 FROM tg_bc_targets WHERE job_id=? AND status IN ('pending','retry','sending'))""",(jid,jid))

    def test_milestones_100_200_and_final_once_even_after_restart(self):
        jid=self.create()
        self.complete(jid,111)
        self.store.bc_queue_admin_reports()
        self.store.bc_queue_admin_reports()
        e=self.store.bc_claim_admin_report()
        self.assertEqual((e['job_id'],e['milestone'],e['admin_id']), (jid,100,10))
        api=FakeApi();engine=BroadcastEngine(self.store,lambda:api)
        engine.process_admin_report(api,e)
        self.complete(jid,201)
        self.store.bc_queue_admin_reports()
        self.assertEqual(self.store.bc_claim_admin_report()['milestone'],200)
        self.assertIn('100',api.calls[0][2])
        # Report 200 may be interrupted after claim: never double-send automatically.
        self.assertEqual(self.store.bc_recover_admin_reports(),1)
        self.assertIsNone(self.store.bc_claim_admin_report())
        self.complete(jid,251)
        self.store.bc_queue_admin_reports()
        f=self.store.bc_claim_admin_report()
        self.assertEqual(f['milestone'],0)
        engine.process_admin_report(api,f)
        self.assertIn('به پایان رسید',api.calls[-1][2])
        self.store.bc_queue_admin_reports()
        self.assertIsNone(self.store.bc_claim_admin_report())
        statuses={r['milestone']:r['status'] for r in self.store.bc_report_statuses(jid)}
        self.assertEqual(statuses,{0:'sent',100:'sent',200:'uncertain'})
        # The uncertain event is visible for the admin; never silently resent.

    def test_exact_multiple_100_still_gets_final_report(self):
        jid=self.create()
        self.complete(jid,251)
        self.store.bc_queue_admin_reports()
        milestones=[]
        fake=FakeApi();engine=BroadcastEngine(self.store,lambda:fake)
        for _ in range(3):
            row=self.store.bc_claim_admin_report()
            milestones.append(row['milestone'])
            engine.process_admin_report(fake,row)
        self.assertEqual(milestones,[100,200,0])
        self.assertEqual(len(fake.calls),3)

    def test_429_retries_admin_report_not_customer_message(self):
        jid=self.create()
        self.complete(jid,100)
        self.store.bc_queue_admin_reports()
        row=self.store.bc_claim_admin_report()
        fake=FakeApi({10:TelegramAPIError(429,'rate limit',retry_after=7)})
        BroadcastEngine(self.store,lambda:fake).process_admin_report(fake,row)
        with self.store.connection() as c:
            r=c.execute('SELECT status,due_at FROM tg_bc_reports WHERE job_id=? AND milestone=100',(jid,)).fetchone()
        import time
        self.assertEqual(r['status'],'retry')
        self.assertGreater(r['due_at'],time.time()+7)
        self.assertIsNone(self.store.bc_claim_admin_report())

    def test_mixed_results_report_counts(self):
        jid=self.create()
        with self.store.connection() as c:
            c.execute("UPDATE tg_bc_targets SET status='sent' WHERE job_id=? AND user_id<=200",(jid,))
            c.execute("UPDATE tg_bc_targets SET status='blocked' WHERE job_id=? AND user_id BETWEEN 201 AND 210",(jid,))
            c.execute("UPDATE tg_bc_targets SET status='unreachable' WHERE job_id=? AND user_id BETWEEN 211 AND 220",(jid,))
        self.store.bc_queue_admin_reports()
        row=self.store.bc_claim_admin_report()
        self.assertEqual(row['milestone'],100)
        text=BroadcastEngine._report_text(self.store.bc_stats(jid),100)
        self.assertIn('<code>100</code>',text)
        self.assertIn('<code>10</code>',text)
        self.assertIn('باقی‌مانده',text)

    def test_service_updates_include_promo_optouts_but_not_blocked(self):
        self.store.bc_subscription(101,False)
        self.store.bc_mark_blocked(102)
        self.store.admin_set_block(103,True)
        promo=self.create(category='marketing')
        with self.store.connection() as c:
            ids={r[0] for r in c.execute('SELECT user_id FROM tg_bc_targets WHERE job_id=?',(promo,))}
        self.assertNotIn(101,ids)
        self.assertNotIn(102,ids)
        self.assertNotIn(103,ids)
        service=self.create(category='service')
        with self.store.connection() as c:
            ids={r[0] for r in c.execute('SELECT user_id FROM tg_bc_targets WHERE job_id=?',(service,))}
        self.assertIn(101,ids)
        self.assertNotIn(102,ids)
        self.assertNotIn(103,ids)
        row=self.store.bc_claim()
        self.assertEqual(row['job_id'],promo)
        self.assertEqual(row['category'],'marketing')
        # Service payload must not append opt-out notice.
        while self.store.bc_claim() is not None:
            # Claims could be exhausted on attempts; not useful here.
            break
        srow={'job_id':service,'user_id':101,'category':'service','kind':'text','body':'خبر سرویس','file_id':None}
        with self.store.connection() as c:
            c.execute("UPDATE tg_bc_targets SET status='sending' WHERE job_id=? AND user_id=101",(service,))
        fake=FakeApi()
        BroadcastEngine(self.store,lambda:fake).process_claim(fake,srow)
        self.assertEqual(len(fake.calls),1)
        self.assertNotIn('/unsubscribe',fake.calls[0][2])

    def test_service_notice_not_skipped_by_promotional_optout_mid_campaign(self):
        jid=self.create(category='service')
        self.store.bc_subscription(101,False)
        with self.store.connection() as c:
            status=c.execute('SELECT status FROM tg_bc_targets WHERE job_id=? AND user_id=101',(jid,)).fetchone()[0]
        self.assertEqual(status,'pending')
        row=self.store.bc_claim()
        self.assertEqual(row['user_id'],101)
        fake=FakeApi()
        BroadcastEngine(self.store,lambda:fake).process_claim(fake,row)
        self.assertEqual(len(fake.calls),1)
        self.assertEqual(self.store.bc_stats(jid)['counts']['sent'],1)

    def test_exact_200_recipients_produces_200_report_and_finish(self):
        # Remove 51 of 251 audience records before snapshot.
        with self.store.connection() as c:
            c.execute('DELETE FROM tg_users WHERE user_id>=301')
        jid=self.create()
        self.complete(jid,200)
        self.store.bc_queue_admin_reports()
        markers=[]
        api=FakeApi();engine=BroadcastEngine(self.store,lambda:api)
        while row:=self.store.bc_claim_admin_report():
            markers.append(row['milestone'])
            engine.process_admin_report(api,row)
        self.assertEqual(markers,[100,200,0])
        self.assertEqual(self.store.bc_stats(jid)['status'],'completed')

    def test_service_photo_preview_no_footer(self):
        from telegram_broadcast_admin import BroadcastAdminMixin
        fake=FakeApi()
        BroadcastAdminMixin.bc_send_payload(fake,123,dict(kind='photo',body='اطلاعیه',file_id='photo-id',category='service'))
        self.assertNotIn('/unsubscribe',fake.calls[0][3])
        BroadcastAdminMixin.bc_send_payload(fake,123,dict(kind='text',body='فروش ویژه',file_id=None,category='marketing'))
        self.assertNotIn('/unsubscribe',fake.calls[1][2])

    def test_admin_type_selection_and_safe_controls(self):
        from unittest.mock import Mock
        api=Mock(); api.send.return_value={'message_id':88}
        app=Storefront(self.store,api,admin_ids={10},card_number='',card_holder='',support_contact='')
        app.callback({'id':'cb','from':{'id':10},'data':'adm:bc:begin:service','message':{'chat':{'id':10,'type':'private'},'message_id':2}})
        self.assertEqual(self.store.get_state(10),('bc_compose','service'))
        app.message({'chat':{'id':10,'type':'private'},'from':{'id':10},'text':'وضعیت ضروری سرویس'})
        self.assertEqual(self.store.bc_draft(10)['category'],'service')
        app.callback({'id':'cb','from':{'id':101},'data':'adm:bc:begin:service','message':{'chat':{'id':101,'type':'private'},'message_id':2}})
        self.assertIsNone(self.store.get_state(101)[0])

if __name__=='__main__':unittest.main()
