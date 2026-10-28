"""Telegram-only pure storefront presentation for Internet Azad | Pro.

No bot token, no database connection, and no side effects. All dynamic values
are HTML-escaped; messages use only a small number of visually meaningful icons.
"""
from __future__ import annotations

import html
from customer_experience import SUPPORT, BALE_USERNAME, money, plan_volume, order_ref, jalali_datetime, card_for_order

SEPARATOR = '━━━━━━━━━━━━━━'
STATUS = {
    'awaiting_receipt': '🧾 منتظر پرداخت',
    'under_review': '⏳ در حال بررسی رسید',
    'approved': '✅ تأییدشده',
    'rejected': '❌ تأییدنشده',
    'cancelled': '🚫 لغوشده',
}


def esc(value):
    return html.escape(str(value if value is not None else ''), quote=False)


def home():
    return (f'🌐 <b>اینترنت آزاد | پرو</b>\n{SEPARATOR}\n'
            'سرویس‌های VPN برای عبور از محدودیت‌های اینترنتی، با قیمت شفاف و خرید آسان.\n\n'
            '🛡 مناسب شرایط محدودیت و اختلال اینترنت\n'
            '📱 قابل استفاده روی دستگاه‌ها و اینترنت‌های رایج\n'
            '⏱ بررسی رسید پرداخت: حداکثر 10 دقیقه\n\n'
            '👇 برای دیدن بسته‌ها و قیمت‌ها، <b>«خرید سرویس»</b> را انتخاب کنید.\n\n'
            f'💬 {SUPPORT}  |  🟢 بله: {BALE_USERNAME}')


def catalog(categories_count):
    return ('🛍 <b>خرید سرویس VPN</b>\n' + SEPARATOR + '\n'
            f'{categories_count} دسته سرویس در دسترس است.\n'
            'برای دیدن بسته‌ها و قیمت‌ها، یکی از دسته‌ها را انتخاب کنید.')


def category(name, items, available_count):
    return (f'📦 <b>{esc(name)}</b>\n{SEPARATOR}\n'
            f'{items} بسته | {available_count} بسته قابل سفارش\n\n'
            'قیمت و مشخصات هر بسته را پیش از ثبت سفارش ببینید.')


def plan(name, category_name, volume, users, price, *, duration=None, available=True, reason=None, single=False):
    lines = [f'✨ <b>{esc(name)}</b>', SEPARATOR,
             f'🌐 نوع سرویس: {esc(category_name)}',
             f'📦 حجم: {esc(volume)}']
    if duration:
        lines.append(f'📅 مدت اعتبار: {esc(duration)}')
    lines += [f'👤 کاربران: {esc(users)}',
              '', f'💰 <b>{money(price)} تومان</b>', '',
              '✅ آماده ثبت سفارش' if available else '⛔ در حال حاضر ناموجود']
    if single:
        lines += ['', 'ℹ️ این بسته مخصوص یک نفر است؛ مصرف منصفانه: 100 گیگابایت.']
    if not available and reason:
        lines.append(esc(str(reason)[:250]))
    return '\n'.join(lines)


def invoice(ref, plan_name, amount, card_number, holder):
    return (f'✅ <b>سفارش شما ثبت شد</b>\n{SEPARATOR}\n'
            f'🧾 شماره سفارش: <code>{esc(ref)}</code>\n'
            f'📦 سرویس: {esc(plan_name)}\n'
            f'💰 مبلغ پرداخت: <b>{money(amount)} تومان</b>\n\n'
            f'💳 <b>شماره کارت برای واریز:</b>\n<code>{esc(card_number)}</code>\n'
            f'👤 به نام: {esc(holder)}\n\n'
            '1. مبلغ را به کارت بالا واریز کنید.\n'
            '2. دکمه «ارسال رسید» را بزنید و تصویر رسید را بفرستید.\n\n'
            f'⏱ بررسی رسید: حداکثر 10 دقیقه\n'
            f'💬 پشتیبانی: {SUPPORT}')


def order_list(n):
    if not n:
        return ('📦 <b>سفارش‌های من</b>\n' + SEPARATOR + '\n'
                'هنوز سفارشی ثبت نکرده‌اید. از بخش خرید، سرویس موردنظر را انتخاب کنید.')
    return (f'📦 <b>سفارش‌های من</b>\n{SEPARATOR}\n'
            'برای مشاهده جزئیات و وضعیت پرداخت، روی شماره سفارش بزنید.')


def order_details(order, plan_name):
    oid = order['id']
    value = (f'🧾 <b>سفارش {order_ref(oid)}</b>\n{SEPARATOR}\n'
             f'📦 سرویس: {esc(plan_name)}\n'
             f'💰 مبلغ: {money(order["amount"])} تومان\n'
             f'📍 وضعیت: {STATUS.get(order["status"], "نامشخص")}\n'
             f'📅 زمان ثبت: {esc(jalali_datetime(order["created_at"]))}')
    if order['status'] == 'approved' and order.get('service_link'):
        value += ('\n\n🔗 <b>اطلاعات اتصال</b>\n'
                  '<code>' + esc(order['service_link'])[:1900] + '</code>')
    if order['status'] == 'under_review':
        value += '\n\n⏱ رسید دریافت شده و در صف بررسی است (حداکثر 10 دقیقه).'
    if order['status'] == 'rejected':
        value += f'\n\nبرای پیگیری وضعیت پرداخت با پشتیبانی تماس بگیرید: {SUPPORT}'
    return value


def profile(user, stats, last_order):
    display_name = esc((user or {}).get('first_name') or 'کاربر')
    user_id = (user or {}).get('user_id', '')
    result = (f'👤 <b>حساب کاربری</b>\n{SEPARATOR}\n'
              f'نام: {display_name}\n'
              f'🆔 شناسه: <code>{esc(user_id)}</code>\n\n'
              f'📦 کل سفارش‌ها: {stats["total"]}\n'
              f'✅ سفارش‌های تأییدشده: {stats["approved"]}\n'
              f'⏳ رسیدهای در حال بررسی: {stats["reviewing"]}')
    if last_order:
        result += (f'\n\n<b>آخرین سفارش</b>\n'
                   f'<code>{order_ref(last_order["id"])}</code>  |  '
                   f'{STATUS.get(last_order["status"], "نامشخص")}')
    result += '\n\nبرای پیگیری سفارش یا خرید سرویس جدید از گزینه‌های زیر استفاده کنید.'
    return result


def guide():
    return (f'📖 <b>راهنمای خرید</b>\n{SEPARATOR}\n'
            '1️⃣ سرویس مناسب را از بخش خرید انتخاب کنید.\n'
            '2️⃣ مشخصات و قیمت را بررسی و سفارش را ثبت کنید.\n'
            '3️⃣ مبلغ را واریز کنید و روی «ارسال رسید» بزنید.\n'
            '4️⃣ پس از تأیید پرداخت، مشخصات اتصال برایتان ارسال می‌شود.\n\n'
            '⏱ بررسی رسید: حداکثر 10 دقیقه\n'
            '🔐 اطلاعات اتصال را در اختیار دیگران نگذارید.')
