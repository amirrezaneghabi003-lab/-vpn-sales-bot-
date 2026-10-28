"""Bale admin menu and guarded actions; SQLite only, no external dependencies."""
import sqlite3
from contextlib import closing
from customer_experience import order_ref, money, jalali_datetime

class BaleAdmin:
    def __init__(self,db,send,photo=None):
        self.db,self.send,self.photo=db,send,photo
        with closing(sqlite3.connect(db.db_name,timeout=10)) as c:
            c.execute('CREATE TABLE IF NOT EXISTS bale_activity (user_id INTEGER PRIMARY KEY,last_seen TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS bale_admin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT,admin_id INTEGER NOT NULL,target_id INTEGER NOT NULL,action TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
            c.commit()

    def conn(self):return sqlite3.connect(self.db.db_name,timeout=10)
    @staticmethod
    def menu(*rows):return {'inline_keyboard':[[{'text':t,'callback_data':cb} for t,cb in row] for row in rows]}
    def emit(self,u,text,keyboard=None):return self.send(u,text,reply_markup=keyboard)
    def touch(self,u):
        with closing(self.conn()) as c:
            c.execute('INSERT OR IGNORE INTO bale_activity(user_id,last_seen) VALUES(?,CURRENT_TIMESTAMP)',(u,))
            c.execute("UPDATE bale_activity SET last_seen=CURRENT_TIMESTAMP WHERE user_id=? AND last_seen < datetime('now','-5 minutes')",(u,))
            c.commit()
    def audit(self,admin,target,action):
        with closing(self.conn()) as c:
            c.execute('INSERT INTO bale_admin_audit(admin_id,target_id,action) VALUES(?,?,?)',(admin,target,action));c.commit()
    def home(self,u):
        if not self.db.is_admin(u):return False
        self.db.delete_user_state(u)
        self.emit(u,'🛡 مرکز مدیریت | اینترنت آزاد پرو\n\nبخش موردنظر را انتخاب کنید.',self.menu(
            [('👥 کاربران','badmin:users'),('📦 سفارش‌ها','badmin:orders')],
            [('💬 پیام خصوصی','badmin:dm'),('📊 آمار','badmin:stats')],
            [('🛍 مدیریت سرویس‌ها','badmin:catalog')],
            [('📢 ارسال همگانی','badmin:broadcast')]))
        return True
    def lookup(self,term):
        term=term.strip().lstrip('@')[:80]
        if not term:return []
        with closing(self.conn()) as c:
            if term.isdigit():
                return c.execute('SELECT user_id,first_name,last_name,username FROM users WHERE user_id=? LIMIT 10',(int(term),)).fetchall()
            term=term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            v='%'+term+'%'
            return c.execute("""SELECT user_id,first_name,last_name,username FROM users
                WHERE COALESCE(first_name,'') LIKE ? ESCAPE '\\' OR COALESCE(last_name,'') LIKE ? ESCAPE '\\'
                OR COALESCE(username,'') LIKE ? ESCAPE '\\' ORDER BY joined_at DESC LIMIT 10""",(v,v,v)).fetchall()
    def user(self,u,target):
        if not self.db.is_admin(u):return False
        info=self.db.get_user_info(target,use_cache=False)
        if not info:return self.emit(u,'کاربر در ربات بله یافت نشد.')
        with closing(self.conn()) as c:
            all_count,ok,pending=c.execute("SELECT COUNT(*),SUM(status='approved'),SUM(status='pending') FROM orders WHERE user_id=?",(target,)).fetchone()
            last=c.execute('SELECT last_seen FROM bale_activity WHERE user_id=?',(target,)).fetchone()
        blocked=self.db.is_user_blocked_by_admin(target)
        text=(f'👤 پرونده کاربر\n\nشناسه: {target}\n'
              f'نام: {info.get("first_name") or ""} {info.get("last_name") or ""}\n'
              f'نام کاربری: {info.get("username") or "ندارد"}\n'
              f'سفارش‌ها: {all_count} | تأیید: {ok or 0} | در انتظار: {pending or 0}\n'
              f'آخرین فعالیت: {jalali_datetime(last[0]) if last else "ثبت نشده"}\n'
              f'وضعیت: {"مسدود" if blocked else "مجاز"}')
        actions=[('💬 ارسال پیام',f'badmin:compose:{target}')]
        if not self.db.is_admin(target):
            actions.append(('✅ رفع مسدودی' if blocked else '🚫 مسدودسازی',f'badmin:blockask:{target}'))
        return self.emit(u,text,self.menu(actions,[('🔍 جست‌وجو','badmin:users'),('🏠 مدیریت','badmin:home')]))
    def order(self,u,oid):
        if not self.db.is_admin(u):return False
        order=self.db.get_order_by_id(oid)
        if not order:return self.emit(u,'سفارش یافت نشد.')
        txt=(f'📦 سفارش {order_ref(oid)}\n'
             f'کاربر: {order[1]}\nسرویس: {self.db.order_title(oid,order[2])}\n'
             f'مبلغ: {money(order[3])} تومان\nوضعیت: {order[4]}\n'
             f'تاریخ: {jalali_datetime(order[5])}\nرسید: {"دارد" if order[6] else "ندارد"}')
        rows=[]
        if order[4]=='pending' and order[6]:
            rows.append([('✅ تأیید',f'approve_{oid}'),('❌ رد',f'badmin:rejectask:{oid}')])
        rows.extend([[('👤 کاربر',f'badmin:user:{order[1]}')],[('🏠 مدیریت','badmin:home')]])
        self.emit(u,txt,self.menu(*rows))
        if order[6] and self.photo:self.photo(u,order[6],caption=f'رسید {order_ref(oid)}')
        return True
    def callback(self,u,data):
        if not data.startswith('badmin:'):return False
        if not self.db.is_admin(u):return True
        name=data[7:]
        if name=='home':return self.home(u)
        if name in ('users','orders','dm'):
            states={'users':'badmin_search_user','orders':'badmin_search_order','dm':'badmin_search_dm'}
            self.db.set_user_state(u,states[name])
            ask={'users':'نام، یوزرنیم یا شناسه کاربر را وارد کنید.',
                 'orders':'شماره سفارش مانند pro121 را بفرستید.',
                 'dm':'نام یا شناسه کاربر گیرنده را بفرستید.'}[name]
            return self.emit(u,'🔍 '+ask+'\nلغو: /cancel',self.menu([('🏠 مدیریت','badmin:home')]))
        if name=='stats':
            s=self.db.get_stats()
            with closing(self.conn()) as c:
                a7=c.execute("SELECT COUNT(*) FROM bale_activity WHERE last_seen>=datetime('now','-7 days')").fetchone()[0]
                a30=c.execute("SELECT COUNT(*) FROM bale_activity WHERE last_seen>=datetime('now','-30 days')").fetchone()[0]
                waiting=c.execute("SELECT COUNT(*) FROM orders WHERE status='pending' AND receipt_photo IS NOT NULL").fetchone()[0]
            return self.emit(u,f'📊 آمار ربات بله\n👥 کل کاربران: {s["total_users"]}\n'
                f'🟢 فعال 7 روز: {a7} | فعال 30 روز: {a30}\n'
                f'📦 سفارش‌ها: {s["total_orders"]} | تأیید: {s["approved_orders"]}\n'
                f'⏳ رسیدهای منتظر: {waiting}\n💰 فروش تأییدشده: {s["total_revenue"]:,} تومان',
                self.menu([('🏠 مدیریت','badmin:home')]))
        if name=='catalog':
            return self.emit(u,'مدیریت سرویس‌ها و دسته‌ها:',self.menu(
                [('📦 مشاهده سرویس‌ها','adm:catalog')],[('🏠 مدیریت','badmin:home')]))
        if name=='broadcast':
            controller=getattr(self,'broadcast_controller',None)
            if controller is None:
                return self.emit(u,'بخش ارسال همگانی هنوز راه‌اندازی نشده است.')
            return controller.home(u)
        if ':' not in name:return True
        action,raw=name.split(':',1)
        if not raw.isdigit():return True
        target=int(raw)
        if not 0 < target < 9223372036854775808:return True
        if action=='user':return self.user(u,target)
        if action=='order':return self.order(u,target)
        if action=='compose':
            if not self.db.get_user_info(target,use_cache=False):return True
            self.db.set_user_state(u,'badmin_compose',{'target':target})
            return self.emit(u,f'متن پیام خصوصی برای {target} را بفرستید (حداکثر 3000 کاراکتر).')
        if action=='blockask':
            if self.db.is_admin(target) or not self.db.get_user_info(target,use_cache=False):return True
            self.db.set_user_state(u,'badmin_confirm_block',{'target':target})
            return self.emit(u,f'تغییر وضعیت مسدودی {target} را تأیید می‌کنید؟',self.menu(
                [('✅ تأیید',f'badmin:blockyes:{target}'),('انصراف','badmin:home')]))
        if action=='blockyes':
            st,meta=self.db.get_user_state(u)
            if st!='badmin_confirm_block' or not meta or meta.get('target')!=target or self.db.is_admin(target):return True
            self.db.delete_user_state(u)
            if self.db.is_user_blocked_by_admin(target):
                self.db.remove_admin_block(target);a='unblock'
            else:self.db.add_admin_block(target);a='block'
            self.audit(u,target,a)
            self.emit(u,'✅ وضعیت کاربر تغییر کرد.');return self.user(u,target)
        if action=='rejectask':
            order=self.db.get_order_by_id(target)
            if not order or order[4]!='pending' or not order[6]:return True
            self.db.set_user_state(u,'badmin_confirm_reject',{'order_id':target})
            return self.emit(u,f'آیا سفارش {order_ref(target)} رد شود؟',self.menu(
                [('✅ تأیید رد',f'badmin:rejectyes:{target}'),('انصراف','badmin:home')]))
        if action=='rejectyes':
            st,meta=self.db.get_user_state(u)
            if st!='badmin_confirm_reject' or not meta or meta.get('order_id')!=target:return True
            self.db.delete_user_state(u)
            ok=self.db.reject_order(target)
            if ok:
                self.audit(u,target,'reject')
                order=self.db.get_order_by_id(target)
                self.send(order[1],'رسید پرداخت تأیید نشد. برای پیگیری با پشتیبانی @amiir_vip12 ارتباط بگیرید.')
            return self.emit(u,'✅ سفارش رد شد.' if ok else 'سفارش قبلاً بررسی شده است.')
        if action=='dmconfirm':
            st,meta=self.db.get_user_state(u)
            if st!='badmin_confirm_dm' or not meta or meta.get('target')!=target:return True
            self.db.delete_user_state(u)
            if self.db.is_user_blocked_by_admin(target):return self.emit(u,'امکان ارسال به کاربر مسدودشده نیست.')
            result=self.send(target,'📩 پیام پشتیبانی اینترنت آزاد پرو\n\n'+meta['text'])
            if result and result.get('ok'):
                self.audit(u,target,'private_message')
                return self.emit(u,'✅ ارسال از طرف API بله پذیرفته شد.')
            return self.emit(u,'⚠️ پیام ارسال نشد؛ وضعیت کاربر را بررسی کنید.')
        return True
    def text(self,u,text):
        if not self.db.is_admin(u):return False
        state,data=self.db.get_user_state(u)
        if not state or not state.startswith('badmin_'):return False
        if text=='/cancel':self.home(u);return True
        if state in ('badmin_search_user','badmin_search_dm'):
            rows=self.lookup(text)
            self.db.delete_user_state(u)
            if not rows:return self.emit(u,'موردی پیدا نشد. دوباره از منو جست‌وجو کنید.') or True
            if state=='badmin_search_user' and len(rows)==1:self.user(u,rows[0][0]);return True
            buttons=[[(f'👤 {uid} · {(fn or "")[:14]}',f'badmin:{"compose" if state=="badmin_search_dm" else "user"}:{uid}')] for uid,fn,ln,un in rows]
            buttons.append([('🏠 مدیریت','badmin:home')])
            self.emit(u,'نتایج جست‌وجو:',self.menu(*buttons));return True
        if state=='badmin_search_order':
            self.db.delete_user_state(u)
            raw=text.strip().lower()
            if raw.startswith('pro12'):raw=raw[5:]
            if not raw.isdigit():self.emit(u,'شماره سفارش معتبر نیست.');return True
            self.order(u,int(raw));return True
        if state=='badmin_compose':
            if not data or not isinstance(data.get('target'),int) or not text.strip() or len(text)>3000:
                self.emit(u,'متن باید بین 1 و 3000 نویسه باشد.');return True
            target=data['target']
            self.db.set_user_state(u,'badmin_confirm_dm',{'target':target,'text':text})
            self.emit(u,f'پیش‌نمایش پیام برای {target}:\n\n{text}\n\nارسال شود؟',self.menu(
                [('✅ ارسال',f'badmin:dmconfirm:{target}'),('انصراف','badmin:home')]))
            return True
        return True
