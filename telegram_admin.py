"""Private Telegram admin interface; no dependencies, no network calls on import."""
from __future__ import annotations

import html
import re

from customer_experience import order_ref, jalali_datetime
from service_catalog import PLANS
from telegram_broadcast_admin import BroadcastAdminMixin
from telegram_catalog_admin import CatalogAdminMixin


def safe(value):
    return html.escape(str(value or ''), quote=False)


def keyboard(*rows):
    return {'inline_keyboard': [[{'text': title, 'callback_data': cmd} for title, cmd in row] for row in rows]}


class AdminPanelMixin(CatalogAdminMixin, BroadcastAdminMixin):
    """Methods assume a storefront exposing db, screen, safe_send, is_admin and API."""

    def admin_home(self, chat_id, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        text = ('🛡 <b>مرکز مدیریت | اینترنت آزاد پرو</b>\n\n'
                'مدیریت کاربران، سفارش‌ها و پیام‌های خصوصی.\n'
                'اطلاعات این صفحه مربوط به دیتابیس همین ربات است.')
        self.screen(chat_id, text, keyboard(
            [('👥 جست‌وجوی کاربر', 'adm:finduser'), ('📦 جست‌وجوی سفارش', 'adm:findorder')],
            [('⏳ رسیدهای در انتظار', 'adm:pending'), ('📊 آمار', 'adm:stats')],
            [('💬 ارسال پیام خصوصی', 'adm:dmchoose')],
            [('📢 ارسال همگانی', 'adm:bc')],
            [('📦 مدیریت سرویس‌ها و قیمت‌ها', 'adm:catalog')],
            [('🏠 صفحه اصلی', 'home')]), callback_message)
        return True

    def admin_stats_page(self, chat_id, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        s = self.db.detailed_admin_stats()
        text = (f'📊 <b>آمار تلگرام</b>\n\n'
                f'👥 کل کاربران: <code>{s["users"]}</code>\n'
                f'🟢 فعال در 7 روز: <code>{s["active_7"]}</code>\n'
                f'🟢 فعال در 30 روز: <code>{s["active_30"]}</code>\n'
                f'🚫 مسدودشده توسط ادمین: <code>{s["blocked"]}</code>\n\n'
                f'📦 کل سفارش‌ها: <code>{s["orders"]}</code>\n'
                f'⏳ در انتظار رسیدگی: <code>{s["review"]}</code>\n'
                f'✅ تأییدشده: <code>{s["approved"]}</code>\n'
                f'💰 فروش تأییدشده: <code>{s["revenue"]:,}</code> تومان\n\n'
                'فعال یعنی کاربری که در بازه مربوط با ربات تعامل داشته است؛ نه آنلاین بودن.')
        self.screen(chat_id, text, keyboard([('🔄 تازه‌سازی', 'adm:stats')], [('🔙 مدیریت', 'adm:home')]), callback_message)
        return True

    def admin_find_prompt(self, chat_id, kind, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        state = {'user': 'adm_finduser', 'order': 'adm_findorder', 'dm': 'adm_finddm'}[kind]
        self.db.set_state(chat_id, state)
        prompt = {
            'user': '👥 نام، یوزرنیم یا شناسه عددی کاربر را بفرستید.',
            'order': '📦 شناسه سفارش (مانند pro12128 یا 128) را ارسال کنید.',
            'dm': '💬 شناسه عددی یا یوزرنیم کاربری که می‌خواهید به او پیام دهید را بفرستید.',
        }[kind]
        self.screen(chat_id, prompt + '\nبرای لغو /cancel را بفرستید.', keyboard([('🔙 مدیریت', 'adm:home')]), callback_message)
        return True

    def admin_find_user_results(self, chat_id, term, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        users = self.db.search_users(term, limit=12)
        if not users:
            self.screen(chat_id, '🔎 کاربری پیدا نشد؛ شناسه یا نام دیگری وارد کنید.',
                        keyboard([('🔍 جست‌وجوی دوباره', 'adm:finduser')], [('🔙 مدیریت', 'adm:home')]), callback_message)
            return True
        rows = [[(f"👤 {u['first_name'] or u['username'] or u['user_id']} · {u['user_id']}",
                   f"adm:user:{u['user_id']}")] for u in users]
        rows.append([('🔍 جست‌وجوی دوباره', 'adm:finduser')])
        rows.append([('🔙 مدیریت', 'adm:home')])
        self.screen(chat_id, f'🔎 <b>نتیجه جست‌وجو</b>\nتعداد نتیجه: {len(users)} (حداکثر 12 مورد)', keyboard(*rows), callback_message)
        return True

    def admin_user_page(self, chat_id, target_id, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        user = self.db.get_user(target_id)
        if not user:
            self.screen(chat_id, 'کاربر موردنظر در تلگرام این ربات ثبت نشده است.',
                        keyboard([('🔎 جست‌وجو', 'adm:finduser')], [('🔙 مدیریت', 'adm:home')]), callback_message)
            return True
        stats = self.db.user_stats(target_id)
        blocked = self.db.admin_is_blocked(target_id)
        text = (f'👤 <b>پروفایل کاربر</b>\n\n'
                f'نام: {safe(user["first_name"])}\n'
                f'یوزرنیم: {safe("@"+user["username"] if user["username"] else "—")}\n'
                f'شناسه: <code>{target_id}</code>\n'
                f'آخرین فعالیت: {safe(jalali_datetime(user["last_seen"]))}\n'
                f'سفارش‌ها: <code>{stats["total"]}</code> | تأیید: <code>{stats["approved"]}</code>\n'
                f'دسترسی: {"🚫 مسدود" if blocked else "✅ فعال"}')
        control = [('✅ رفع مسدودی', f'adm:unblock:{target_id}')] if blocked else [('🚫 مسدودکردن', f'adm:blockask:{target_id}')]
        self.screen(chat_id, text, keyboard(
            [('💬 پیام خصوصی', f'adm:dm:{target_id}')],
            [('📦 سفارش‌های کاربر', f'adm:userorders:{target_id}')], control,
            [('🔙 جست‌وجوی کاربران', 'adm:finduser')], [('🏠 مدیریت', 'adm:home')]), callback_message)
        return True

    def admin_order_search(self, chat_id, term, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        term = term.lower().strip()
        raw = term[5:] if term.startswith('pro12') else term
        if not raw.isdigit() or len(raw) > 12:
            return self.screen(chat_id, 'شناسه سفارش معتبر نیست. نمونه: <code>pro12128</code>',
                               keyboard([('🔍 تلاش دوباره', 'adm:findorder')], [('🔙 مدیریت', 'adm:home')]), callback_message)
        return self.admin_order_page(chat_id, int(raw), callback_message)

    def admin_order_page(self, chat_id, order_id, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        order = self.db.get_order(order_id)
        if not order:
            return self.screen(chat_id, 'این سفارش در تلگرام پیدا نشد.',
                               keyboard([('🔍 جست‌وجو', 'adm:findorder')], [('🔙 مدیریت', 'adm:home')]), callback_message)
        plan = PLANS.get(order['plan_id'], {})
        state = {'awaiting_receipt': 'منتظر رسید', 'under_review': 'در انتظار بررسی',
                 'approved': 'تأییدشده', 'rejected': 'ردشده', 'cancelled': 'لغوشده'}
        text = (f'📦 <b>جزئیات سفارش {order_ref(order_id)}</b>\n\n'
                f'👤 کاربر: <code>{order["user_id"]}</code>\n'
                f'📦 سرویس: {safe(self.db.order_title(order_id,order["plan_id"]))}\n'
                f'💰 مبلغ: <code>{order["amount"]:,}</code> تومان\n'
                f'📊 وضعیت: {state.get(order["status"], "نامشخص")}\n'
                f'📅 ثبت: {safe(jalali_datetime(order["created_at"]))}')
        rows = []
        if order['status'] == 'under_review':
            rows.append([('🧾 نمایش رسید', f'adm:receipt:{order_id}')])
            rows.append([('✅ تأیید با لینک', f'admin:approve:{order_id}'),
                         ('❌ رد رسید', f'adm:rejectask:{order_id}')])
        if order['status'] == 'approved' and order.get('service_link'):
            rows.append([('🔁 ارسال مجدد لینک', f'adm:resend:{order_id}')])
        rows.extend([[('👤 مشاهده کاربر', f'adm:user:{order["user_id"]}')], [('🔙 مدیریت', 'adm:home')]])
        self.screen(chat_id, text, keyboard(*rows), callback_message)
        return True

    def admin_user_orders(self, chat_id, target_id, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        rows = [[(f'{order_ref(o["id"])} · {o["status"]}', f'adm:order:{o["id"]}')]
                for o in self.db.user_orders(target_id, limit=10)]
        rows.append([('🔙 پروفایل', f'adm:user:{target_id}')])
        self.screen(chat_id, f'📦 سفارش‌های کاربر <code>{target_id}</code>', keyboard(*rows), callback_message)
        return True

    def admin_dm_start(self, chat_id, target_id, callback_message=None):
        if not self.is_admin(chat_id):
            return False
        if not self.db.get_user(target_id):
            return self.safe_send(chat_id, 'این کاربر در ربات تلگرام ثبت نشده است.')
        if self.db.admin_is_blocked(target_id):
            return self.safe_send(chat_id, 'کاربر مسدود است؛ برای ارسال پیام ابتدا رفع مسدودی کنید.')
        self.db.set_state(chat_id, 'adm_message', target_id)
        self.screen(chat_id, f'💬 متن پیام خصوصی به کاربر <code>{target_id}</code> را ارسال کنید.\n'
                            'حداکثر 2000 نویسه؛ برای لغو /cancel.',
                    keyboard([('🔙 پروفایل', f'adm:user:{target_id}')]), callback_message)
        return True

    def admin_handle_text(self, chat_id, content):
        """Consume only admin state; admin authorization is rechecked on every input."""
        if not self.is_admin(chat_id):
            return False
        if self.catalog_text(chat_id,content):
            return True
        state, value = self.db.get_state(chat_id)
        if state in ('adm_finduser', 'adm_finddm'):
            self.db.clear_state(chat_id)
            self.admin_find_user_results(chat_id, content)
            if state == 'adm_finddm':
                self.safe_send(chat_id, 'برای ارسال پیام خصوصی، پروفایل کاربر را باز کرده و «پیام خصوصی» را بزنید.')
            return True
        if state == 'adm_findorder':
            self.db.clear_state(chat_id)
            self.admin_order_search(chat_id, content)
            return True
        if state == 'adm_message':
            self.db.clear_state(chat_id)
            if not value or not str(value).isdigit():
                self.safe_send(chat_id, 'شناسه مخاطب معتبر نیست.')
                return True
            target = int(value)
            if not 1 <= len(content) <= 2000 or content.startswith('/'):
                self.safe_send(chat_id, 'متن پیام معتبر نیست یا بیشتر از 2000 نویسه دارد.')
                return True
            if not self.db.get_user(target) or self.db.admin_is_blocked(target):
                self.safe_send(chat_id, 'کاربر یافت نشد یا مسدود است.')
                return True
            result = self.safe_send(target, '📩 <b>پیام پشتیبانی</b>\n\n' + safe(content))
            self.db.admin_audit(chat_id, 'direct_message', target, 'sent' if result else 'failed')
            self.safe_send(chat_id, f'{"✅ پیام ارسال شد" if result else "❌ پیام ارسال نشد"} · شناسه {target}')
            return True
        return False

    def admin_handle_callback(self, chat_id, data, callback_message=None):
        if not self.is_admin(chat_id):
            self.safe_send(chat_id, '⛔ شما مجوز مدیریت ندارید.')
            return True
        if data.startswith(('adm:cat','adm:ct')):
            return self.catalog_callback(chat_id,data,callback_message)
        if data.startswith('adm:bc'):
            return self.bc_callback(chat_id,data,callback_message)
        if data in ('adm:home',):
            self.db.clear_state(chat_id)
            return self.admin_home(chat_id, callback_message)
        if data == 'adm:stats':
            return self.admin_stats_page(chat_id, callback_message)
        if data == 'adm:pending':
            return self.admin_pending(chat_id)
        if data in ('adm:finduser','adm:findorder','adm:dmchoose'):
            return self.admin_find_prompt(chat_id, {'adm:finduser':'user','adm:findorder':'order','adm:dmchoose':'dm'}[data], callback_message)
        bits = data.split(':')
        if len(bits) != 3 or not bits[2].isdigit() or len(bits[2]) > 15:
            return True
        op, target_id = bits[1], int(bits[2])
        if op == 'user':
            return self.admin_user_page(chat_id, target_id, callback_message)
        if op == 'userorders':
            return self.admin_user_orders(chat_id, target_id, callback_message)
        if op == 'order':
            return self.admin_order_page(chat_id, target_id, callback_message)
        if op == 'dm':
            return self.admin_dm_start(chat_id, target_id, callback_message)
        if op == 'blockask':
            if target_id in self.admin_ids:
                return self.safe_send(chat_id, 'امکان مسدودکردن مدیر وجود ندارد.')
            return self.screen(chat_id, f'آیا کاربر <code>{target_id}</code> مسدود شود؟', keyboard(
                [('🚫 بله، مسدود شود', f'adm:block:{target_id}')],
                [('خیر، بازگشت', f'adm:user:{target_id}')]), callback_message)
        if op in ('block','unblock'):
            if target_id in self.admin_ids:
                return self.safe_send(chat_id, 'تغییر دسترسی مدیر مجاز نیست.')
            if not self.db.get_user(target_id):
                return self.safe_send(chat_id, 'کاربر ثبت‌شده‌ای با این شناسه پیدا نشد.')
            self.db.admin_set_block(target_id, op=='block')
            self.db.admin_audit(chat_id, op, target_id)
            return self.admin_user_page(chat_id, target_id, callback_message)
        if op == 'receipt':
            order = self.db.get_order(target_id)
            if order and order.get('receipt_file_id'):
                try:
                    self.api.photo(chat_id, order['receipt_file_id'], caption=f'رسید {order_ref(target_id)}')
                except Exception:
                    try:
                        self.api.document(chat_id, order['receipt_file_id'], caption=f'رسید {order_ref(target_id)}')
                    except Exception:
                        self.safe_send(chat_id, 'تصویر رسید قابل دریافت نبود.')
            else:
                self.safe_send(chat_id, 'رسیدی برای این سفارش ثبت نشده است.')
            return True
        if op == 'rejectask':
            order = self.db.get_order(target_id)
            if not order or order['status'] != 'under_review':
                return self.safe_send(chat_id, 'این سفارش در انتظار بررسی نیست.')
            return self.screen(chat_id, f'آیا رسید سفارش {order_ref(target_id)} رد شود؟',
                keyboard([('❌ تأیید رد رسید', f'admin:reject:{target_id}')],
                         [('🔙 بازگشت', f'adm:order:{target_id}')]), callback_message)
        if op == 'resend':
            order = self.db.get_order(target_id)
            if order and order['status']=='approved' and order.get('service_link'):
                return self.deliver(target_id)
            return self.safe_send(chat_id, 'این سفارش لینک تأییدشده ندارد.')
        return True
