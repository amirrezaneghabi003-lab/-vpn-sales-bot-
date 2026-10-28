"""Telegram V2 storefront for the existing BaleBot project.

Runs next to bot.py, uses the same bot_data.db for shared catalogue/announcements,
but separates Telegram users, states, orders, and receipt file IDs.
No third-party Telegram SDK required (requests + python-dotenv from existing project).
"""
from __future__ import annotations

import argparse
from contextlib import closing
import html
import logging
import os
import re
import sys
import time
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from config import TELEGRAM_BOT_TOKEN, CARD_NUMBER, CARD_HOLDER
from service_catalog import PLANS, CatalogStore
from telegram_store import TelegramStore
from telegram_admin import AdminPanelMixin
from telegram_broadcast import BroadcastEngine
import telegram_ui as ui
from customer_experience import (SUPPORT, card_for_order, order_ref, jalali_datetime,
    money, purchase_text, plan_volume, single_user_notice, latin_digits, welcome_text, customer_announcement,
    TELEGRAM_DESCRIPTION, TELEGRAM_SHORT_DESCRIPTION, telegram_welcome_html)

ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger('telegram_v2')
STATUS = {
    'awaiting_receipt': '🧾 منتظر رسید',
    'under_review': '⏳ در حال بررسی',
    'approved': '✅ تأییدشده',
    'rejected': '❌ ردشده',
    'cancelled': '🚫 لغوشده',
}
EMOJI = {'طلایی': '🥇', 'بین‌الملل': '🌍', 'الترا': '⚡', 'تک کاربره نامحدود': '♾️'}


def escape(value):
    return html.escape(str(value or ''), quote=False)


def inline(*rows):
    return {'inline_keyboard': [[{'text': label, 'callback_data': data} for label, data in row] for row in rows]}


def reply_menu():
    return {
        'keyboard': [
            [{'text': '🛍 خرید اشتراک'}, {'text': '📦 سفارش‌های من'}],
            [{'text': '👤 حساب من'}, {'text': '📖 راهنمای اتصال'}],
            [{'text': '💬 پشتیبانی'}, {'text': '📢 اطلاعیه‌ها'}],
        ],
        'resize_keyboard': True,
        'is_persistent': True,
    }


# Native Telegram "Menu" button next to the input: works independently of inline keyboards.
# All actions are regular bot commands handled server-side below.
USER_MENU_COMMANDS = (
    ('start', '🏠 صفحه اصلی'),
    ('buy', '🛍 خرید سرویس'),
    ('orders', '📦 سفارش‌های من'),
    ('account', '👤 حساب کاربری'),
    ('guide', '📖 راهنمای اتصال'),
    ('support', '💬 پشتیبانی'),
    ('news', '📢 اطلاعیه‌ها'),
    ('cancel', '✖️ لغو مرحله فعلی'),
)


def apply_telegram_menu(api):
    """One-shot native command menu setup (never changes on startup)."""
    commands = [{'command': key, 'description': label} for key, label in USER_MENU_COMMANDS]
    for locale in ('', 'fa'):
        api.call('setMyCommands', {'commands': commands, 'language_code': locale})
    api.call('setChatMenuButton', {'menu_button': {'type': 'commands'}})


def time_diagnostic(db_path=None):
    """Read-only clock check; never prints user data or config/secrets."""
    utc_now = datetime.now(timezone.utc)
    from customer_experience import TEHRAN
    local_now = utc_now.astimezone(TEHRAN)
    print('Python UTC:', utc_now.strftime('%Y-%m-%d %H:%M:%S %z'))
    print('Tehran clock:', local_now.strftime('%Y-%m-%d %H:%M:%S %z'))
    print('Tehran Jalali:', jalali_datetime(utc_now))
    with closing(sqlite3.connect(':memory:')) as c:
        sql_now = c.execute('SELECT CURRENT_TIMESTAMP').fetchone()[0]
    print('SQLite CURRENT_TIMESTAMP (UTC):', sql_now)
    delta = abs((utc_now - datetime.fromisoformat(sql_now).replace(tzinfo=timezone.utc)).total_seconds())
    print('SQLite vs Python drift:', f'{delta:.1f}s')
    if db_path and db_path.is_file():
        # sqlite URI mode=ro protects live database against incidental write.
        with closing(sqlite3.connect(db_path.as_uri()+'?mode=ro', uri=True, timeout=5)) as conn:
            for table in ('orders', 'tg_orders'):
                found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if found:
                    row = conn.execute(f'SELECT created_at FROM {table} ORDER BY id DESC LIMIT 1').fetchone()
                    if row:
                        print(f'{table} last saved UTC:', row[0])
                        print(f'{table} display Tehran:', jalali_datetime(row[0]))


class TelegramAPIError(Exception):
    def __init__(self, code, description='', retry_after=None):
        self.code = code
        self.description = description
        self.retry_after = retry_after
        super().__init__(f'Telegram API {code}: {description}')


class TelegramAPI:
    def __init__(self, token, session=None):
        if not token or ':' not in token:
            raise ValueError('TELEGRAM_BOT_TOKEN تنظیم نشده یا معتبر نیست.')
        self.root = f'https://api.telegram.org/bot{token}'
        self.session = session or requests.Session()

    def call(self, method, payload=None, timeout=15):
        # Never log requests exceptions: their URLs contain the secret bot token.
        try:
            r = self.session.post(f'{self.root}/{method}', json=payload or {}, timeout=timeout)
            obj = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramAPIError(0, f'{method}: {type(exc).__name__}') from None
        if not isinstance(obj, dict) or not obj.get('ok'):
            if not isinstance(obj, dict):
                raise TelegramAPIError(0, f'{method}: پاسخ نامعتبر')
            desc = str(obj.get('description', 'Unknown error'))
            # Server descriptions should not contain our token, but still sanitize.
            token = self.root.rsplit('bot', 1)[-1]
            desc = desc.replace(token, '[REDACTED]')
            raise TelegramAPIError(obj.get('error_code', r.status_code), desc,
                                   (obj.get('parameters') or {}).get('retry_after'))
        return obj.get('result')

    def send(self, chat_id, text, markup=None):
        data = {'chat_id': chat_id, 'text': latin_digits(text), 'parse_mode': 'HTML',
                'disable_web_page_preview': True}
        if markup:
            data['reply_markup'] = markup
        return self.call('sendMessage', data)

    def photo(self, chat_id, file_id, caption=None, markup=None):
        data = {'chat_id': chat_id, 'photo': file_id, 'parse_mode': 'HTML'}
        if caption:
            data['caption'] = caption[:950]
        if markup:
            data['reply_markup'] = markup
        return self.call('sendPhoto', data)

    def document(self, chat_id, file_id, caption=None, markup=None):
        data = {'chat_id': chat_id, 'document': file_id, 'parse_mode': 'HTML'}
        if caption:
            data['caption'] = caption[:950]
        if markup:
            data['reply_markup'] = markup
        return self.call('sendDocument', data)

    def edit(self, chat_id, message_id, text, markup):
        return self.call('editMessageText', {
            'chat_id': chat_id, 'message_id': message_id, 'text': latin_digits(text),
            'parse_mode': 'HTML', 'disable_web_page_preview': True,
            'reply_markup': markup,
        })

    def answer(self, callback_id, text=None, alert=False):
        payload = {'callback_query_id': callback_id}
        if text:
            payload.update(text=text, show_alert=alert)
        return self.call('answerCallbackQuery', payload, timeout=5)


class Storefront(AdminPanelMixin):
    def __init__(self, store, api, *, admin_ids, card_number, card_holder, support_contact):
        self.db = store
        self.api = api
        self.admin_ids = frozenset(admin_ids)
        self.card_number = card_number
        self.card_holder = card_holder
        self.support_contact = SUPPORT

    def safe_send(self, chat_id, text, markup=None):
        try:
            return self.api.send(chat_id, latin_digits(text), markup)
        except TelegramAPIError as e:
            LOGGER.warning('sendMessage to chat %s failed (HTTP/API %s)', chat_id, e.code)
            return None

    def screen(self, chat_id, text, keyboard, callback_message=None):
        if callback_message and callback_message.get('chat', {}).get('id') == chat_id:
            try:
                self.api.edit(chat_id, callback_message['message_id'], latin_digits(text), keyboard)
                return
            except TelegramAPIError as e:
                if 'message is not modified' in e.description.lower():
                    return
                LOGGER.info('edit fallback for chat %s (code %s)', chat_id, e.code)
        self.safe_send(chat_id, text, keyboard)

    def is_admin(self, user_id):
        return user_id in self.admin_ids

    def welcome(self, chat_id, name='', callback_message=None):
        rows = [[('🛍 خرید سرویس VPN', 'catalog')],
                [('📦 سفارش‌های من', 'orders:0'), ('👤 حساب کاربری', 'account')],
                [('📖 راهنمای خرید', 'guide'), ('💬 پشتیبانی', 'support')]]
        if self.is_admin(chat_id):
            rows.append([('🛡 مرکز مدیریت', 'adm:home')])
        self.screen(chat_id, ui.home(), inline(*rows), callback_message)

    def catalog(self, chat_id, callback_message=None):
        categories = list(dict.fromkeys(p.get('type') for p in PLANS.values() if p.get('type')))
        known={}
        if isinstance(getattr(self.db,'path',None),(str,Path)) and Path(self.db.path).is_file():
            known={c['name']:c['id'] for c in CatalogStore(self.db.path).categories()}
        buttons = [(f"{EMOJI.get(typ, '🔹')} {typ}", f'categoryid:{known[typ]}' if typ in known else f'category:{i}')
                   for i, typ in enumerate(categories)]
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        rows.append([('🏠 صفحه اصلی', 'home')])
        self.screen(chat_id, ui.catalog(len(categories)), inline(*rows), callback_message)

    def category_by_id(self,chat_id,cid,callback_message=None):
        record=CatalogStore(self.db.path).category(cid)
        if not record or record['deleted']:
            return self.catalog(chat_id,callback_message)
        categories=list(dict.fromkeys(p.get('type') for p in PLANS.values() if p.get('type')))
        if record['name'] not in categories:return self.catalog(chat_id,callback_message)
        return self.category(chat_id,categories.index(record['name']),callback_message)

    def category(self, chat_id, index, callback_message=None):
        categories = list(dict.fromkeys(p.get('type') for p in PLANS.values() if p.get('type')))
        if index < 0 or index >= len(categories):
            return self.catalog(chat_id, callback_message)
        typ = categories[index]
        plans = [(pid, plan) for pid, plan in PLANS.items() if plan.get('type') == typ]
        statuses = self.db.plan_statuses([pid for pid, _ in plans])
        popular = self.db.popular_plan_id()
        rows = []
        for plan_id, plan in plans:
            available, _ = statuses[plan_id]
            # Keep labels readable on mobile and show prices before the user taps.
            if available:
                prefix = '⭐ پرفروش · ' if plan_id == popular else '✅ '
                label = prefix + f"{plan['display']} · {money(plan['price'])} تومان"
                rows.append([(label, f'plan:{plan_id}')])
            else:
                # Telegram has no disabled inline buttons; no-op callback never buys.
                rows.append([(f'⛔ {plan["display"]} · ناموجود',f'soldout:{plan_id}')])
        text=ui.category(typ, len(plans), sum(bool(statuses[pid][0]) for pid,_ in plans))
        rows.append([('🔙 دسته‌بندی‌ها', 'catalog')])
        self.screen(chat_id,text,inline(*rows),callback_message)

    def plan(self, chat_id, plan_id, callback_message=None):
        plan = PLANS.get(plan_id)
        if not plan:
            return self.catalog(chat_id, callback_message)
        available, unavailable_message = self.db.plan_status(plan_id)
        is_single = plan_id == 'UNLIMITED_SINGLE' or plan.get('type') == 'تک کاربره نامحدود'
        duration = plan.get('duration') or plan.get('validity')
        text = ui.plan(plan['display'], plan.get('type','سرویس VPN'), plan_volume(plan),
                       'فقط یک کاربر' if is_single else plan.get('users', 'مطابق مشخصات پلن'),
                       plan['price'], duration=duration, available=available,
                       reason=unavailable_message, single=is_single)
        actions = []
        if available:
            actions.append([('🛒 ثبت سفارش', f'buy:{plan_id}')])
        actions.append([('🔙 بسته‌های این دسته', f'category:{self.category_index(plan.get("type"))}')])
        actions.append([('🏠 صفحه اصلی', 'home')])
        self.screen(chat_id, text, inline(*actions), callback_message)

    def category_index(self, typ):
        categories = list(dict.fromkeys(p.get('type') for p in PLANS.values() if p.get('type')))
        return categories.index(typ) if typ in categories else 0

    def checkout(self, chat_id, plan_id, callback_message=None, acknowledged=False):
        plan = PLANS.get(plan_id)
        if not plan:
            return self.catalog(chat_id, callback_message)
        available, _ = self.db.plan_status(plan_id)
        if not available:
            return self.safe_send(chat_id, 'این سرویس فعلاً موجود نیست. لطفاً سرویس دیگری انتخاب بفرمایید.')
        if (plan_id == 'UNLIMITED_SINGLE' or plan.get('type') == 'تک کاربره نامحدود') and not acknowledged:
            self.db.set_state(chat_id, 'ack_unlimited', plan_id)
            return self.screen(chat_id, escape(single_user_notice()),
                               inline([('✅ متوجه شدم، ادامه خرید', f'ack:{plan_id}')],
                                      [('🔙 بازگشت به سرویس‌ها', 'catalog')]), callback_message)
        if not self.admin_ids:
            return self.safe_send(chat_id, 'ثبت سفارش موقتاً در دسترس نیست. لطفاً با پشتیبانی تماس بگیرید.')
        order, _ = self.db.open_order(chat_id, plan_id, int(plan['price']))
        self.db.set_state(chat_id, 'receipt_pending', order['id'])
        self.payment(chat_id, order['id'], callback_message)

    def payment(self, chat_id, order_id, callback_message=None):
        order = self.db.get_user_order(order_id, chat_id)
        if not order:
            return self.safe_send(chat_id, 'سفارش موردنظر در حساب شما پیدا نشد.')
        if order['status'] != 'awaiting_receipt':
            return self.order_detail(chat_id, order_id, callback_message)
        if not self.db.plan_status(order['plan_id'])[0]:
            self.db.clear_state(chat_id)
            return self.screen(chat_id,self.stock_notice(),
                inline([('💬 پشتیبانی','support')],[('📦 سفارش‌ها','orders:0')]),callback_message)
        self.db.set_state(chat_id, 'receipt_pending', order_id)
        card_number, card_holder = card_for_order(order.get('payment_card_index'), self.card_number, self.card_holder)
        plan = PLANS.get(order['plan_id'], {'display':order['plan_id']})
        self.screen(chat_id,
                    ui.invoice(order_ref(order_id), self.db.order_title(order_id,order['plan_id']), order['amount'], card_number, card_holder),
                    inline([('📤 ارسال رسید', f'receipt:{order_id}')]), callback_message)

    @staticmethod
    def stock_notice():
        return ('⛔ این سرویس در حال حاضر ناموجود است و امکان ثبت رسید جدید ندارد.\n'
                'اگر قبلاً پرداخت کرده‌اید، برای پیگیری پرداخت یا بازگشت وجه با پشتیبانی تماس بگیرید: '
                + escape(SUPPORT))

    def receipt_prompt(self, chat_id, order_id):
        order = self.db.get_user_order(order_id, chat_id)
        if not order or order['status'] != 'awaiting_receipt' or order.get('receipt_file_id'):
            return self.safe_send(chat_id, 'این سفارش امکان دریافت رسید جدید ندارد. وضعیت سفارش را از بخش سفارش‌های من بررسی کنید.')
        if not self.db.plan_status(order['plan_id'])[0]:
            self.db.clear_state(chat_id)
            return self.safe_send(chat_id,self.stock_notice())
        self.db.set_state(chat_id, 'waiting_receipt', order_id)
        return self.safe_send(chat_id, f'📤 لطفاً تصویر رسید پرداخت سفارش <b>{order_ref(order_id)}</b> را در همین گفت‌وگو ارسال کنید.')

    def orders(self, chat_id, page=0, callback_message=None):
        page = max(0, min(page, 100000))
        items = self.db.user_orders(chat_id, limit=7, offset=page * 7)
        rows = [[(f"{ui.STATUS.get(item['status'], '❓')} · {order_ref(item['id'])}", f"order:{item['id']}")]
                for item in items]
        navigation = []
        if page:
            navigation.append(('⬅️ قبلی', f'orders:{page-1}'))
        if len(items) == 7:
            navigation.append(('بعدی ➡️', f'orders:{page+1}'))
        if navigation:
            rows.append(navigation)
        if not items:
            rows.append([('🛍 مشاهده سرویس‌ها', 'catalog')])
        else:
            rows.append([('🛍 خرید سرویس جدید', 'catalog')])
        rows.append([('🏠 صفحه اصلی', 'home')])
        self.screen(chat_id, ui.order_list(len(items)), inline(*rows), callback_message)

    def order_detail(self, chat_id, order_id, callback_message=None):
        order = self.db.get_user_order(order_id, chat_id)
        if not order:
            return self.screen(chat_id, 'این سفارش در حساب شما پیدا نشد.',
                               inline([('📦 سفارش‌های من', 'orders:0')]), callback_message)
        plan = PLANS.get(order['plan_id'], {})
        text = ui.order_details(order, self.db.order_title(order_id,order['plan_id']))
        rows = []
        if order['status'] == 'awaiting_receipt':
            if self.db.plan_status(order['plan_id'])[0]:
                rows.append([('💳 مشاهده پرداخت / ارسال رسید', f'pay:{order_id}')])
            else:
                text += '\n\n⛔ سرویس ناموجود است. اگر پرداخت کرده‌اید با پشتیبانی تماس بگیرید.'
                rows.append([('💬 پشتیبانی','support')])
            rows.append([('🚫 لغو سفارش', f'cancelask:{order_id}')])
        elif order['status'] == 'rejected':
            rows.append([('💬 پشتیبانی', 'support')])
        rows.append([('🔙 سفارش‌های من', 'orders:0')])
        self.screen(chat_id, text, inline(*rows), callback_message)

    def account(self, chat_id, callback_message=None):
        info = self.db.get_user(chat_id) or {'user_id': chat_id}
        stats = self.db.user_stats(chat_id)
        last = self.db.user_orders(chat_id, limit=1)
        if not isinstance(last, (list, tuple)):
            last = []
        text = ui.profile(info, stats, last[0] if last else None)
        rows = []
        if last:
            rows.append([('🔎 آخرین سفارش', f'order:{last[0]["id"]}')])
        rows.extend([[('📦 سفارش‌های من', 'orders:0'), ('🛍 خرید سرویس', 'catalog')],
                     [('🏠 صفحه اصلی', 'home')]])
        self.screen(chat_id, text, inline(*rows), callback_message)

    def guide(self, chat_id, callback_message=None):
        self.screen(chat_id, ui.guide(),
                    inline([('🛍 مشاهده سرویس‌ها', 'catalog'), ('💬 پشتیبانی', 'support')],
                           [('🏠 صفحه اصلی', 'home')]), callback_message)

    def support(self, chat_id, callback_message=None):
        text = ('💬 <b>پشتیبانی</b>\n\n'
                'اگر درباره خرید، پرداخت یا اتصال به سرویس سؤالی دارید، با پشتیبانی در ارتباط باشید.\n\n'
                f'📩 شناسه پشتیبانی: <code>{SUPPORT}</code>')
        kb = {'inline_keyboard': [
            [{'text':'✉️ گفت‌وگو با پشتیبانی','url':'https://t.me/' + SUPPORT[1:]}],
            [{'text':'🏠 بازگشت به خانه','callback_data':'home'}]
        ]}
        self.screen(chat_id, text, kb, callback_message)

    def news(self, chat_id, callback_message=None):
        message = customer_announcement(self.db.announcement())
        text = '📢 <b>اطلاعیه‌ها</b>\n\n' + (escape(message)[:3000] if message else 'فعلاً اطلاعیه‌ای ثبت نشده.')
        self.screen(chat_id, text, inline([('🏠 خانه', 'home')]), callback_message)

    def ask_cancel(self, chat_id, order_id, callback_message=None):
        order = self.db.get_user_order(order_id, chat_id)
        if not order or order['status'] != 'awaiting_receipt':
            return self.order_detail(chat_id, order_id, callback_message)
        kb = inline([('بله، سفارش را لغو کن', f'cancel:{order_id}')],
                    [('خیر، بازگشت', f'order:{order_id}')])
        self.screen(chat_id, f'آیا می‌خواهید سفارش {order_ref(order_id)} را لغو کنید؟', kb, callback_message)

    def cancel(self, chat_id, order_id, callback_message=None):
        ok = self.db.cancel_order(order_id, chat_id)
        state, data = self.db.get_state(chat_id)
        if ok and state == 'waiting_receipt' and data == str(order_id):
            self.db.clear_state(chat_id)
        self.screen(chat_id, '✅ سفارش لغو شد.' if ok else '⚠️ امکان لغو این سفارش وجود ندارد.',
                    inline([('📦 سفارش‌ها', 'orders:0')], [('🏠 خانه', 'home')]), callback_message)

    def on_photo(self, chat_id, photo_id, is_document=False):
        state, data = self.db.get_state(chat_id)
        if state != 'waiting_receipt' or not data or not data.isdigit():
            self.safe_send(chat_id, 'برای ثبت رسید، ابتدا روی دکمه «ارسال رسید» در صفحه پرداخت بزنید و سپس تصویر را ارسال کنید.')
            return
        order_id = int(data)
        if not self.db.submit_receipt(order_id, chat_id, photo_id):
            self.db.clear_state(chat_id)
            order=self.db.get_user_order(order_id,chat_id)
            if order and not self.db.plan_status(order['plan_id'])[0]:
                self.safe_send(chat_id,self.stock_notice())
            else:
                self.safe_send(chat_id,'⚠️ این سفارش دیگر منتظر رسید نیست. وضعیت آن را بررسی کنید.')
            return
        self.db.clear_state(chat_id)
        self.safe_send(chat_id, f'✅ رسید سفارش <b>{order_ref(order_id)}</b> دریافت شد.\n⏱ زمان بررسی: حداکثر ۱۰ دقیقه.',
                       inline([('📦 پیگیری سفارش', f'order:{order_id}')]))
        self.notify_admins(order_id, photo_id, is_document=is_document)

    def notify_admins(self, order_id, photo_id, is_document=False):
        order = self.db.get_order(order_id)
        if not order:
            return
        plan = PLANS.get(order['plan_id'], {})
        text = (f"🔔 <b>رسید جدید | سفارش {order_ref(order_id)}</b>\n"
                f"👤 شناسه تلگرام: <code>{order['user_id']}</code>\n"
                f"📦 {escape(plan.get('display', order['plan_id']))}\n"
                f"💰 {order['amount']:,} تومان\n\n"
                '⚠️ پرداخت را مستقل بررسی کنید؛ تصویر رسید به‌تنهایی اثبات پرداخت نیست.')
        kb = inline([('✅ تأیید', f'admin:approve:{order_id}'),
                     ('❌ رد', f'admin:reject:{order_id}')])
        if not self.admin_ids:
            LOGGER.error('TG_ADMIN_IDS empty: receipt %s has no assigned reviewer!', order_id)
        for admin_id in self.admin_ids:
            self.safe_send(admin_id, text)
            try:
                if is_document:
                    self.api.document(admin_id, photo_id, caption=f'🧾 رسید سفارش {order_ref(order_id)}', markup=kb)
                else:
                    self.api.photo(admin_id, photo_id, caption=f'🧾 رسید سفارش {order_ref(order_id)}', markup=kb)
            except TelegramAPIError as e:
                LOGGER.warning('Sending receipt to admin %s failed (%s)', admin_id, e.code)
                self.safe_send(admin_id, f'🧾 سفارش {order_ref(order_id)}', kb)

    def admin_pending(self, chat_id):
        if not self.is_admin(chat_id):
            return
        orders = self.db.pending_orders()
        if not orders:
            self.safe_send(chat_id, '✅ سفارشی در صف بررسی نیست.')
            return
        for order in orders:
            kb = inline([('✅ تأیید', f"admin:approve:{order['id']}"),
                         ('❌ رد', f"admin:reject:{order['id']}")])
            self.safe_send(chat_id,
                           f"⏳ سفارش {order_ref(order['id'])} | {escape(PLANS.get(order['plan_id'], {}).get('display', order['plan_id']))}\n"
                           f"مبلغ: {order['amount']:,} تومان", kb)
            if order['receipt_file_id']:
                try:
                    self.api.photo(chat_id, order['receipt_file_id'], caption=f"فیش سفارش {order_ref(order['id'])}")
                except TelegramAPIError:
                    # Document receipts may not be usable with sendPhoto.
                    try:
                        self.api.document(chat_id, order['receipt_file_id'], caption=f"فیش سفارش {order_ref(order['id'])}")
                    except TelegramAPIError:
                        LOGGER.warning('Could not resend receipt for order %s', order['id'])

    def admin_callback(self, chat_id, data):
        if not self.is_admin(chat_id):
            self.safe_send(chat_id, '⛔ دسترسی مدیریت ندارید.')
            return
        parts = data.split(':')
        if len(parts) != 3 or not parts[2].isdigit():
            return
        action, oid = parts[1], int(parts[2])
        order = self.db.get_order(oid)
        if not order or order['status'] != 'under_review':
            self.safe_send(chat_id, '⚠️ سفارش پیدا نشد یا قبلاً بررسی شده است.')
            return
        if action == 'approve':
            self.db.set_state(chat_id, 'admin_link', oid)
            self.safe_send(chat_id, f'✅ لینک اتصال سفارش {order_ref(oid)} را بفرست.\n'
                                    'برای لغو این مرحله /cancel را ارسال کن.')
        elif action == 'reject':
            if self.db.reject_order(oid):
                self.safe_send(chat_id, f'❌ سفارش {order_ref(oid)} رد شد.')
                self.safe_send(order['user_id'], f'نتیجه بررسی سفارش {order_ref(oid)}: پرداخت تأیید نشد.\nبرای پیگیری لطفاً با پشتیبانی تماس بگیرید: {SUPPORT}')
            else:
                self.safe_send(chat_id, '⚠️ وضعیت سفارش تغییر کرده است.')

    def admin_link(self, admin_id, link):
        state, data = self.db.get_state(admin_id)
        if state != 'admin_link' or not data or not data.isdigit():
            return False
        oid = int(data)
        value = link.strip()
        if not 5 <= len(value) <= 2000 or not re.match(r'(?i)^(https?://|vless://|vmess://|trojan://|ss://|ssr://|hysteria2?://|tuic://)', value):
            self.safe_send(admin_id, '⚠️ لینک معتبر ارسال کن (https، vless، vmess، trojan، ss و ...).')
            return True
        self.db.clear_state(admin_id)
        if not self.db.approve_order(oid, value):
            self.safe_send(admin_id, '⚠️ سفارش قبلاً بررسی شده یا دیگر در صف نیست.')
            return True
        self.deliver(oid)
        return True

    def deliver(self, order_id):
        order = self.db.get_order(order_id)
        if not order or order['status'] != 'approved' or not order['service_link']:
            return False
        content = (f"🎉 <b>سفارش {order_ref(order_id)} تأیید شد!</b>\n\n"
                   '🔗 اطلاعات اتصال سرویس:\n'
                   f"<code>{escape(order['service_link'])}</code>\n\n"
                   'برای اتصال، این لینک را در نرم‌افزار سازگار وارد کنید.\n\nاز اعتماد شما سپاسگزاریم.')
        result = self.safe_send(order['user_id'], content,
                                inline([('📦 جزئیات سفارش', f'order:{order_id}')],
                                       [('💬 پشتیبانی', 'support')]))
        if result is not None:
            self.db.mark_delivered(order_id)
            for admin_id in self.admin_ids:
                self.safe_send(admin_id, f'✅ سفارش {order_ref(order_id)} تأیید و لینک به مشتری ارسال شد.')
            return True
        for admin_id in self.admin_ids:
            self.safe_send(admin_id, f'⚠️ سفارش {order_ref(order_id)} تأیید شد اما ارسال لینک ناموفق بود. /resend {order_id}')
        return False

    def message(self, msg):
        if msg.get('chat', {}).get('type') != 'private':
            return
        chat_id = msg['chat']['id']
        who = msg.get('from') or {}
        if who.get('is_bot'):
            return
        self.db.upsert_user(chat_id, who.get('username'), who.get('first_name'))
        if not self.is_admin(chat_id) and isinstance(self.db, TelegramStore) and self.db.admin_is_blocked(chat_id):
            return self.safe_send(chat_id, 'دسترسی این حساب به ربات محدود شده است. برای پیگیری با پشتیبانی تماس بگیرید.')
        # Admin announcement composer consumes photos/text before customer receipt flow.
        if self.is_admin(chat_id) and self.bc_handle_message(chat_id,msg):
            return
        photos = msg.get('photo') or []
        if photos:
            return self.on_photo(chat_id, photos[-1]['file_id'])
        doc = msg.get('document')
        if doc and str(doc.get('mime_type', '')).startswith('image/'):
            return self.on_photo(chat_id, doc['file_id'], is_document=True)
        content = (msg.get('text') or '').strip()
        if not content:
            return self.safe_send(chat_id, 'لطفاً یکی از گزینه‌های منوی تلگرام را انتخاب کنید یا تصویر رسید سفارش را ارسال بفرمایید.', inline([('🛍 خرید سرویس', 'catalog')], [('🏠 صفحه اصلی', 'home')]))
        cmd = content.split(maxsplit=1)[0].split('@')[0]
        if cmd in ('/unsubscribe','/stop_ads'):
            self.db.bc_subscription(chat_id, False)
            return self.safe_send(chat_id, '✅ ارسال اطلاعیه‌های گروهی برای شما غیرفعال شد.\nبرای فعال‌سازی دوباره: /subscribe')
        if cmd in ('/subscribe','/start_ads'):
            self.db.bc_subscription(chat_id, True)
            return self.safe_send(chat_id, '✅ دریافت اطلاعیه‌های گروهی فعال شد.')
        if cmd in ('/start', '/menu'):
            self.db.bc_user_reachable(chat_id)

            self.db.clear_state(chat_id)
            return self.welcome(chat_id, who.get('first_name', ''))
        # Menu button commands must work even when the inline purchase keyboard is open.
        destinations = {
            '/buy': self.catalog,
            '/orders': self.orders,
            '/account': self.account,
            '/guide': self.guide,
            '/support': self.support,
            '/news': self.news,
        }
        if cmd in destinations:
            self.db.clear_state(chat_id)
            return destinations[cmd](chat_id)
        if cmd == '/cancel':
            self.db.clear_state(chat_id)
            return self.safe_send(chat_id, '✅ مرحله فعلی لغو شد. برای ادامه، یکی از گزینه‌ها را انتخاب کنید.', inline([('🛍 خرید سرویس', 'catalog')], [('🏠 صفحه اصلی', 'home')]))
        if cmd == '/myid':
            return self.safe_send(chat_id, f'🆔 شناسه تلگرام شما: <code>{chat_id}</code>')
        if cmd == '/admin' and self.is_admin(chat_id):
            self.db.clear_state(chat_id)
            return self.admin_home(chat_id)
        if cmd == '/pending' and self.is_admin(chat_id):
            return self.admin_pending(chat_id)
        if cmd == '/stats' and self.is_admin(chat_id):
            return self.admin_stats_page(chat_id)
        if cmd == '/resend' and self.is_admin(chat_id):
            parts = content.split()
            if len(parts) != 2 or not parts[1].isdigit():
                return self.safe_send(chat_id, 'فرمت صحیح: /resend 123')
            return self.deliver(int(parts[1]))
        if self.is_admin(chat_id) and self.db.get_state(chat_id)[0] == 'admin_link':
            return self.admin_link(chat_id, content)
        if self.admin_handle_text(chat_id, content):
            return
        if content == '🛍 خرید اشتراک':
            return self.catalog(chat_id)
        if content == '📦 سفارش‌های من':
            return self.orders(chat_id)
        if content == '👤 حساب من':
            return self.account(chat_id)
        if content == '📖 راهنمای اتصال':
            return self.guide(chat_id)
        if content == '💬 پشتیبانی':
            return self.support(chat_id)
        if content == '📢 اطلاعیه‌ها':
            return self.news(chat_id)
        if self.db.get_state(chat_id)[0] == 'waiting_receipt':
            return self.safe_send(chat_id, 'لطفاً تصویر رسید پرداخت را ارسال کنید یا با /cancel این مرحله را لغو کنید.')
        return self.safe_send(chat_id, 'لطفاً از منوی کنار کادر پیام یا گزینه‌های زیر استفاده کنید.', inline([('🛍 خرید سرویس', 'catalog')], [('🏠 صفحه اصلی', 'home')]))

    def callback(self, query):
        user = query.get('from') or {}
        user_id = user.get('id')
        message = query.get('message') or {}
        chat = message.get('chat') or {}
        if not user_id or chat.get('type') != 'private' or chat.get('id') != user_id:
            return
        try:
            self.api.answer(query['id'])
        except TelegramAPIError:
            pass
        self.db.upsert_user(user_id, user.get('username'), user.get('first_name'))
        if not self.is_admin(user_id) and isinstance(self.db, TelegramStore) and self.db.admin_is_blocked(user_id):
            return self.safe_send(user_id, 'دسترسی حساب شما محدود شده است.')
        data = query.get('data', '')
        if data.startswith('adm:'):
            return self.admin_handle_callback(user_id, data, message)
        if data in {'home', 'catalog', 'guide', 'support', 'account', 'news'}:
            self.db.clear_state(user_id)
        if data == 'home':
            return self.welcome(user_id, user.get('first_name', ''), message)
        if data == 'catalog':
            return self.catalog(user_id, message)
        if data == 'guide':
            return self.guide(user_id, message)
        if data == 'support':
            return self.support(user_id, message)
        if data == 'account':
            return self.account(user_id, message)
        if data == 'news':
            return self.news(user_id, message)
        try:
            action, arg = data.split(':', 1)
        except ValueError:
            return self.safe_send(user_id, 'این منوی قدیمی است؛ از /start دوباره شروع کن.')
        if action == 'admin':
            return self.admin_callback(user_id, data)
        if action == 'categoryid' and arg.isdigit():
            self.db.clear_state(user_id)
            return self.category_by_id(user_id,int(arg),message)
        if action == 'soldout':
            return  # Already answered callback; nothing to buy or navigate to.
        if action == 'category' and arg.isdigit():
            self.db.clear_state(user_id)
            return self.category(user_id, int(arg), message)
        if action == 'plan':
            self.db.clear_state(user_id)
            return self.plan(user_id, arg, message)
        if action == 'buy':
            return self.checkout(user_id, arg, message)
        if action == 'ack' and PLANS.get(arg) and (arg == 'UNLIMITED_SINGLE' or PLANS[arg].get('type') == 'تک کاربره نامحدود'):
            state, value = self.db.get_state(user_id)
            if state != 'ack_unlimited' or value != arg:
                return self.safe_send(user_id, 'لطفاً ابتدا شرایط این سرویس را مطالعه کنید.',
                                      inline([('📦 مشاهده سرویس', f'plan:{arg}')]))
            self.db.clear_state(user_id)
            return self.checkout(user_id, arg, message, acknowledged=True)
        if action == 'orders' and arg.isdigit():
            self.db.clear_state(user_id)
            return self.orders(user_id, int(arg), message)
        if action == 'order' and arg.isdigit():
            return self.order_detail(user_id, int(arg), message)
        if action == 'pay' and arg.isdigit():
            return self.payment(user_id, int(arg), message)
        if action == 'receipt' and arg.isdigit():
            return self.receipt_prompt(user_id, int(arg))
        if action == 'cancelask' and arg.isdigit():
            return self.ask_cancel(user_id, int(arg), message)
        if action == 'cancel' and arg.isdigit():
            return self.cancel(user_id, int(arg), message)
        return self.safe_send(user_id, 'این گزینه شناخته نشد؛ /start را بزن.')


class Runner:
    def __init__(self, app, api, offset_path):
        self.app = app
        self.api = api
        self.offset_path = Path(offset_path)

    def shutdown(self):
        if hasattr(self,'_bc_stop'):
            self._bc_stop.set()
            self._bc_engine.close()
            self.app.db.bc_lease_release(self._bc_owner)

    def load_offset(self):
        try:
            value = int(self.offset_path.read_text(encoding='utf-8').strip())
            return value if value >= 0 else None
        except (FileNotFoundError, ValueError):
            return None

    def save_offset(self, offset):
        temp = self.offset_path.with_suffix(self.offset_path.suffix + '.tmp')
        temp.write_text(str(offset), encoding='utf-8')
        os.replace(temp, self.offset_path)

    def run(self):
        profile = self.api.call('getMe')
        LOGGER.info('Telegram bot connected: @%s', profile.get('username', '(unnamed)'))
        hook = self.api.call('getWebhookInfo')
        if hook.get('url'):
            raise RuntimeError('Webhook is active. Disable it explicitly before using long polling.')
        # Only one broadcaster may hold the local SQLite lease at once.
        self._bc_owner=str(uuid.uuid4())
        if not self.app.db.bc_lease_acquire(self._bc_owner):
            raise RuntimeError('Another broadcast worker owns this database. Stop other Telegram process.')
        self._bc_engine=BroadcastEngine(self.app.db, lambda: TelegramAPI(TELEGRAM_BOT_TOKEN),
                           workers=int(os.getenv('TG_BROADCAST_WORKERS','3')),
                           rate=int(os.getenv('TG_BROADCAST_RATE','18')))
        self._bc_stop=threading.Event()
        def heartbeat():
            while not self._bc_stop.wait(8):
                try:
                    if not self.app.db.bc_lease_renew(self._bc_owner):
                        LOGGER.critical('Broadcast lease lost; pausing all local senders')
                        self._bc_engine.close()
                        return
                except Exception:
                    LOGGER.exception('Broadcast lease heartbeat failure')
                    self._bc_engine.close()
                    return
        self._bc_engine.start()
        self._bc_heartbeat=threading.Thread(target=heartbeat,daemon=True,name='bc-heartbeat')
        self._bc_heartbeat.start()
        offset = self.load_offset()
        delay = 1
        LOGGER.info('Polling started. Offset=%s', offset)
        while True:
            try:
                updates = self.api.call('getUpdates', {
                    'offset': offset, 'timeout': 35, 'limit': 50,
                    'allowed_updates': ['message', 'callback_query']
                }, timeout=45)
                delay = 1
                for upd in updates:
                    update_id = upd.get('update_id')
                    if update_id is None:
                        continue
                    try:
                        if 'message' in upd:
                            self.app.message(upd['message'])
                        elif 'callback_query' in upd:
                            self.app.callback(upd['callback_query'])
                    except Exception:
                        LOGGER.exception('Handler failed (update_id=%s)', update_id)
                    offset = update_id + 1
                    self.save_offset(offset)
            except TelegramAPIError as exc:
                if exc.code == 409:
                    LOGGER.error('HTTP 409: another polling instance or webhook is active. STOP the other process.')
                    raise RuntimeError('Telegram polling conflict (409)') from None
                LOGGER.warning('Polling API error (%s), retry in %ss', exc.code, delay)
                time.sleep(max(delay, min(exc.retry_after or delay, 60)))
                delay = min(delay * 2, 30)
            except KeyboardInterrupt:
                LOGGER.info('Stopped by user.')
                return
            except Exception:
                LOGGER.exception('Unexpected error; retrying')
                time.sleep(delay)
                delay = min(delay * 2, 30)


def parse_admin_ids(value):
    result = set()
    for item in value.split(','):
        item = item.strip()
        if item:
            result.add(int(item))
    return result



def apply_telegram_profile(api):
    """Explicit, one-shot Telegram profile update; never run automatically on boot.

    Update both fallback and Persian localized fields so an old fa-specific
    description cannot hide the new default for Persian-language clients.
    """
    if not (0 < len(TELEGRAM_DESCRIPTION) <= 512):
        raise ValueError('Telegram pre-start description exceeds 512 chars')
    if not (0 < len(TELEGRAM_SHORT_DESCRIPTION) <= 120):
        raise ValueError('Telegram short profile description exceeds 120 chars')
    for locale in ('', 'fa'):
        api.call('setMyDescription', {
            'description': TELEGRAM_DESCRIPTION, 'language_code': locale,
        })
        api.call('setMyShortDescription', {
            'short_description': TELEGRAM_SHORT_DESCRIPTION, 'language_code': locale,
        })



def main():
    parser = argparse.ArgumentParser(description='Safe Telegram V2 storefront for BaleBot')
    parser.add_argument('--check', action='store_true', help='read-only environment validation, no network or writes')
    parser.add_argument('--migrate-only', action='store_true', help='create only tg_* tables; back up DB first')
    parser.add_argument('--remove-webhook', action='store_true', help='explicitly remove Telegram webhook (keep pending updates)')
    parser.add_argument('--preview-profile', action='store_true', help='show new profile texts locally; no network')
    parser.add_argument('--set-profile', action='store_true', help='update Telegram pre-start and short descriptions once')
    parser.add_argument('--set-menu', action='store_true', help='configure native command Menu button once')
    parser.add_argument('--preview-menu', action='store_true', help='display native menu items offline')
    parser.add_argument('--time-check', action='store_true', help='read-only UTC/Tehran/SQLite clock diagnosis')
    parser.add_argument('--setup-ui', action='store_true', help='one-shot profile and native Menu configuration')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    selected = [name for name in ('check','migrate_only','remove_webhook','preview_profile','set_profile','set_menu','preview_menu','time_check','setup_ui')
                if getattr(args, name)]
    if len(selected) > 1:
        parser.error('Select only one action at a time.')
    if args.preview_profile:
        print('=== Telegram description before Start ===')
        print(TELEGRAM_DESCRIPTION)
        print('=== Short profile bio ===')
        print(TELEGRAM_SHORT_DESCRIPTION)
        print('No Telegram API calls made.')
        return
    if args.preview_menu:
        for command, description in USER_MENU_COMMANDS:
            print(f'/{command}: {description}')
        print('No Telegram API calls made.')
        return
    if args.setup_ui:
        api = TelegramAPI(TELEGRAM_BOT_TOKEN)
        apply_telegram_profile(api)
        apply_telegram_menu(api)
        print('PASS: Telegram pre-start profile and native Menu installed (default + fa).')
        print('No webhook, database, or customer messages changed.')
        return
    if args.set_menu:
        apply_telegram_menu(TelegramAPI(TELEGRAM_BOT_TOKEN))
        print('PASS: native Telegram Menu and commands configured (default + fa).')
        print('No webhook, database, or customer messages changed.')
        return
    if args.time_check:
        target = Path(os.getenv('BOT_DB_PATH') or ROOT / 'bot_data.db').expanduser().resolve()
        time_diagnostic(target)
        return
    if args.set_profile:
        apply_telegram_profile(TelegramAPI(TELEGRAM_BOT_TOKEN))
        print('PASS: Telegram profile descriptions updated (default and fa).')
        print('No webhook, database, or bot messages changed.')
        return
    path = Path(os.getenv('BOT_DB_PATH') or ROOT / 'bot_data.db').expanduser().resolve()
    admins = parse_admin_ids(os.getenv('TG_ADMIN_IDS', ''))
    if args.check:
        print('Database file:', path, '| exists:', path.is_file())
        print('Telegram token:', 'SET' if TELEGRAM_BOT_TOKEN else 'MISSING')
        print('TG_ADMIN_IDS:', f'{len(admins)} admin(s)' if admins else 'MISSING')
        print('Payment cards: 2 configured (shared approval-based rotation)')
        print('Plans:', len(PLANS))
        print('Data migration: NOT executed. Telegram API: NOT contacted.')
        return
    if not path.is_file():
        parser.error(f'DB not found: {path}. Refusing to create an empty unrelated DB.')
    if not args.migrate_only and not admins:
        LOGGER.warning('TG_ADMIN_IDS empty: onboarding only; purchase is disabled until admin ID is configured.')
    db = TelegramStore(path)
    db.initialize()
    if args.migrate_only:
        print('Telegram tables initialized without touching Bale data.')
        return
    api = TelegramAPI(TELEGRAM_BOT_TOKEN)
    if args.remove_webhook:
        api.call('deleteWebhook', {'drop_pending_updates': False})
        print('Webhook removed; pending updates preserved.')
    app = Storefront(db, api, admin_ids=admins, card_number=CARD_NUMBER,
                     card_holder=CARD_HOLDER,
                     support_contact=SUPPORT)
    runner=Runner(app, api, Path(os.getenv('TG_OFFSET_PATH') or (ROOT / 'telegram_last_update.txt')))
    try:
        runner.run()
    finally:
        runner.shutdown()


if __name__ == '__main__':
    main()
