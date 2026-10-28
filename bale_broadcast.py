"""Crash-aware, SQLite-backed Bale broadcast queue (no Redis, no web panel).

API acceptance != delivery/read. Timed-out sends are classified 'unknown', never
silently retried because an unseen acknowledgement might have been delivered.
"""
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger(__name__)

MAX_TEXT = 3900
MAX_CAPTION = 950
MAX_ATTEMPTS = 5

def _utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')

def _later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S.%f')


def _db(path):
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.execute('PRAGMA busy_timeout=15000')
    conn.row_factory = sqlite3.Row
    return conn


class BroadcastStore:
    def __init__(self, path):
        self.path = str(path)
        self.schema()

    def schema(self):
        with closing(_db(self.path)) as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS bale_broadcast_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('text','photo')),
                content TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft','running','paused','completed','cancelled')),
                total INTEGER NOT NULL DEFAULT 0,
                cooldown_until TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS bale_broadcast_targets (
                campaign_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','sending','retry','sent','blocked','failed','unknown')),
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_at TEXT,
                error_text TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(campaign_id,user_id),
                FOREIGN KEY(campaign_id) REFERENCES bale_broadcast_campaigns(id)
            );
            CREATE INDEX IF NOT EXISTS ix_bale_broadcast_queue
                ON bale_broadcast_targets(status,retry_at,campaign_id);
            CREATE TABLE IF NOT EXISTS bale_broadcast_reports (
                campaign_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','sending','retry','sent','unknown')),
                retry_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(campaign_id,milestone)
            );
            ''')

    def draft(self, admin, kind, content, caption=''):
        if kind not in ('text','photo') or not isinstance(content,str) or not content.strip():
            raise ValueError('empty or invalid broadcast')
        if len(content) > (MAX_TEXT if kind=='text' else 1024):
            raise ValueError('message too long')
        if len(caption)>MAX_CAPTION:raise ValueError('caption too long')
        with closing(_db(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            cur=c.execute('INSERT INTO bale_broadcast_campaigns(admin_id,kind,content,caption) VALUES(?,?,?,?)',
                          (admin,kind,content,caption))
            c.execute('COMMIT')
            return cur.lastrowid

    def get(self, campaign):
        with closing(_db(self.path)) as c:
            return c.execute('SELECT * FROM bale_broadcast_campaigns WHERE id=?',(campaign,)).fetchone()

    def start(self, campaign, admin):
        """Freeze recipients with one INSERT..SELECT; duplicate confirmation is inert."""
        with closing(_db(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            cur=c.execute("UPDATE bale_broadcast_campaigns SET status='running',started_at=? "
                          "WHERE id=? AND admin_id=? AND status='draft'",(_utc(),campaign,admin))
            if not cur.rowcount:
                c.execute('ROLLBACK');return False
            c.execute('''INSERT INTO bale_broadcast_targets(campaign_id,user_id)
                         SELECT ?,u.user_id FROM users u
                         WHERE NOT EXISTS (SELECT 1 FROM admin_blocked_users b WHERE b.user_id=u.user_id)
                         ''',(campaign,))
            cnt=c.execute('SELECT COUNT(*) FROM bale_broadcast_targets WHERE campaign_id=?',(campaign,)).fetchone()[0]
            c.execute('UPDATE bale_broadcast_campaigns SET total=? WHERE id=?',(cnt,campaign))
            c.execute('COMMIT')
        self.reports(campaign)
        return True

    def pause(self, campaign, admin, paused):
        old,new=('running','paused') if paused else ('paused','running')
        with closing(_db(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            changed=c.execute('UPDATE bale_broadcast_campaigns SET status=? WHERE id=? AND admin_id=? AND status=?',
                              (new,campaign,admin,old)).rowcount
            c.execute('COMMIT')
            return bool(changed)

    def cancel_draft(self,campaign,admin):
        with closing(_db(self.path)) as c:
            return bool(c.execute("UPDATE bale_broadcast_campaigns SET status='cancelled' "
                                  "WHERE id=? AND admin_id=? AND status='draft'",(campaign,admin)).rowcount)

    def recover(self):
        """An in-flight request could have reached Bale; never auto-send it twice."""
        with closing(_db(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE bale_broadcast_targets SET status='unknown',error_text='restart during send' WHERE status='sending'")
            c.execute("UPDATE bale_broadcast_reports SET status='unknown' WHERE status='sending'")
            c.execute('COMMIT')
        with closing(_db(self.path)) as c:
            ids=[r[0] for r in c.execute("SELECT id FROM bale_broadcast_campaigns WHERE status='running'")]
        for i in ids:self.reports(i)

    def claim(self):
        with closing(_db(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            now=_utc()
            r=c.execute('''SELECT t.campaign_id,t.user_id,t.attempts,c.kind,c.content,c.caption
              FROM bale_broadcast_targets t JOIN bale_broadcast_campaigns c ON c.id=t.campaign_id
              WHERE c.status='running' AND (c.cooldown_until IS NULL OR c.cooldown_until<=?)
              AND (t.status='pending' OR (t.status='retry' AND t.retry_at<=?))
              ORDER BY t.campaign_id,t.user_id LIMIT 1''',(now,now)).fetchone()
            if r:
                c.execute("UPDATE bale_broadcast_targets SET status='sending',attempts=attempts+1,updated_at=? "
                          "WHERE campaign_id=? AND user_id=? AND status IN ('pending','retry')",(now,r['campaign_id'],r['user_id']))
            c.execute('COMMIT')
            return dict(r) if r else None

    def finish(self,campaign,user,status,error='',wait_seconds=None):
        assert status in ('sent','blocked','failed','unknown','retry')
        with closing(_db(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT attempts FROM bale_broadcast_targets WHERE campaign_id=? AND user_id=? AND status=?',
                        (campaign,user,'sending')).fetchone()
            if not r:
                c.execute('ROLLBACK');return False
            if status=='retry' and r['attempts']>=MAX_ATTEMPTS:status='failed'
            retry_time=_later(max(1,int(wait_seconds or 5))) if status=='retry' else None
            c.execute('''UPDATE bale_broadcast_targets SET status=?,retry_at=?,error_text=?,updated_at=?
                         WHERE campaign_id=? AND user_id=?''',
                      (status,retry_time,str(error)[:180],_utc(),campaign,user))
            if status=='retry' and wait_seconds:
                c.execute('UPDATE bale_broadcast_campaigns SET cooldown_until=? WHERE id=?',
                          (retry_time,campaign))
            c.execute('COMMIT')
        self.reports(campaign)
        return True

    def summary(self,campaign):
        with closing(_db(self.path)) as c:
            camp=c.execute('SELECT * FROM bale_broadcast_campaigns WHERE id=?',(campaign,)).fetchone()
            if not camp:return None
            counts={r['status']:r['n'] for r in c.execute('''SELECT status,COUNT(*) n FROM bale_broadcast_targets
                                                               WHERE campaign_id=? GROUP BY status''',(campaign,))}
            return {'campaign':dict(camp),'counts':counts,
                    'settled':sum(counts.get(s,0) for s in ('sent','blocked','failed','unknown'))}

    def reports(self,campaign):
        """Transactional thresholds: exactly one record per completed 100 and final."""
        with closing(_db(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            camp=c.execute('SELECT * FROM bale_broadcast_campaigns WHERE id=?',(campaign,)).fetchone()
            if not camp or camp['status'] not in ('running','completed'):
                c.execute('COMMIT');return
            counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM bale_broadcast_targets '
                                                       'WHERE campaign_id=? GROUP BY status',(campaign,))}
            settled=sum(counts.get(s,0) for s in ('sent','blocked','failed','unknown'))
            total=camp['total']
            def message(head):
                return (f'{head}\nکمپین: #{campaign}\n'
                        f'پردازش‌شده: {settled}/{total}\n'
                        f'✅ پذیرفته‌شده توسط بله: {counts.get("sent",0)}\n'
                        f'🚫 مسدود: {counts.get("blocked",0)}\n'
                        f'❌ ناموفق: {counts.get("failed",0)}\n'
                        f'⚠️ نتیجه نامشخص: {counts.get("unknown",0)}\n'
                        f'⏳ باقیمانده/تلاش مجدد: {total-settled}')
            for threshold in range(100,settled+1,100):
                c.execute('INSERT OR IGNORE INTO bale_broadcast_reports(campaign_id,milestone,text) VALUES(?,?,?)',
                          (campaign,threshold,message(f'📊 گزارش {threshold} مخاطب')))
            if settled==total and camp['status']=='running':
                c.execute("UPDATE bale_broadcast_campaigns SET status='completed',finished_at=? WHERE id=?",(_utc(),campaign))
            if settled==total:
                c.execute('INSERT OR IGNORE INTO bale_broadcast_reports(campaign_id,milestone,text) VALUES(?,?,?)',
                          (campaign,-1,message('✅ ارسال همگانی به پایان رسید.')))
            c.execute('COMMIT')

    def next_report(self):
        with closing(_db(self.path)) as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('''SELECT r.campaign_id,r.milestone,r.text,c.admin_id FROM bale_broadcast_reports r
                  JOIN bale_broadcast_campaigns c ON c.id=r.campaign_id
                  WHERE r.status='pending' OR (r.status='retry' AND r.retry_at<=?)
                  ORDER BY r.campaign_id,CASE WHEN r.milestone=-1 THEN 999999999 ELSE r.milestone END LIMIT 1''',(_utc(),)).fetchone()
            if r:c.execute("UPDATE bale_broadcast_reports SET status='sending',attempts=attempts+1 "
                           "WHERE campaign_id=? AND milestone=?",(r['campaign_id'],r['milestone']))
            c.execute('COMMIT')
            return dict(r) if r else None

    def report_result(self,campaign,milestone,status,delay=0):
        assert status in ('sent','unknown','retry')
        with closing(_db(self.path)) as c:
            c.execute('UPDATE bale_broadcast_reports SET status=?,retry_at=? WHERE campaign_id=? AND milestone=? '
                      "AND status='sending'",(status,_later(delay) if status=='retry' else None,campaign,milestone))

    def last(self,admin):
        with closing(_db(self.path)) as c:
            r=c.execute('SELECT id FROM bale_broadcast_campaigns WHERE admin_id=? ORDER BY id DESC LIMIT 1',(admin,)).fetchone()
            return r['id'] if r else None


def classify(result):
    """Parse a Telegram-like Bale API response without assuming guaranteed delivery."""
    if not isinstance(result,dict):return ('unknown','no acknowledgement',None)
    if result.get('ok') is True:return ('sent','',None)
    code=result.get('error_code') or result.get('code')
    try:code=int(code) if code is not None else None
    except (ValueError,TypeError):code=None
    desc=str(result.get('description') or result.get('message') or '')
    low=desc.lower()
    if code==429 or 'too many requests' in low or 'rate limit' in low:
        p=result.get('parameters') or {}
        try:delay=max(2,min(int(p.get('retry_after') or result.get('retry_after') or 15),3600))
        except (ValueError,TypeError):delay=15
        return ('retry',desc or '429',delay)
    if 'blocked' in low or 'bot was blocked' in low or 'bot is blocked' in low:
        return ('blocked',desc,None)
    if code in (400,403,404) or 'chat not found' in low or 'user is deactivated' in low:
        return ('failed',desc or str(code),None)
    if code in (500,502,503,504) or 'temporarily' in low:
        return ('retry',desc or 'server error',6)
    # Unknown JSON errors may have been accepted upstream; avoid automatic repeat.
    return ('unknown',desc or 'ambiguous response',None)


class BaleBroadcast:
    def __init__(self,db,send_message,send_photo,rate=None):
        self.db=db
        self.store=BroadcastStore(db.db_name)
        self.send=send_message
        self.photo=send_photo
        raw = rate if rate is not None else os.getenv('BALE_BROADCAST_RATE_PER_SEC','5')
        try:self.rate=min(10.0,max(0.5,float(raw)))
        except (ValueError,TypeError):self.rate=5.0
        self._stop=threading.Event()
        self._wake=threading.Event()
        self._thread=None

    @staticmethod
    def menu(*rows):
        return {'inline_keyboard':[[{'text':label,'callback_data':cb} for label,cb in row] for row in rows]}

    def emit(self,admin,text,keyboard=None):
        return self.send(admin,text,reply_markup=keyboard)

    def home(self,admin):
        if not self.db.is_admin(admin):return False
        last=self.store.last(admin)
        if last:
            s=self.store.summary(last)
            note=f'\nآخرین کمپین: #{last} ({s["campaign"]["status"]})'
        else:note=''
        self.emit(admin,'📢 مرکز ارسال همگانی بله'+note+'\n\n'
                       'پیام متنی یا عکس با کپشن ارسال کنید. پیش از شروع، پیش‌نمایش و تأیید نمایش داده می‌شود.',
                  self.menu([('➕ پیام جدید','bbr:new')],
                            [('📊 گزارش آخرین کمپین','bbr:status')],
                            [('⏸ توقف','bbr:pause'),('▶️ ادامه','bbr:resume')],
                            [('🏠 مدیریت','badmin:home')]))
        return True

    def begin(self,admin):
        if not self.db.is_admin(admin):return False
        self.db.set_user_state(admin,'bbr_waiting')
        self.emit(admin,'لطفاً متن یا عکس همراه کپشن را ارسال کنید.\nبرای انصراف: /cancel')
        return True

    def capture(self,admin,text=None,photo_id=None,caption=''):
        if not self.db.is_admin(admin):return False
        state,_=self.db.get_user_state(admin)
        if state!='bbr_waiting':return False
        if not photo_id and (not isinstance(text,str) or not text.strip()):
            self.emit(admin,'متن خالی است. دوباره ارسال کنید.');return True
        if photo_id and (not isinstance(photo_id,str) or len(photo_id)>1024):
            self.emit(admin,'تصویر معتبر نیست. دوباره ارسال کنید.');return True
        try:
            campaign=self.store.draft(admin,'photo' if photo_id else 'text',photo_id or text,caption if photo_id else '')
        except ValueError:
            self.emit(admin,'طول پیام یا کپشن بیشتر از حد مجاز است. متن کوتاه‌تری بفرستید.');return True
        self.db.set_user_state(admin,'bbr_confirm',{'id':campaign})
        # A failed preview never authorizes a mass send.
        resp=(self.photo(admin,photo_id,caption=caption) if photo_id else self.send(admin,text))
        if not isinstance(resp,dict) or not resp.get('ok'):
            self.store.cancel_draft(campaign,admin)
            self.db.delete_user_state(admin)
            self.emit(admin,'پیش‌نمایش ارسال نشد. لطفاً اتصال بله را بررسی و دوباره تلاش کنید.')
            return True
        self.emit(admin,f'👆 پیش‌نمایش کمپین #{campaign}\n'
                        'بعد از تأیید، ارسال برای کاربران ثبت‌شده به‌طور خودکار آغاز می‌شود.',
                  self.menu([('🚀 تأیید و شروع',f'bbr:start:{campaign}')],
                            [('❌ انصراف',f'bbr:discard:{campaign}')]))
        return True

    def cancel(self,admin):
        state,meta=self.db.get_user_state(admin)
        if state not in ('bbr_waiting','bbr_confirm'):return False
        if state=='bbr_confirm' and meta:self.store.cancel_draft(meta.get('id'),admin)
        self.db.delete_user_state(admin)
        self.emit(admin,'ساخت پیام همگانی لغو شد.')
        return True

    def callback(self,admin,data):
        if not data.startswith('bbr:'):return False
        if not self.db.is_admin(admin):return True
        name=data[4:]
        if name=='new':return self.begin(admin)
        if name=='status':
            last=self.store.last(admin)
            if not last:return self.emit(admin,'هنوز کمپینی ساخته نشده است.') or True
            s=self.store.summary(last)
            ct=s['counts']; total=s['campaign']['total']
            return self.emit(admin,f'📊 گزارش کمپین #{last}\nوضعیت: {s["campaign"]["status"]}\n'
                 f'پردازش‌شده: {s["settled"]}/{total}\n'
                 f'✅ پذیرفته‌شده: {ct.get("sent",0)} | 🚫 مسدود: {ct.get("blocked",0)}\n'
                 f'❌ ناموفق: {ct.get("failed",0)} | ⚠️ نامشخص: {ct.get("unknown",0)}') or True
        if name in ('pause','resume'):
            cid=self.store.last(admin)
            if not cid:return self.emit(admin,'کمپین قابل کنترلی پیدا نشد.') or True
            ok=self.store.pause(cid,admin,name=='pause')
            if ok:self._wake.set()
            return self.emit(admin,'✅ وضعیت کمپین تغییر کرد.' if ok else 'وضعیت فعلی اجازه این عملیات را نمی‌دهد.') or True
        if ':' not in name:return True
        action,raw=name.split(':',1)
        if action not in ('start','discard') or not (0 < len(raw) <= 18) or not raw.isascii() or not raw.isdigit():return True
        cid=int(raw)
        state,meta=self.db.get_user_state(admin)
        if state!='bbr_confirm' or not meta or meta.get('id')!=cid:return True
        self.db.delete_user_state(admin)
        if action=='discard':self.store.cancel_draft(cid,admin);return self.emit(admin,'پیام همگانی لغو شد.') or True
        ok=self.store.start(cid,admin)
        if not ok:return self.emit(admin,'این کمپین قبلاً شروع یا لغو شده است.') or True
        s=self.store.summary(cid)
        self._wake.set()
        self.emit(admin,f'🚀 کمپین #{cid} آغاز شد.\nمخاطبان: {s["campaign"]["total"]}\n'
                        'ارسال خودکار ادامه پیدا می‌کند. پس از هر 100 مخاطب گزارش دریافت می‌کنید.')
        return True

    def process_one(self):
        """Do one customer send or one pending admin report, for tests or worker."""
        # Prefer already queued reports when available.
        report=self.store.next_report()
        if report:
            try:result=self.send(report['admin_id'],report['text'])
            except Exception:result=None
            status,_,delay=classify(result)
            final='sent' if status=='sent' else ('retry' if status=='retry' else 'unknown')
            self.store.report_result(report['campaign_id'],report['milestone'],final,delay or 5)
            return 'report'
        row=self.store.claim()
        if not row:return None
        try:
            result=(self.photo(row['user_id'],row['content'],caption=row['caption']) if row['kind']=='photo'
                    else self.send(row['user_id'],row['content']))
        except Exception as e:
            LOG.warning('Bale broadcast transport: %s',type(e).__name__)
            result=None
        status,err,delay=classify(result)
        self.store.finish(row['campaign_id'],row['user_id'],status,err,delay)
        return status

    def _run(self):
        try:self.store.recover()
        except Exception:LOG.exception('Bale broadcast recovery')
        next_send=time.monotonic()
        while not self._stop.is_set():
            try:
                now=time.monotonic()
                if now<next_send:
                    self._stop.wait(min(0.2,next_send-now));continue
                result=self.process_one()
                if result:
                    next_send=time.monotonic()+(1.0/self.rate)
                else:
                    self._wake.wait(0.5);self._wake.clear()
            except Exception:
                LOG.exception('Bale broadcast worker error')
                self._stop.wait(1)

    def start_worker(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear()
        self._thread=threading.Thread(target=self._run,name='bale-broadcast-worker',daemon=True)
        self._thread.start()

    def stop_worker(self):
        self._stop.set();self._wake.set()
        if self._thread:self._thread.join(timeout=2)
