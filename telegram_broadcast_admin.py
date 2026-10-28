"""Inline Telegram admin UI for durable announcements; admin authorization on every action."""
import html


def kb(*rows):
    return {'inline_keyboard': [[{'text': label, 'callback_data': data} for label,data in row] for row in rows]}


def esc(s):
    return html.escape(str(s or ''),quote=False)


class BroadcastAdminMixin:
    def bc_panel(self, admin_id, callback_message=None):
        if not self.is_admin(admin_id): return False
        recent=self.db.bc_last_job()
        rows=[[('🛡 اطلاعیه ضروری سرویس','adm:bc:begin:service')],
              [('📣 پیام تبلیغاتی / پیشنهاد ویژه','adm:bc:begin')]]
        if recent:
            rows.append([('📊 آخرین کمپین',f'adm:bc:stats:{recent}')])
        rows.append([('🔙 مرکز مدیریت','adm:home')])
        self.screen(admin_id, '📢 <b>ارسال همگانی تلگرام</b>\n\n'
                    'پیام متنی یا تصویر همراه کپشن بفرستید.\n'
                    'ابتدا پیش‌نمایش و ارسال آزمایشی انجام می‌شود؛ سپس با تأیید شما، ارسال آغاز خواهد شد.\n\n'
                    '🔒 پیام‌ها فقط برای کاربران مجازِ همین ربات تلگرام ارسال می‌شوند.\n'
                    '🚦 پس از شروع، ارسال خودکار ادامه پیدا می‌کند.\n'
                    '📊 گزارش هر 100 مخاطب و گزارش نهایی خودکار به مدیر فرستاده می‌شود.\n'
                    'اطلاعیه ضروری مختص مسائل واقعی سرویس است؛ تبلیغات از ترجیحات دریافت پیام پیروی می‌کند.',kb(*rows),callback_message)
        return True

    def bc_begin(self, admin_id, callback_message=None, category='marketing'):
        if not self.is_admin(admin_id): return False
        self.db.bc_clear_draft(admin_id)
        self.db.set_state(admin_id,'bc_compose',category)
        self.screen(admin_id,'✍️ <b>پیام کمپین جدید</b>\n\n'
                    'متن یا یک عکس بفرستید. می‌توانید برای عکس، کپشن هم بنویسید.\n'+
                    ('🛡 این پیام درباره وضعیت ضروری سرویس است.\n' if category=='service' else '📣 این پیام تبلیغاتی است.\n')+
                    'حداکثر متن 3900 و کپشن عکس 850 نویسه است.\n\n'
                    'برای بازگشت /cancel را ارسال کنید.',kb([('❌ لغو ساخت','adm:bc:abort')]),callback_message)
        return True

    def bc_handle_message(self, admin_id, message):
        if not self.is_admin(admin_id): return False
        state,category=self.db.get_state(admin_id)
        if state!='bc_compose': return False
        category=category if category in ('service','marketing') else 'marketing'
        raw=(message.get('text') or '').strip()
        if raw.startswith('/'): return False
        photos=message.get('photo') or []
        doc=message.get('document') or {}
        if photos:
            kind,file_id,body='photo',photos[-1]['file_id'],(message.get('caption') or '').strip()
        elif doc and str(doc.get('mime_type','')).startswith('image/'):
            kind,file_id,body='document',doc.get('file_id'),(message.get('caption') or '').strip()
        elif raw:
            kind,file_id,body='text',None,raw
        else:
            self.safe_send(admin_id,'لطفاً یک پیام متنی یا یک عکس ارسال کنید.')
            return True
        if (kind=='text' and not 1<=len(body)<=3900) or (kind!='text' and len(body)>850):
            self.safe_send(admin_id,'متن بیش از حد طولانی است. لطفاً متن کوتاه‌تری ارسال کنید.')
            return True
        self.db.bc_save_draft(admin_id,kind,body,file_id,category=category)
        self.db.clear_state(admin_id)
        self.bc_preview(admin_id)
        return True

    def bc_preview(self, admin_id):
        if not self.is_admin(admin_id): return False
        draft=self.db.bc_draft(admin_id)
        if not draft:
            return self.bc_panel(admin_id)
        self.safe_send(admin_id,'👁 <b>پیش‌نمایش پیام برای مخاطبان:</b>')
        self.bc_send_payload(self.api,admin_id,draft)
        self.safe_send(admin_id,'این نمونه فقط در گفت‌وگوی مدیر نمایش داده شد.\n'
            'برای اطمینان از نمایش، «ارسال آزمایشی» را بزنید، سپس کمپین را آماده کنید.',
            kb([('🧪 ارسال آزمایشی به من','adm:bc:test')],
               [('✅ آماده‌سازی و مشاهده تعداد','adm:bc:prepare')],
               [('✏️ تغییر پیام','adm:bc:begin'),('❌ انصراف','adm:bc:abort')]))
        return True

    @staticmethod
    def bc_send_payload(api,uid,draft):
        body=esc(draft['body'])
        if draft['kind']=='photo': return api.photo(uid,draft['file_id'],caption=body)
        if draft['kind']=='document': return api.document(uid,draft['file_id'],caption=body)
        return api.send(uid,body)

    def bc_report(self, admin_id, job_id, callback_message=None):
        if not self.is_admin(admin_id): return False
        stats=self.db.bc_stats(job_id)
        if not stats:
            return self.safe_send(admin_id,'کمپین موردنظر پیدا نشد.')
        v=stats['counts'];total=stats['total']
        sent=v.get('sent',0);blocked=v.get('blocked',0)
        remaining=sum(v.get(x,0) for x in ('pending','retry','sending'))
        unknown=v.get('uncertain',0)
        reports=self.db.bc_report_statuses(job_id)
        report_problem=sum(x['status'] in ('uncertain','failed') for x in reports)
        text=(f'📊 <b>گزارش کمپین #{job_id}</b>\n\n'
              f'وضعیت: <b>{esc(stats["status"])}</b>\n'
              f'👥 مخاطبان واجد شرایط هنگام ایجاد: <code>{total}</code>\n'
              f'✅ پذیرفته‌شده توسط Telegram API: <code>{sent}</code>\n'
              f'🚫 مسدودکرده‌اند: <code>{blocked}</code>\n'
              f'⛔ غیرقابل‌دسترسی: <code>{v.get("unreachable",0)}</code>\n'
              f'❌ خطای قطعی: <code>{v.get("failed",0)}</code>\n'
              f'⚠️ نتیجه نامشخص: <code>{unknown}</code>\n'
              f'⏳ باقیمانده / تلاش مجدد: <code>{remaining}</code>\n'
              f'⏭ لغوشده: <code>{v.get("skipped",0)}</code>\n\n'
              f'📬 گزارش‌های خودکار ناموفق/نامشخص: <code>{report_problem}</code>\n'
              'پس از هر 100 مخاطب پردازش‌شده و بعد از پایان ارسال، گزارش خودکار می‌رسد.\n'
              'تأیید API به معنای خوانده‌شدن پیام توسط کاربر نیست.')
        rows=[[('🔄 تازه‌سازی گزارش',f'adm:bc:stats:{job_id}')]]
        if stats['status']=='prepared':
            rows.append([('🚀 شروع ارسال',f'adm:bc:go:{job_id}')])
        elif stats['status']=='running':
            rows.append([('⏸ توقف موقت',f'adm:bc:pause:{job_id}')])
        elif stats['status']=='paused':
            rows.append([('▶️ ادامه ارسال',f'adm:bc:resume:{job_id}')])
        if stats['status'] in ('prepared','running','paused'):
            rows.append([('🛑 لغو کمپین',f'adm:bc:cancelask:{job_id}')])
        rows.append([('🔙 پیام همگانی','adm:bc')])
        self.screen(admin_id,text,kb(*rows),callback_message)
        return True

    def bc_callback(self,admin_id,data,callback_message=None):
        if not self.is_admin(admin_id): return False
        if data=='adm:bc':return self.bc_panel(admin_id,callback_message)
        if data=='adm:bc:begin':return self.bc_begin(admin_id,callback_message)
        if data=='adm:bc:begin:service':return self.bc_begin(admin_id,callback_message,category='service')
        if data=='adm:bc:abort':
            self.db.clear_state(admin_id)
            self.db.bc_clear_draft(admin_id)
            return self.bc_panel(admin_id,callback_message)
        if data=='adm:bc:test':
            d=self.db.bc_draft(admin_id)
            if not d:return self.bc_panel(admin_id,callback_message)
            try:self.bc_send_payload(self.api,admin_id,d)
            except Exception:return self.safe_send(admin_id,'ارسال آزمایشی ناموفق بود. اتصال و قالب پیام را بررسی کنید.')
            return self.safe_send(admin_id,'✅ پیام آزمایشی به حساب مدیر ارسال شد.\n'
                       'می‌توانید کمپین را آماده کنید.',kb([('✅ آماده‌سازی کمپین','adm:bc:prepare')],[('🔙 ارسال همگانی','adm:bc')]))
        if data=='adm:bc:prepare':
            job=self.db.bc_create_job(admin_id,self.admin_ids)
            return self.bc_report(admin_id,job,callback_message) if job else self.bc_panel(admin_id,callback_message)
        bits=data.split(':')
        if len(bits)==4 and bits[0:2]==['adm','bc'] and bits[3].isdigit():
            op,jid=bits[2],int(bits[3])
            job=self.db.bc_job(jid)
            if not job: return self.safe_send(admin_id,'کمپین یافت نشد.')
            if op=='stats':return self.bc_report(admin_id,jid,callback_message)
            if op=='cancelask':
                return self.screen(admin_id,f'⚠️ کمپین #{jid} لغو شود؟ مخاطبانی که قبلاً پیام گرفته‌اند، پیام را حفظ می‌کنند.',
                       kb([('بله، لغو کامل',f'adm:bc:canceljob:{jid}')],[('خیر، بازگشت',f'adm:bc:stats:{jid}')]),callback_message)
            changed={'go':'running','resume':'running','pause':'paused','canceljob':'cancelled'}.get(op)
            if changed:
                result=self.db.bc_change_job(jid,changed)
                if result:self.db.admin_audit(admin_id,'broadcast_'+op,jid)
                return self.bc_report(admin_id,jid,callback_message)
        return True
