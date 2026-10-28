"""Offline end-to-end tests: no real Bale API, no production data."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bale_broadcast import BaleBroadcast, BroadcastStore, classify
from database import Database


class BaleBroadcastTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'bale.sqlite')
        with patch.dict('os.environ',{'BOT_DB_PATH':self.path}):
            self.db=Database(self.path)
        self.db.save_user(1000,'admin','مدیر')
        self.db.add_admin(1000,1000)
        for u in range(1,251):self.db.save_user(u,f'u{u}',f'کاربر{u}')
        self.deliveries=[]
        self.report_messages=[]
        self.responses={}
        def send(target,text,reply_markup=None):
            self.deliveries.append((target,text,reply_markup))
            if target==1000:self.report_messages.append(text)
            return self.responses.get(target,{'ok':True})
        def photo(target,fileid,caption=None):
            self.deliveries.append((target,('photo',fileid,caption),None))
            if target==1000:self.report_messages.append(('photo',fileid,caption))
            return self.responses.get(target,{'ok':True})
        self.send=send;self.photo=photo
        self.manager=BaleBroadcast(self.db,send,photo,rate=100)

    def draft(self,kind='text',content='سلام',caption=''):
        return self.manager.store.draft(1000,kind,content,caption)

    def test_admin_only_and_forged_callbacks(self):
        self.assertFalse(self.manager.begin(1))
        self.assertTrue(self.manager.callback(1,'bbr:new'))
        self.assertIsNone(self.db.get_user_state(1)[0])
        self.manager.begin(1000)
        self.manager.capture(1000,text='آزمایش')
        cid=self.manager.store.last(1000)
        self.manager.callback(1,f'bbr:start:{cid}')
        self.assertEqual(self.manager.store.get(cid)['status'],'draft')
        self.manager.callback(1000,f'bbr:start:{cid+99}')
        self.assertEqual(self.manager.store.get(cid)['status'],'draft')

    def test_text_preview_and_one_click_start(self):
        self.manager.begin(1000)
        self.manager.capture(1000,text='تست')
        cid=self.manager.store.last(1000)
        self.assertFalse(any(u!=1000 for u,*_ in self.deliveries))
        self.assertTrue(any(msg=='تست' for msg in self.report_messages))
        self.assertTrue(any('🚀 تأیید و شروع' in str(item) for item in self.deliveries))
        self.manager.callback(1000,f'bbr:start:{cid}')
        self.manager.callback(1000,f'bbr:start:{cid}')
        self.assertEqual(self.manager.store.get(cid)['total'],251)
        self.assertEqual(self.manager.store.get(cid)['status'],'running')
        with closing(sqlite3.connect(self.path)) as conn:
            n=conn.execute('SELECT COUNT(*) FROM bale_broadcast_targets WHERE campaign_id=?',(cid,)).fetchone()[0]
        self.assertEqual(n,251)

    def test_photo_and_caption_preserved_without_footer(self):
        self.manager.begin(1000)
        self.manager.capture(1000,photo_id='BALE_PHOTO_ABC',caption='فروش ویژه')
        cid=self.manager.store.last(1000)
        self.manager.callback(1000,f'bbr:start:{cid}')
        # Process one report or recipient.
        self.manager.process_one()
        self.assertIn(('photo','BALE_PHOTO_ABC','فروش ویژه'),[x[1] for x in self.deliveries])
        self.assertFalse(any('/unsubscribe' in str(x) for x in self.deliveries))

    def test_first_100_second_100_and_final_only_once(self):
        # exclude admin; exactly 250 recipients
        self.db.add_admin_block(1000)
        cid=self.draft()
        self.manager.store.start(cid,1000)
        for _ in range(250):
            self.manager.process_one()
            # queued reports are sent before next target
        for _ in range(10):
            if not self.manager.process_one():break
        reports=[m for m in self.report_messages if isinstance(m,str) and ('گزارش ' in m or 'به پایان رسید' in m)]
        self.assertEqual(sum('گزارش 100 مخاطب' in s for s in reports),1)
        self.assertEqual(sum('گزارش 200 مخاطب' in s for s in reports),1)
        self.assertEqual(sum('به پایان رسید' in s for s in reports),1)
        summary=self.manager.store.summary(cid)
        self.assertEqual(summary['settled'],250)
        self.assertEqual(summary['campaign']['status'],'completed')
        self.manager.store.reports(cid)
        self.assertIsNone(self.manager.process_one())

    def test_429_wait_and_no_skip(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('DELETE FROM users WHERE user_id NOT IN (1000,1)');c.commit()
        self.db.add_admin_block(1000)
        cid=self.draft()
        self.manager.store.start(cid,1000)
        self.responses[1]={'ok':False,'error_code':429,'parameters':{'retry_after':2}}
        self.assertEqual(self.manager.process_one(),'retry')
        s=self.manager.store.summary(cid)
        self.assertEqual(s['settled'],0)
        self.assertEqual(s['counts']['retry'],1)
        self.assertIsNone(self.manager.process_one())
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("UPDATE bale_broadcast_targets SET retry_at='2000-01-01' WHERE campaign_id=?",(cid,))
            c.execute("UPDATE bale_broadcast_campaigns SET cooldown_until='2000-01-01' WHERE id=?",(cid,))
            c.commit()
        self.responses.pop(1)
        self.assertEqual(self.manager.process_one(),'sent')
        self.manager.process_one()
        self.assertEqual(self.manager.store.summary(cid)['campaign']['status'],'completed')

    def test_timeout_unknown_never_silent_retry(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('DELETE FROM users WHERE user_id NOT IN (1000,1)');c.commit()
        self.db.add_admin_block(1000)
        cid=self.draft();self.manager.store.start(cid,1000)
        self.responses[1]=None
        self.assertEqual(self.manager.process_one(),'unknown')
        self.assertEqual(self.manager.store.summary(cid)['counts']['unknown'],1)
        self.assertEqual(len([u for u,*_ in self.deliveries if u==1]),1)
        self.manager.process_one()
        self.assertEqual(len([u for u,*_ in self.deliveries if u==1]),1)

    def test_restart_inflight_is_ambiguous(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('DELETE FROM users WHERE user_id NOT IN (1000,1)');c.commit()
        self.db.add_admin_block(1000)
        cid=self.draft();self.manager.store.start(cid,1000)
        claimed=self.manager.store.claim()
        self.assertEqual(claimed['user_id'],1)
        reopened=BroadcastStore(self.path)
        reopened.recover()
        self.assertEqual(reopened.summary(cid)['counts']['unknown'],1)
        self.assertIsNone(reopened.claim())

    def test_pause_and_resume(self):
        cid=self.draft();self.manager.store.start(cid,1000)
        self.assertTrue(self.manager.store.pause(cid,1000,True))
        self.assertIsNone(self.manager.store.claim())
        self.assertFalse(self.manager.store.pause(cid,1,False))
        self.assertTrue(self.manager.store.pause(cid,1000,False))
        self.assertIsNotNone(self.manager.store.claim())

    def test_cancel_draft_and_duplicate_start(self):
        cid=self.draft();self.assertTrue(self.manager.store.cancel_draft(cid,1000))
        self.assertFalse(self.manager.store.start(cid,1000))
        cid2=self.draft();self.assertTrue(self.manager.store.start(cid2,1000))
        self.assertFalse(self.manager.store.start(cid2,1000))

    def test_mixed_200_reports_count_terminal_not_only_sent(self):
        self.db.add_admin_block(1000)
        cid=self.draft();self.manager.store.start(cid,1000)
        for u in range(1,201):
            if u%7==0:self.responses[u]={'ok':False,'error_code':403,'description':'bot was blocked by the user'}
            if u%11==0:self.responses[u]={'ok':False,'error_code':400,'description':'chat not found'}
        for _ in range(260):
            if self.manager.process_one() is None:break
        s=self.manager.store.summary(cid)
        self.assertEqual(s['settled'],250)
        self.assertGreater(s['counts']['blocked'],0)
        self.assertGreater(s['counts']['failed'],0)
        self.assertEqual(s['campaign']['status'],'completed')

    def test_rejected_empty_and_oversize(self):
        with self.assertRaises(ValueError):self.manager.store.draft(1000,'text','')
        with self.assertRaises(ValueError):self.manager.store.draft(1000,'photo','f', 'x'*1000)
        with self.assertRaises(ValueError):self.manager.store.draft(1000,'text','y'*4000)

    def test_milestones_survive_restart_without_duplicate(self):
        self.db.add_admin_block(1000)
        cid=self.draft();self.manager.store.start(cid,1000)
        for _ in range(100):
            self.manager.process_one()
        self.manager.store.recover()
        reopened=BaleBroadcast(self.db,self.send,self.photo)
        for _ in range(155):
            if reopened.process_one() is None:break
        reports=[m for m in self.report_messages if isinstance(m,str)]
        self.assertEqual(sum('گزارش 100 مخاطب' in x for x in reports),1)
        self.assertEqual(sum('گزارش 200 مخاطب' in x for x in reports),1)
        self.assertEqual(sum('به پایان رسید' in x for x in reports),1)

    def test_1000_users_durable_sql_selection(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.executemany('INSERT OR IGNORE INTO users(user_id,first_name) VALUES(?,?)',
                          [(i,'new') for i in range(300,1300)])
            c.commit()
        self.db.add_admin_block(1000)
        cid=self.draft();self.manager.store.start(cid,1000)
        self.assertEqual(self.manager.store.get(cid)['total'],1249)
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(DISTINCT user_id) FROM bale_broadcast_targets '
                'WHERE campaign_id=?',(cid,)).fetchone()[0],1249)

    def test_marketing_footer_is_never_added_by_dispatch(self):
        self.db.add_admin_block(1000)
        cid=self.draft('text','تست')
        self.manager.store.start(cid,1000)
        for _ in range(5):self.manager.process_one()
        messages=[m for u,m,meta in self.deliveries if u!=1000]
        self.assertEqual(messages,['تست']*len(messages))

    def test_classification_ambiguous_and_rate(self):
        self.assertEqual(classify(None)[0],'unknown')
        self.assertEqual(classify({'ok':True})[0],'sent')
        self.assertEqual(classify({'ok':False,'error_code':429,'parameters':{'retry_after':15}}),('retry','429',15))
        self.assertEqual(classify({'ok':False,'description':'bot was blocked by the user'})[0],'blocked')

    def test_migrate_idempotent_and_db_schema(self):
        s=BroadcastStore(self.path);s.schema();s.schema()
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')


if __name__=='__main__':unittest.main()
