"""Durable Telegram broadcast engine with per-100/admin completion reporting; SQLite only.

`sent` = Bot API accepted, NOT proof of device delivery/read.
Ambiguous transport errors and in-flight sends after a crash are never auto-retried:
Telegram Bot API offers no idempotency key for message delivery.
"""
from __future__ import annotations

import html
import logging
import os
import threading
import time
from contextlib import suppress

LOGGER = logging.getLogger('tg_broadcast')
REPORT_INTERVAL = 100


class BroadcastStoreMixin:
    def initialize_broadcast(self):
        with self.connection() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS tg_bc_prefs (
                    user_id INTEGER PRIMARY KEY,
                    unsubscribed INTEGER NOT NULL DEFAULT 0,
                    bot_blocked INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tg_bc_drafts (
                    admin_id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('text','photo','document')),
                    body TEXT NOT NULL,
                    file_id TEXT,
                    job_id INTEGER,
                    category TEXT NOT NULL DEFAULT 'marketing',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tg_bc_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('text','photo','document')),
                    body TEXT NOT NULL,
                    file_id TEXT,
                    category TEXT NOT NULL DEFAULT 'marketing',
                    status TEXT NOT NULL DEFAULT 'prepared'
                        CHECK(status IN ('prepared','running','paused','completed','cancelled')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS tg_bc_targets (
                    job_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','sending','retry','sent','blocked','unreachable','failed','uncertain','skipped')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    due_at REAL NOT NULL DEFAULT 0,
                    claimed_at REAL,
                    sent_at TEXT,
                    message_id INTEGER,
                    last_error TEXT,
                    PRIMARY KEY(job_id,user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tg_bc_queue ON tg_bc_targets(status,due_at,job_id);
                CREATE TABLE IF NOT EXISTS tg_bc_reports (
                    job_id INTEGER NOT NULL,
                    milestone INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    due_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    message_id INTEGER,
                    PRIMARY KEY(job_id,milestone)
                );
                CREATE INDEX IF NOT EXISTS idx_tg_bc_report_queue
                    ON tg_bc_reports(status,due_at,job_id);
                CREATE TABLE IF NOT EXISTS tg_bc_runner_lease (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    owner TEXT NOT NULL,
                    heartbeat REAL NOT NULL
                );
            ''')
            # Existing Phase3B installations already have these tables.
            for table in ('tg_bc_drafts', 'tg_bc_jobs'):
                names={r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
                if 'category' not in names:
                    c.execute("ALTER TABLE "+table+" ADD COLUMN category TEXT NOT NULL DEFAULT 'marketing'")

    def bc_subscription(self, user_id, allowed):
        with self.connection() as c:
            c.execute('''INSERT INTO tg_bc_prefs (user_id,unsubscribed) VALUES (?,?)
                ON CONFLICT(user_id) DO UPDATE SET unsubscribed=excluded.unsubscribed,
                updated_at=CURRENT_TIMESTAMP''', (user_id, 0 if allowed else 1))
            if not allowed:
                # Keep mandatory operational updates separate from promotional opt-out.
                c.execute('''UPDATE tg_bc_targets SET status='skipped'
                    WHERE user_id=? AND status IN ('pending','retry')
                    AND job_id IN (SELECT id FROM tg_bc_jobs WHERE category='marketing')''',(user_id,))

    def bc_user_reachable(self, user_id):
        with self.connection() as c:
            c.execute('UPDATE tg_bc_prefs SET bot_blocked=0,updated_at=CURRENT_TIMESTAMP WHERE user_id=?', (user_id,))

    def bc_mark_blocked(self, user_id):
        with self.connection() as c:
            c.execute('''INSERT INTO tg_bc_prefs(user_id,bot_blocked) VALUES (?,1)
                ON CONFLICT(user_id) DO UPDATE SET bot_blocked=1, updated_at=CURRENT_TIMESTAMP''', (user_id,))

    def bc_save_draft(self, admin_id, kind, body, file_id=None, category='marketing'):
        if kind not in ('text','photo','document') or not isinstance(body,str) or len(body)>3900 or (kind!='text' and not file_id):
            raise ValueError('Invalid broadcast draft')
        if category not in ('service','marketing'):
            raise ValueError('Invalid broadcast category')
        if kind!='text' and len(body)>850:
            raise ValueError('Photo caption too long')
        with self.connection() as c:
            c.execute('''INSERT INTO tg_bc_drafts(admin_id,kind,body,file_id,job_id,category)
                VALUES(?,?,?,?,NULL,?) ON CONFLICT(admin_id) DO UPDATE SET
                kind=excluded.kind,body=excluded.body,file_id=excluded.file_id,
                category=excluded.category, job_id=NULL,created_at=CURRENT_TIMESTAMP''', (admin_id,kind,body,file_id,category))

    def bc_draft(self, admin_id):
        with self.connection() as c:
            r=c.execute('SELECT * FROM tg_bc_drafts WHERE admin_id=?',(admin_id,)).fetchone()
            return dict(r) if r else None

    def bc_clear_draft(self, admin_id):
        with self.connection() as c:
            c.execute('DELETE FROM tg_bc_drafts WHERE admin_id=?',(admin_id,))

    def bc_create_job(self, admin_id, excluded_admin_ids=()):
        """Atomic recipient snapshot; repeated click returns same job ID."""
        excluded=tuple(int(x) for x in excluded_admin_ids)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            d=c.execute('SELECT * FROM tg_bc_drafts WHERE admin_id=?',(admin_id,)).fetchone()
            if not d:
                return None
            if d['job_id']:
                return d['job_id']
            cur=c.execute('INSERT INTO tg_bc_jobs(admin_id,kind,body,file_id,category) VALUES(?,?,?,?,?)',
                          (admin_id,d['kind'],d['body'],d['file_id'],d['category']))
            job=cur.lastrowid
            exclusions= (' AND u.user_id NOT IN ('+','.join('?' for _ in excluded)+')') if excluded else ''
            c.execute('''INSERT INTO tg_bc_targets(job_id,user_id)
                SELECT ?,u.user_id FROM tg_users u
                LEFT JOIN tg_admin_blocks b ON b.user_id=u.user_id
                LEFT JOIN tg_bc_prefs p ON p.user_id=u.user_id
                WHERE b.user_id IS NULL AND (?='service' OR COALESCE(p.unsubscribed,0)=0)
                AND COALESCE(p.bot_blocked,0)=0'''+exclusions, (job,d['category'],*excluded))
            c.execute('UPDATE tg_bc_drafts SET job_id=? WHERE admin_id=?',(job,admin_id))
            return job

    def bc_job(self, job_id):
        with self.connection() as c:
            r=c.execute('SELECT * FROM tg_bc_jobs WHERE id=?',(job_id,)).fetchone()
            return dict(r) if r else None

    def bc_last_job(self):
        with self.connection() as c:
            r=c.execute('SELECT id FROM tg_bc_jobs ORDER BY id DESC LIMIT 1').fetchone()
            return r[0] if r else None

    def bc_change_job(self, job_id, to_status):
        transitions={'running':('prepared','paused'), 'paused':('running',), 'cancelled':('prepared','running','paused')}
        if to_status not in transitions:
            return False
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT status,category FROM tg_bc_jobs WHERE id=?',(job_id,)).fetchone()
            if not row:
                return False
            if row['status']==to_status:
                return True
            if row['status'] not in transitions[to_status]:
                return False
            c.execute('''UPDATE tg_bc_jobs SET status=?,
                started_at=CASE WHEN ?='running' AND started_at IS NULL THEN CURRENT_TIMESTAMP ELSE started_at END,
                finished_at=CASE WHEN ?='cancelled' THEN CURRENT_TIMESTAMP ELSE finished_at END
                WHERE id=?''', (to_status,to_status,to_status,job_id))
            if to_status=='cancelled':
                c.execute("UPDATE tg_bc_targets SET status='skipped' WHERE job_id=? AND status IN ('pending','retry')",(job_id,))
            elif to_status=='running':
                c.execute('''UPDATE tg_bc_jobs SET status='completed',finished_at=CURRENT_TIMESTAMP
                    WHERE id=? AND NOT EXISTS (SELECT 1 FROM tg_bc_targets WHERE job_id=?
                    AND status IN ('pending','retry','sending'))''',(job_id,job_id))
            return True

    def bc_stats(self, job_id):
        with self.connection() as c:
            job=c.execute('SELECT * FROM tg_bc_jobs WHERE id=?',(job_id,)).fetchone()
            if not job:
                return None
            vals={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM tg_bc_targets WHERE job_id=? GROUP BY status',(job_id,))}
            return {**dict(job),'total':sum(vals.values()),'counts':vals}

    def bc_claim(self, now=None):
        """One claim per transaction, never delivered by two workers at once."""
        now=time.time() if now is None else now
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('''SELECT t.job_id,t.user_id,j.kind,j.body,j.file_id,j.category
                FROM tg_bc_targets t JOIN tg_bc_jobs j ON j.id=t.job_id
                WHERE j.status='running' AND t.status IN ('pending','retry')
                AND t.due_at<=? AND t.attempts<8
                ORDER BY t.job_id,t.user_id LIMIT 1''',(now,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE tg_bc_targets SET status='sending', attempts=attempts+1,claimed_at=? WHERE job_id=? AND user_id=?",
                      (now,row['job_id'],row['user_id']))
            return dict(row)

    def bc_should_send(self, job_id, user_id):
        """Recheck job status after the rate-limit wait and before network I/O."""
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            j=c.execute('SELECT status,category FROM tg_bc_jobs WHERE id=?',(job_id,)).fetchone()
            r=c.execute('SELECT status FROM tg_bc_targets WHERE job_id=? AND user_id=?',(job_id,user_id)).fetchone()
            if not j or not r or r['status']!='sending': return False
            if j['status']=='running':
                denied=c.execute('''SELECT 1 FROM tg_bc_prefs
                    WHERE user_id=? AND (bot_blocked=1 OR (unsubscribed=1 AND ?='marketing'))''',(user_id,j['category'])).fetchone()
                admin_blocked=c.execute('SELECT 1 FROM tg_admin_blocks WHERE user_id=?',(user_id,)).fetchone()
                if not denied and not admin_blocked:
                    return True
                next_state='skipped'
            else:
                next_state='skipped' if j['status']=='cancelled' else 'pending'
            c.execute('''UPDATE tg_bc_targets SET status=?,
                attempts=MAX(attempts-1,0),claimed_at=NULL
                WHERE job_id=? AND user_id=?''',
                      (next_state,job_id,user_id))
            return False

    def bc_resolve(self, job_id, user_id, status, *, retry_after=0, message_id=None, error=''):
        if status not in ('sent','blocked','unreachable','failed','uncertain','retry'):
            raise ValueError('Invalid resolution')
        with self.connection() as c:
            c.execute('''UPDATE tg_bc_targets SET status=?,due_at=?,message_id=?,
                sent_at=CASE WHEN ?='sent' THEN CURRENT_TIMESTAMP ELSE sent_at END,
                last_error=? WHERE job_id=? AND user_id=? AND status='sending' ''',
                (status,time.time()+retry_after if status=='retry' else 0,message_id,status,str(error)[:120],job_id,user_id))
            if status=='blocked':
                c.execute('''INSERT INTO tg_bc_prefs(user_id,bot_blocked) VALUES (?,1)
                    ON CONFLICT(user_id) DO UPDATE SET bot_blocked=1,updated_at=CURRENT_TIMESTAMP''',(user_id,))
            # Update complete ONLY when no work is inflight/pending (uncertain is terminal/manual).
            c.execute('''UPDATE tg_bc_jobs SET status='completed',finished_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='running' AND NOT EXISTS
                (SELECT 1 FROM tg_bc_targets WHERE job_id=? AND status IN ('pending','retry','sending'))''',(job_id,job_id))


    def bc_queue_admin_reports(self):
        """Record every crossed 100-recipient milestone and terminal report atomically.

        Counts terminal targets (including blocked, failed, uncertain and skipped),
        not just successes. A concurrent worker cannot create duplicate events.
        """
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            for j in c.execute("SELECT id,status FROM tg_bc_jobs WHERE started_at IS NOT NULL").fetchall():
                job_id=j['id']
                n=c.execute("""SELECT COUNT(*) FROM tg_bc_targets WHERE job_id=? AND
                    status IN ('sent','blocked','unreachable','failed','uncertain','skipped')""",(job_id,)).fetchone()[0]
                max_event=c.execute('SELECT COALESCE(MAX(milestone),0) FROM tg_bc_reports WHERE job_id=?',(job_id,)).fetchone()[0]
                for point in range(max_event+REPORT_INTERVAL, n+1, REPORT_INTERVAL):
                    c.execute('INSERT OR IGNORE INTO tg_bc_reports(job_id,milestone) VALUES (?,?)',(job_id,point))
                if j['status'] in ('completed','cancelled'):
                    c.execute('INSERT OR IGNORE INTO tg_bc_reports(job_id,milestone) VALUES (?,0)',(job_id,))

    def bc_claim_admin_report(self, now=None):
        now=time.time() if now is None else now
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute("""SELECT r.job_id,r.milestone,j.admin_id,j.status AS job_status
                FROM tg_bc_reports r JOIN tg_bc_jobs j ON j.id=r.job_id
                WHERE r.status IN ('pending','retry') AND r.due_at<=?
                ORDER BY r.job_id, CASE WHEN r.milestone=0 THEN 999999999 ELSE r.milestone END
                LIMIT 1""",(now,)).fetchone()
            if not r:return None
            c.execute("""UPDATE tg_bc_reports SET status='sending',attempts=attempts+1
                WHERE job_id=? AND milestone=?""",(r['job_id'],r['milestone']))
            return dict(r)

    def bc_resolve_admin_report(self, job_id, milestone, status, *, retry_after=0, message_id=None, error=''):
        if status not in ('sent','retry','uncertain','failed'):
            raise ValueError('Invalid report status')
        with self.connection() as c:
            c.execute("""UPDATE tg_bc_reports SET status=?,due_at=?,message_id=?,last_error=?
                WHERE job_id=? AND milestone=? AND status='sending'""",
                (status,time.time()+retry_after if status=='retry' else 0,message_id,
                 str(error)[:100],job_id,milestone))

    def bc_recover_admin_reports(self):
        # Never send again if result of previous send is ambiguous.
        with self.connection() as c:
            return c.execute("""UPDATE tg_bc_reports SET status='uncertain',
                last_error='interrupted during admin report' WHERE status='sending'""").rowcount

    def bc_report_statuses(self, job_id):
        with self.connection() as c:
            return [dict(r) for r in c.execute(
                'SELECT milestone,status FROM tg_bc_reports WHERE job_id=? ORDER BY milestone', (job_id,))]

    def bc_attempts(self, job_id, user_id):
        with self.connection() as c:
            r=c.execute('SELECT attempts FROM tg_bc_targets WHERE job_id=? AND user_id=?',(job_id,user_id)).fetchone()
            return r['attempts'] if r else 0

    def bc_recover_interrupted(self):
        """Never automatically repeat possibly delivered messages."""
        with self.connection() as c:
            n=c.execute("UPDATE tg_bc_targets SET status='uncertain',last_error='process interrupted during send' WHERE status='sending'").rowcount
            c.execute('''UPDATE tg_bc_jobs SET status='completed',finished_at=CURRENT_TIMESTAMP
                WHERE status='running' AND NOT EXISTS
                (SELECT 1 FROM tg_bc_targets WHERE job_id=tg_bc_jobs.id AND status IN ('pending','retry','sending'))''')
            return n

    def bc_has_work(self):
        with self.connection() as c:
            return bool(c.execute("SELECT 1 FROM tg_bc_jobs WHERE status='running' LIMIT 1").fetchone())

    def bc_lease_acquire(self, owner, ttl=60):
        now=time.time()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT owner,heartbeat FROM tg_bc_runner_lease WHERE id=1').fetchone()
            if row and row['owner']!=owner and row['heartbeat']>now-ttl:
                return False
            c.execute('''INSERT INTO tg_bc_runner_lease(id,owner,heartbeat) VALUES(1,?,?)
                ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,heartbeat=excluded.heartbeat''',(owner,now))
            return True

    def bc_lease_renew(self, owner):
        with self.connection() as c:
            return bool(c.execute('UPDATE tg_bc_runner_lease SET heartbeat=? WHERE id=1 AND owner=?',
                                  (time.time(),owner)).rowcount)

    def bc_lease_release(self, owner):
        with self.connection() as c:
            c.execute('DELETE FROM tg_bc_runner_lease WHERE id=1 AND owner=?',(owner,))


class RateGate:
    """Shared process-wide limiter for all broadcast threads."""
    def __init__(self, per_second=18, clock=time.monotonic, sleeper=time.sleep):
        self.delay=1/max(1,min(int(per_second),25))
        self.clock=clock
        self.sleeper=sleeper
        self.lock=threading.Lock()
        self.next_at=0.
        self.cooldown_until=0.

    def wait(self):
        with self.lock:
            now=self.clock()
            when=max(now,self.next_at,self.cooldown_until)
            if when>now:
                self.sleeper(when-now)
            self.next_at=max(when,self.clock())+self.delay

    def cooldown(self, delay):
        with self.lock:
            self.cooldown_until=max(self.cooldown_until,self.clock()+max(1,delay))
            self.next_at=max(self.next_at,self.cooldown_until)


class BroadcastEngine:
    """Worker threads share rate gate but NOT HTTP sessions."""
    def __init__(self, store, api_factory, *, workers=3, rate=18):
        self.store=store
        self.api_factory=api_factory
        self.workers=max(1,min(int(workers),5))
        self.gate=RateGate(rate)
        self.stop=threading.Event()
        self.threads=[]

    def start(self):
        # Invoked only after Runner verified polling ownership/removed webhook.
        n=self.store.bc_recover_interrupted()
        if n: LOGGER.warning('%s interrupted sends marked uncertain; review report before retry',n)
        rn=self.store.bc_recover_admin_reports()
        if rn: LOGGER.warning('%s interrupted admin reports marked uncertain',rn)
        for i in range(self.workers):
            t=threading.Thread(target=self._loop,name=f'bc-worker-{i}',daemon=True)
            t.start()
            self.threads.append(t)
        reporter=threading.Thread(target=self._report_loop,name='bc-admin-reports',daemon=True)
        reporter.start()
        self.threads.append(reporter)

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=18)

    def process_claim(self,api,row):
        """One confirmed API attempt; no second attempt on ambiguous results."""
        if not self.store.bc_should_send(row['job_id'],row['user_id']):
            return
        uid=row['user_id']
        body=html.escape(row['body'],quote=False)
        try:
            if row['kind']=='text':
                result=api.send(uid,body)
            elif row['kind']=='photo':
                result=api.photo(uid,row['file_id'],caption=body)
            else:
                result=api.document(uid,row['file_id'],caption=body)
        except Exception as exc:
            code=getattr(exc,'code',0)
            desc=str(getattr(exc,'description','')).lower()
            if code==429:
                retry_after=max(2,int(getattr(exc,'retry_after',0) or 10)+1)
                self.gate.cooldown(retry_after)
                # Do not leave exhausted retries stuck forever.
                attempts=self.store.bc_attempts(row['job_id'],uid)
                if attempts>=8:
                    self.store.bc_resolve(row['job_id'],uid,'failed',error='429 retries exhausted')
                else:
                    self.store.bc_resolve(row['job_id'],uid,'retry',retry_after=retry_after,error='429 rate limit')
            elif code==403 and 'blocked' in desc:
                self.store.bc_resolve(row['job_id'],uid,'blocked',error='403 bot blocked')
            elif code in (400,403):
                self.store.bc_resolve(row['job_id'],uid,'unreachable',error=f'{code} invalid/unreachable')
            else:
                # 0: connection ambiguity. 5xx: API may have accepted the message.
                self.store.bc_resolve(row['job_id'],uid,'uncertain',error=f'{code} ambiguous')
        else:
            mid=result.get('message_id') if isinstance(result,dict) else None
            self.store.bc_resolve(row['job_id'],uid,'sent',message_id=mid)

    def _loop(self):
        api=self.api_factory()
        try:
            while not self.stop.is_set():
                try:
                    row=self.store.bc_claim()
                    if not row:
                        self.stop.wait(0.45)
                        continue
                    self.gate.wait()
                    self.process_claim(api,row)
                except Exception:
                    LOGGER.exception('Broadcast worker failure (no token or message logged)')
                    self.stop.wait(1)
        finally:
            with suppress(Exception):
                api.session.close()

    @staticmethod
    def _report_text(stats, milestone):
        v=stats['counts']
        done=sum(v.get(x,0) for x in ('sent','blocked','unreachable','failed','uncertain','skipped'))
        remaining=sum(v.get(x,0) for x in ('pending','retry','sending'))
        title=(f'📊 <b>گزارش خودکار {milestone} مخاطب</b>' if milestone else
               ('✅ <b>ارسال همگانی به پایان رسید</b>' if stats['status']=='completed'
                else '🛑 <b>کمپین لغو شد</b>'))
        return (f'{title}\n\n'
                f'کمپین: <code>#{stats["id"]}</code>\n'
                f'👥 کل مخاطبان: <code>{stats["total"]}</code>\n'
                f'📍 تعیین‌تکلیف‌شده: <code>{done}</code>\n'
                f'✅ پذیرفته‌شده در تلگرام: <code>{v.get("sent",0)}</code>\n'
                f'🚫 ربات را مسدود کرده‌اند: <code>{v.get("blocked",0)}</code>\n'
                f'⛔ غیرقابل‌دسترسی: <code>{v.get("unreachable",0)}</code>\n'
                f'❌ خطای قطعی: <code>{v.get("failed",0)}</code>\n'
                f'⚠️ نتیجه نامشخص: <code>{v.get("uncertain",0)}</code>\n'
                f'⏭ ردشده/لغوشده: <code>{v.get("skipped",0)}</code>\n'
                f'⏳ باقی‌مانده: <code>{remaining}</code>\n\n'
                'پذیرش توسط API به معنی خوانده‌شدن پیام نیست.')

    def process_admin_report(self,api,row):
        stats=self.store.bc_stats(row['job_id'])
        if not stats:
            self.store.bc_resolve_admin_report(row['job_id'],row['milestone'],'failed',error='job missing')
            return
        try:
            result=api.send(row['admin_id'], self._report_text(stats,row['milestone']))
        except Exception as exc:
            code=getattr(exc,'code',0)
            if code==429:
                with self.store.connection() as c:
                    tries=c.execute('SELECT attempts FROM tg_bc_reports WHERE job_id=? AND milestone=?',
                                    (row['job_id'],row['milestone'])).fetchone()
                if tries and tries['attempts']>=8:
                    self.store.bc_resolve_admin_report(row['job_id'],row['milestone'],'failed',error='report 429 limit')
                else:
                    wait=max(2,int(getattr(exc,'retry_after',0) or 10)+1)
                    self.store.bc_resolve_admin_report(row['job_id'],row['milestone'],'retry',retry_after=wait,error='rate limit')
            elif code in (0,500,502,503,504):
                self.store.bc_resolve_admin_report(row['job_id'],row['milestone'],'uncertain',error=f'API {code} ambiguous')
            else:
                self.store.bc_resolve_admin_report(row['job_id'],row['milestone'],'failed',error=f'API {code}')
        else:
            mid=result.get('message_id') if isinstance(result,dict) else None
            self.store.bc_resolve_admin_report(row['job_id'],row['milestone'],'sent',message_id=mid)

    def _report_loop(self):
        api=self.api_factory()
        try:
            while not self.stop.is_set():
                try:
                    self.store.bc_queue_admin_reports()
                    row=self.store.bc_claim_admin_report()
                    if row:
                        self.process_admin_report(api,row)
                    else:
                        self.stop.wait(0.7)
                except Exception:
                    LOGGER.exception('Broadcast reporting failed; retrying')
                    self.stop.wait(2)
        finally:
            with suppress(Exception):api.session.close()
