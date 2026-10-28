import requests
import json
import time
import os
import logging
import threading
import sqlite3
import random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, FIRST_COMPLETED, wait
from database import Database
from config import BOT_TOKEN, CARD_NUMBER, CARD_HOLDER, MASTER_ADMIN_ID
from service_catalog import PLANS, CatalogStore
from bale_catalog_admin import BaleCatalogAdmin
from bale_admin import BaleAdmin
from bale_broadcast import BaleBroadcast
from customer_experience import (SUPPORT, card_for_order, order_ref, jalali_datetime,
    money, purchase_text, single_user_notice, latin_digits, welcome_text, customer_announcement)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

API_URL = f"https://tapi.bale.ai/bot{BOT_TOKEN}"
db = Database(os.path.join(os.path.dirname(__file__), "bot_data.db"))
bale_services_admin = BaleCatalogAdmin(db, lambda uid,text,reply_markup=None: send_message(uid,text,reply_markup))
bale_admin = BaleAdmin(db,lambda uid,text,reply_markup=None: send_message(uid,text,reply_markup),
                       lambda uid,file_id,caption=None: send_photo(uid,file_id,caption=caption))
OFFSET_FILE = os.getenv("BALE_OFFSET_PATH") or "last_update.txt"
session = requests.Session()
session.trust_env = False
session.proxies = {}
PROCESSED_IDS = set()
MAX_PROCESSED_IDS = 5000

# ----------------------------------------------------------------------
# توابع کمکی
# ----------------------------------------------------------------------
def set_user_state(user_id, state, data=None):
    db.set_user_state(user_id, state, data)

def get_user_state(user_id):
    return db.get_user_state(user_id)

def delete_user_state(user_id):
    db.delete_user_state(user_id)

def send_message(chat_id, text, reply_markup=None):
    url = f"{API_URL}/sendMessage"
    data = {"chat_id": chat_id, "text": latin_digits(text)}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        response = session.post(url, json=data, timeout=10)
        result = response.json()
        if not result.get('ok'):
            error_desc = result.get('description', '')
            if 'blocked' in error_desc.lower():
                db.add_blocked_user(chat_id)
                logger.warning(f"کاربر {chat_id} ربات را بلاک کرد.")
        return result
    except Exception as e:
        logger.warning("send_message failed: %s", type(e).__name__)
        return None

def send_photo(chat_id, photo_id, caption=None):
    url = f"{API_URL}/sendPhoto"
    data = {"chat_id": chat_id, "photo": photo_id}
    if caption:
        data["caption"] = caption
    try:
        response = session.post(url, json=data, timeout=10)
        return response.json()
    except Exception as e:
        logger.warning("send_photo failed: %s", type(e).__name__)
        return None

# Separate HTTP session for broadcast workers: do not share the polling session.
_broadcast_http_local = threading.local()
def _broadcast_api(method, payload):
    sender = getattr(_broadcast_http_local, 'session', None)
    if sender is None:
        sender = requests.Session()
        sender.trust_env = False
        _broadcast_http_local.session = sender
    try:
        reply = sender.post(f'{API_URL}/{method}', json=payload, timeout=12)
        return reply.json()
    except Exception as exc:
        logger.warning('Bale broadcast %s uncertain response: %s', method, type(exc).__name__)
        return None

def broadcast_send_message(chat_id, text, reply_markup=None):
    payload = {'chat_id': chat_id, 'text': latin_digits(text)}
    if reply_markup is not None:
        payload['reply_markup'] = json.dumps(reply_markup)
    return _broadcast_api('sendMessage', payload)

def broadcast_send_photo(chat_id, photo_id, caption=None):
    payload = {'chat_id': chat_id, 'photo': photo_id}
    if caption: payload['caption'] = caption
    return _broadcast_api('sendPhoto', payload)

# SQLite-persisted broadcast queue; starts only when bot.main() runs.
bale_broadcast = BaleBroadcast(db, broadcast_send_message, broadcast_send_photo)
bale_admin.broadcast_controller = bale_broadcast

# ----------------------------------------------------------------------
# توابع مدیریت فیش با توضیحات
# ----------------------------------------------------------------------
def ask_for_description(chat_id, order_id, photo_id):
    text = """
✅ تصویر رسید دریافت شد.

اگر توضیحی درباره پرداخت دارید، می‌توانید آن را اضافه کنید؛ در غیر این صورت رسید را برای بررسی ارسال بفرمایید.

⏱ زمان بررسی پس از ثبت نهایی رسید: حداکثر ۱۰ دقیقه.
"""
    keyboard = {
        "inline_keyboard": [
            [{"text": "📤 ارسال فیش بدون توضیح", "callback_data": f"send_receipt_no_desc_{order_id}"}],
            [{"text": "✏️ افزودن توضیحات", "callback_data": f"add_receipt_desc_{order_id}"}]
        ]
    }
    send_message(chat_id, text, reply_markup=keyboard)
    set_user_state(chat_id, "waiting_receipt_decision", {"order_id": order_id, "photo_id": photo_id})

def send_receipt_to_admins(order_id, description=None):
    order = db.get_order_by_id(order_id)
    if not order:
        return False
    user_id = order[1]
    photo_id = order[6]
    plan = PLANS.get(order[2], {})
    admin_text = f"""
🔔 سفارش جدید {order_ref(order[0])}
👤 کاربر: {user_id}
🏷️ پنل: {plan.get('type','نامشخص')} - {plan.get('display','')}
💰 مبلغ: {money(order[3])} تومان
"""
    if description:
        admin_text += f"\n📝 توضیحات کاربر:\n{description}"

    for admin_id, _, _ in db.get_all_admins():
        send_message(admin_id, admin_text)
        if photo_id:
            send_photo(admin_id, photo_id, caption=f"رسید سفارش {order_ref(order[0])}")
        send_message(admin_id, "لطفاً سفارش را بررسی کنید:", reply_markup=admin_order_menu(order_id))
    return True

def admin_order_menu(order_id):
    return {"inline_keyboard": [[
        {"text": "✅ تأیید", "callback_data": f"approve_{order_id}"},
        {"text": "❌ رد", "callback_data": f"reject_{order_id}"}
    ]]}

# ----------------------------------------------------------------------
# منوها
# ----------------------------------------------------------------------
def main_menu():
    """Customer menu: VPN plans and purchase support only."""
    return {
        'keyboard': [
            [{'text': '🛍 خرید سرویس'}],
            [{'text': '📦 سفارش‌های من'}, {'text': '👤 حساب کاربری'}],
            [{'text': '💬 پشتیبانی'}, {'text': '📖 راهنمای خرید'}],
            [{'text': '📢 اطلاعیه‌ها'}],
        ],
        'resize_keyboard': True,
    }

def receipt_upload_menu(order_id):
    # Just one action on the payment screen, as requested.
    return {'inline_keyboard': [[{'text': '📤 ارسال رسید',
                                 'callback_data': f'receipt_upload_{int(order_id)}'}]]}


def panel_type_menu():
    """Show only categories backed by real plans; no dead buttons."""
    type_to_callback = {
        'طلایی': ('🥇 طلایی', 'panel_gold'),
        'بین‌الملل': ('🌍 بین‌الملل', 'panel_international'),
        'الترا': ('⚡ الترا', 'panel_ultra'),
        'تک کاربره نامحدود': ('♾ نامحدود اختصاصی', 'panel_unlimited_single'),
    }
    active = {plan.get('type') for plan in PLANS.values()}
    options = [{'text': title, 'callback_data': cb}
               for typ, (title, cb) in type_to_callback.items() if typ in active]
    category_ids={x['name']:x['id'] for x in CatalogStore(db.db_name).categories()}
    for typ in active:
        if typ not in type_to_callback and typ in category_ids:
            options.append({'text':'🔹 '+typ,'callback_data':f'panel_group:{category_ids[typ]}'})
    rows = [options[i:i+2] for i in range(0, len(options), 2)]
    rows.append([{'text': '🏠 بازگشت به منوی اصلی', 'callback_data': 'back_main'}])
    return {'inline_keyboard': rows}

def plans_menu(panel_type):
    """Keep sold-out products as no-op buttons with 'ناموجود' instead of price."""
    keyboard={'inline_keyboard':[]}
    types={'gold':'طلایی','international':'بین‌الملل',
           'ultra':'الترا','unlimited':'تک کاربره نامحدود'}
    status=db.get_all_plans_status()
    category=panel_type[len('custom:'):] if panel_type.startswith('custom:') else types.get(panel_type)
    for pid,info in PLANS.items():
        if info.get('type') != category:continue
        available=status.get(pid,{}).get('available',False)
        label=f"✅ {info['display']} | {info['price']:,} تومان" if available else f"⛔ {info['display']} | ناموجود"
        cb=f'plan_{pid}' if available else f'unavailable_{pid}'
        keyboard['inline_keyboard'].append([{'text':label,'callback_data':cb}])
    keyboard['inline_keyboard'].append([{'text':'🔙 بازگشت به انتخاب پنل',
                                         'callback_data':'back_panel_select'}])
    return keyboard


def plans_description(panel_type,heading):
    return heading+'\n\nلطفاً سرویس موردنظر را انتخاب کنید.'


def receipt_stock_notice():
    return ('⛔ این سرویس در حال حاضر ناموجود است و امکان ثبت رسید جدید ندارد.\n'
            f'اگر قبلاً پرداخت کرده‌اید، برای پیگیری پرداخت یا بازگشت وجه با پشتیبانی {SUPPORT} تماس بگیرید.')

def my_orders_menu(chat_id):
    orders = db.get_user_orders(chat_id)
    if not orders:
        return None
    keyboard = {"inline_keyboard": []}
    status_emoji = {'pending': '⏳', 'approved': '✅', 'rejected': '❌', 'cancelled': '🚫'}
    for order in orders[:10]:
        plan = PLANS.get(order[2], {})
        panel_name = db.order_title(order[0],order[2])
        emoji = status_emoji.get(order[4], '❓')
        keyboard["inline_keyboard"].append([{
            "text": f"{emoji} {order_ref(order[0])} - {panel_name}",
            "callback_data": f"order_{order[0]}"
        }])
    keyboard["inline_keyboard"].append([{"text": "🔙 بازگشت", "callback_data": "back_main"}])
    return keyboard

def cancel_order_menu(order_id):
    return {"inline_keyboard": [[
        {"text": "✅ بله، لغو شود", "callback_data": f"cancel_yes_{order_id}"},
        {"text": "❌ خیر", "callback_data": "back_orders"}
    ]]}

# ----------------------------------------------------------------------
# مدیریت کال‌بک‌ها
# ----------------------------------------------------------------------
def handle_callback_query(callback_query):
    chat_id = callback_query['message']['chat']['id']
    if not db.is_admin(chat_id) and db.is_user_blocked_by_admin(chat_id):
        return
    data = callback_query['data']
    if db.get_user_info(chat_id, use_cache=False):
        bale_admin.touch(chat_id)
    if data.startswith(('badmin:', 'bbr:')):
        # Admin operations must originate from an authenticated private user.
        actor = callback_query.get('from', {}).get('id')
        if actor != chat_id or not db.is_admin(actor):
            return
        try:
            session.post(f'{API_URL}/answerCallbackQuery',
                         json={'callback_query_id': callback_query['id']}, timeout=4)
        except Exception:
            pass
        if data.startswith('bbr:'):
            return bale_broadcast.callback(chat_id, data)
        return bale_admin.callback(chat_id, data)
    if data.startswith(('adm:cat','adm:ct')) or (data=='adm:home' and db.is_admin(chat_id)):
        if not db.is_admin(chat_id):return
        if data=='adm:home':return bale_services_admin.admin_home(chat_id)
        return bale_services_admin.catalog_callback(chat_id,data)
    try:
        session.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": callback_query['id']}, timeout=5)
    except:
        pass

    # Old non-VPN menus can remain in users' message history: do not execute them.
    if (data.startswith('virtual_') or data in {
        'representative_contact', 'diff_coding_vs_template', 'contact_admin',
        'website_service_back', 'virtual_numbers_menu', 'representative_service'
    }):
        send_message(chat_id, 'در این ربات فقط سرویس‌های VPN ارائه می‌شود. برای مشاهده بسته‌ها از بخش خرید استفاده بفرمایید.',
                     reply_markup=main_menu())
        return

    if data.startswith('receipt_upload_'):
        raw = data[len('receipt_upload_'):]
        if not raw.isdigit():
            return
        order_id = int(raw)
        order = db.get_order_by_id(order_id)
        if not order or order[1] != chat_id or order[4] != 'pending' or order[6]:
            send_message(chat_id, 'این سفارش امکان دریافت رسید جدید ندارد. لطفاً از بخش سفارش‌های من وضعیت آن را بررسی کنید.')
            return
        if not db.receipt_service_available(order_id,chat_id):
            delete_user_state(chat_id)
            send_message(chat_id,receipt_stock_notice())
            return
        set_user_state(chat_id, 'waiting_receipt', {'order_id': order_id})
        send_message(chat_id, f'📤 لطفاً تصویر رسید پرداخت سفارش {order_ref(order_id)} را همین‌جا ارسال کنید.')
        return

    # ====== مدیریت تصمیم رسید ======
    if data.startswith("send_receipt_no_desc_"):
        order_id = int(data.split("_")[-1])
        state, state_data = get_user_state(chat_id)
        order = db.get_order_by_id(order_id)
        if not (order and order[1] == chat_id and order[4] == 'pending'
                and order[6] and state == 'waiting_receipt_decision'
                and state_data and state_data.get('order_id') == order_id):
            send_message(chat_id, '⚠️ این رسید متعلق به سفارش فعال شما نیست.')
            return
        send_receipt_to_admins(order_id, description=None)
        send_message(chat_id, '✅ رسید شما برای بررسی ثبت شد.\n⏱ زمان بررسی: حداکثر ۱۰ دقیقه.')
        delete_user_state(chat_id)
        return

    if data.startswith("add_receipt_desc_"):
        order_id = int(data.split("_")[-1])
        state, state_data = get_user_state(chat_id)
        order = db.get_order_by_id(order_id)
        if (order and order[1] == chat_id and order[4] == 'pending'
                and state == "waiting_receipt_decision" and state_data
                and state_data.get("order_id") == order_id):
            set_user_state(chat_id, "waiting_receipt_description", {"order_id": order_id, "photo_id": state_data["photo_id"]})
            send_message(chat_id, "📝 لطفاً توضیحات خود را (حداکثر ۵۰۰ کاراکتر) وارد کنید.\nبرای لغو، /cancel را بزنید.")
        else:
            send_message(chat_id, "❌ خطا در شناسایی سفارش. لطفاً دوباره تلاش کنید.")
        return

    # ====== پنل نامحدود تک کاربره ======
    if data.startswith('panel_group:'):
        try:category_id=int(data.split(':',1)[1])
        except ValueError:return
        row=CatalogStore(db.db_name).category(category_id)
        if not row or row['deleted'] or row['name'] not in {p.get('type') for p in PLANS.values()}:
            return send_message(chat_id,'این دسته دیگر در فروشگاه موجود نیست.',reply_markup=panel_type_menu())
        return send_message(chat_id,plans_description('custom:'+row['name'],'🔹 '+row['name']),
                            reply_markup=plans_menu('custom:'+row['name']))
    if data == "panel_unlimited_single" and len([p for p in PLANS.values() if p.get('type')=='تک کاربره نامحدود'])>1:
        send_message(chat_id, plans_description('unlimited','♾ سرویس‌های نامحدود تک‌کاربره'), reply_markup=plans_menu('unlimited'))
        return
    if data == "panel_unlimited_single":
        available, _ = db.get_plan_status("UNLIMITED_SINGLE")
        if not available:
            send_message(chat_id, 'این سرویس در حال حاضر موجود نیست. لطفاً سرویس دیگری انتخاب بفرمایید.')
            return
        set_user_state(chat_id, 'ack_unlimited', {'plan_id': 'UNLIMITED_SINGLE'})
        send_message(chat_id, single_user_notice(), reply_markup={
            'inline_keyboard': [
                [{'text': '✅ متوجه شدم، ادامه خرید', 'callback_data': 'unlimited_single_buy'}],
                [{'text': '🔙 انتخاب سرویس دیگر', 'callback_data': 'back_panel_select'}],
            ]})
        return

    if data == "unlimited_single_buy" or data.startswith('unlimited_single_buy_'):
        candidate=data[len('unlimited_single_buy_'):] if data!='unlimited_single_buy' else 'UNLIMITED_SINGLE'
        state, state_data = get_user_state(chat_id)
        if state != 'ack_unlimited' or not state_data or state_data.get('plan_id') != candidate:
            send_message(chat_id, 'لطفاً ابتدا شرایط سرویس تک‌کاربره را مطالعه و تأیید کنید.',
                         reply_markup=panel_type_menu())
            return
        plan_id = candidate
        plan = PLANS.get(plan_id)
        if not plan:
            return send_message(chat_id, 'این سرویس در حال حاضر قابل سفارش نیست.')
        available, _ = db.get_plan_status(plan_id)
        if not available:
            return send_message(chat_id, 'این سرویس در حال حاضر موجود نیست.')
        order_id = db.create_order(chat_id, plan_id, plan['price'])
        order = db.get_order_by_id(order_id)
        card, holder = card_for_order(order[8] if len(order)>8 else None, CARD_NUMBER, CARD_HOLDER)
        send_message(chat_id, purchase_text(ref=order_ref(order_id), plan=plan,
                                            price=plan['price'], card_number=card, card_holder=holder),
                     reply_markup=receipt_upload_menu(order_id))
        set_user_state(chat_id, 'receipt_pending', {'order_id':order_id})
        return

    # ====== منوهای خرید ======
    if data == "panel_gold":
        send_message(chat_id, plans_description('gold','🥇 سرویس‌های طلایی'), reply_markup=plans_menu("gold"))
    elif data == "panel_international":
        send_message(chat_id, plans_description('international','🌍 سرویس‌های بین‌الملل'), reply_markup=plans_menu("international"))
    elif data == "panel_ultra":
        send_message(chat_id, plans_description('ultra','⚡ سرویس‌های الترا'), reply_markup=plans_menu("ultra"))
    elif data == "back_panel_select":
        send_message(chat_id, "لطفاً نوع سرویس موردنظر خود را انتخاب بفرمایید:", reply_markup=panel_type_menu())
    elif data.startswith("unavailable_"):
        # Already acknowledged: no navigation and no order from sold-out button.
        return
    elif data.startswith("plan_"):
        plan_id = data[5:]
        if plan_id in PLANS and (plan_id == 'UNLIMITED_SINGLE' or PLANS[plan_id].get('type')=='تک کاربره نامحدود'):
            # Old/forged callbacks must never skip the mandatory acknowledgment.
            set_user_state(chat_id, 'ack_unlimited', {'plan_id': plan_id})
            return send_message(chat_id, single_user_notice(), reply_markup={
                'inline_keyboard': [
                    [{'text': '✅ متوجه شدم، ادامه خرید', 'callback_data':('unlimited_single_buy' if plan_id=='UNLIMITED_SINGLE' else f'unlimited_single_buy_{plan_id}')}],
                    [{'text': '🔙 بازگشت', 'callback_data':'back_panel_select'}],
                ]})
        if plan_id in PLANS:
            available, _ = db.get_plan_status(plan_id)
            if not available:
                send_message(chat_id, "❌ این پلن در حال حاضر موجود نیست.")
                return
            plan = PLANS[plan_id]
            order_id = db.create_order(chat_id, plan_id, plan['price'])
            order = db.get_order_by_id(order_id)
            card, holder = card_for_order(order[8] if len(order)>8 else None, CARD_NUMBER, CARD_HOLDER)
            text = purchase_text(ref=order_ref(order_id), plan=plan, price=plan['price'],
                                 card_number=card, card_holder=holder)
            send_message(chat_id, text, reply_markup=receipt_upload_menu(order_id))
            set_user_state(chat_id, "receipt_pending", {"order_id": order_id})
    elif data == "back_main":
        send_message(chat_id, "از منوی زیر بخش موردنظر خود را انتخاب بفرمایید:", reply_markup=main_menu())
    elif data == "back_orders":
        orders = db.get_user_orders(chat_id)
        if orders:
            send_message(chat_id, "📦 سفارش‌های شما\n\nبرای مشاهده جزئیات، یک سفارش را انتخاب بفرمایید:", reply_markup=my_orders_menu(chat_id))
        else:
            send_message(chat_id, "هنوز سفارشی ثبت نکرده‌اید. برای شروع، بخش خرید را انتخاب بفرمایید.", reply_markup=main_menu())
    elif data.startswith("order_"):
        oid = int(data[6:])
        order = db.get_order_by_id(oid)
        if order and order[1] == chat_id:
            plan = PLANS.get(order[2], {})
            status_text = {'pending':'⏳ در انتظار بررسی', 'approved':'✅ تأیید', 'rejected':'❌ رد', 'cancelled':'🚫 لغو'}
            text = f"""
📦 جزئیات سفارش {order_ref(order[0])}

🏷️ پنل: {plan.get('type','نامشخص')}
📦 بسته: {db.order_title(order[0],order[2])}
👥 نوع: {plan.get('users','نامشخص')}
📊 وضعیت: {status_text.get(order[4], 'نامشخص')}
💰 مبلغ: {money(order[3])} تومان
📅 تاریخ: {jalali_datetime(order[5])}
"""
            if order[4] == 'pending':
                kb = {"inline_keyboard": [
                    [{"text": "📤 ارسال رسید", "callback_data": f"receipt_upload_{oid}"}],
                    [{"text": "🚫 لغو سفارش", "callback_data": f"cancel_confirm_{oid}"}],
                    [{"text": "🔙 بازگشت", "callback_data": "back_orders"}]
                ]}
                send_message(chat_id, text, reply_markup=kb)
            else:
                kb = {"inline_keyboard": [[{"text": "🔙 بازگشت", "callback_data": "back_orders"}]]}
                send_message(chat_id, text, reply_markup=kb)
    elif data.startswith("cancel_confirm_"):
        oid = int(data[15:])
        order = db.get_order_by_id(oid)
        if not order or order[1] != chat_id or order[4] != 'pending':
            send_message(chat_id, '⚠️ این سفارش متعلق به شما نیست یا قابل لغو نیست.')
            return
        send_message(chat_id, "⚠️ آیا از لغو این سفارش اطمینان دارید؟", reply_markup=cancel_order_menu(oid))
    elif data.startswith("cancel_yes_"):
        oid = int(data[11:])
        if db.cancel_order(oid, chat_id):
            send_message(chat_id, '✅ سفارش شما لغو شد.', reply_markup=main_menu())
        else:
            send_message(chat_id, '⚠️ سفارش متعلق به شما نیست یا قبلاً رسیدگی شده است.', reply_markup=main_menu())
    elif data.startswith("approve_"):
        if db.is_admin(chat_id):
            oid = int(data[8:])
            order = db.get_order_by_id(oid)
            if order and order[4] == 'pending' and order[6]:
                set_user_state(chat_id, "send_link", {"order_id": oid})
                send_message(chat_id, f"لطفاً لینک اتصال سفارش {order_ref(oid)} را ارسال کنید:")
            else:
                send_message(chat_id, f"سفارش {order_ref(oid)} پیدا نشد.")
    elif data.startswith("reject_"):
        if db.is_admin(chat_id):
            oid = int(data[7:])
            changed = db.reject_order(oid)
            order = db.get_order_by_id(oid)
            if changed and order:
                send_message(order[1], f"نتیجه بررسی سفارش {order_ref(oid)}: پرداخت تأیید نشد.\nبرای پیگیری لطفاً به {SUPPORT} پیام دهید.")
                send_message(chat_id, f"سفارش {order_ref(oid)} رد شد.")
            else:
                send_message(chat_id, '⚠️ سفارش قبلاً رسیدگی شده یا رسید معتبری ندارد.')

# ----------------------------------------------------------------------
# مدیریت پیام‌های متنی و عکس
# ----------------------------------------------------------------------
def handle_message(message):
    chat_id = message['chat']['id']
    if not db.is_admin(chat_id) and db.is_user_blocked_by_admin(chat_id):
        return

    username = message['chat'].get('username', 'بدون نام کاربری')
    first_name = message['chat'].get('first_name', 'کاربر')
    last_name = message['chat'].get('last_name')
    db.save_user(chat_id, username, first_name, last_name)
    bale_admin.touch(chat_id)

    if 'text' in message and db.is_admin(chat_id):
        text_input=message['text'].strip()
        if text_input=='/admin':
            return bale_admin.home(chat_id)
        if text_input=='/broadcast':
            return bale_broadcast.home(chat_id)
        state_now, _ = get_user_state(chat_id)
        if text_input=='/cancel' and state_now in ('bbr_waiting','bbr_confirm'):
            return bale_broadcast.cancel(chat_id)
        if state_now=='bbr_waiting':
            return bale_broadcast.capture(chat_id,text=text_input)
        if text_input=='/services':
            delete_user_state(chat_id)
            bale_services_admin.catalog_list(chat_id)
            return
        if text_input=='/cancel' and (get_user_state(chat_id)[0] or '').startswith('catalog_'):
            delete_user_state(chat_id)
            bale_services_admin.catalog_list(chat_id)
            return
        if bale_admin.text(chat_id, text_input):
            return
        if bale_services_admin.catalog_text(chat_id,text_input):
            return

    if 'text' in message:
        text = message['text']
        state, state_data = get_user_state(chat_id)
        if state:
            if state == "send_link" and db.is_admin(chat_id):
                order_id = state_data['order_id']
                service_link = text.strip()
                try:
                    changed = db.approve_order(order_id, service_link)
                    if not changed:
                        send_message(chat_id, '⚠️ این سفارش قبلاً رسیدگی شده یا رسید ندارد؛ لینکی ارسال نشد.')
                        delete_user_state(chat_id)
                        return
                    logger.info("Order %s approved", order_ref(order_id))
                except Exception as e:
                    send_message(chat_id, f"❌ خطا در تأیید سفارش: {e}")
                    delete_user_state(chat_id)
                    return
                order = db.get_order_by_id(order_id)
                if order:
                    user_id = order[1]
                    res = send_message(user_id, f"""
✅ سفارش {order_ref(order_id)} تأیید شد.

🔗 لینک اتصال شما:
{service_link}

برای استفاده، لینک را در برنامه سازگار وارد کنید.
از اعتماد شما سپاسگزاریم. 🌷
""")
                    if res and res.get('ok'):
                        send_message(chat_id, f"لینک سفارش {order_ref(order_id)} برای مشتری ارسال شد.")
                    else:
                        send_message(chat_id, f"ارسال لینک ناموفق بود؛ لطفاً مجدداً بررسی کنید.")
                delete_user_state(chat_id)
                return
            elif state == "waiting_broadcast" and db.is_admin(chat_id):
                # Legacy in-flight wizard: never bypass preview and confirmation.
                delete_user_state(chat_id)
                bale_broadcast.begin(chat_id)
                send_message(chat_id,'لطفاً پیام را دوباره برای پیش‌نمایش ارسال کنید.')
                return
            elif state == "waiting_receipt" and state_data:
                pass
            elif state == "waiting_receipt_description" and state_data:
                description = text.strip()
                if len(description) > 500:
                    send_message(chat_id, "❌ توضیحات نمی‌تواند بیشتر از ۵۰۰ کاراکتر باشد. لطفاً کوتاه‌تر بنویسید.")
                    return
                order_id = state_data.get("order_id")
                photo_id = state_data.get("photo_id")
                if order_id and photo_id:
                    if not db.save_receipt(order_id, photo_id, chat_id):
                        send_message(chat_id, '⚠️ رسید برای این سفارش قابل ثبت نیست.')
                        delete_user_state(chat_id)
                        return
                    send_receipt_to_admins(order_id, description=description)
                    send_message(chat_id, "✅ فیش شما همراه با توضیحات برای ادمین ارسال شد.")
                    delete_user_state(chat_id)
                else:
                    send_message(chat_id, "❌ خطا در شناسایی سفارش. لطفاً دوباره تلاش کنید.")
                return

        # ========== دستورات ادمین ==========
        if db.is_admin(chat_id) and text.startswith('/'):
            if chat_id == MASTER_ADMIN_ID:
                if text.startswith('/add_admin '):
                    parts = text.split()
                    if len(parts) != 2:
                        send_message(chat_id, "❌ فرمت: `/add_admin user_id`")
                        return
                    try:
                        new_admin_id = int(parts[1])
                        if db.add_admin(new_admin_id, chat_id):
                            send_message(chat_id, f"✅ کاربر {new_admin_id} به لیست ادمین‌ها اضافه شد.")
                            send_message(new_admin_id, "🎉 شما به عنوان ادمین ربات اضافه شدید. اکنون می‌توانید از دستورات مدیریتی استفاده کنید.")
                        else:
                            send_message(chat_id, "❌ کاربر قبلاً ادمین است یا خطایی رخ داده.")
                    except:
                        send_message(chat_id, "❌ شناسه نامعتبر.")
                    return

                if text.startswith('/remove_admin '):
                    parts = text.split()
                    if len(parts) != 2:
                        send_message(chat_id, "❌ فرمت: `/remove_admin user_id`")
                        return
                    try:
                        target = int(parts[1])
                        if target == MASTER_ADMIN_ID:
                            send_message(chat_id, "❌ نمی‌توانید ادمین اصلی را حذف کنید.")
                            return
                        if db.remove_admin(target):
                            send_message(chat_id, f"✅ کاربر {target} از لیست ادمین‌ها حذف شد.")
                            send_message(target, "⚠️ دسترسی ادمین شما لغو شد.")
                        else:
                            send_message(chat_id, "❌ کاربر ادمین نیست یا حذف نشد.")
                    except:
                        send_message(chat_id, "❌ شناسه نامعتبر.")
                    return

                if text == '/admins_list':
                    admins = db.get_all_admins()
                    if not admins:
                        send_message(chat_id, "❌ هیچ ادمینی یافت نشد.")
                    else:
                        msg = "📋 **لیست ادمین‌ها:**\n\n"
                        for uid, added_by, added_at in admins:
                            info = db.get_user_info(uid)
                            name = info.get('first_name', 'نامشخص') if info else 'نامشخص'
                            master_tag = " (ادمین اصلی)" if uid == MASTER_ADMIN_ID else ""
                            msg += f"🆔 {uid} - {name}{master_tag}\n"
                            msg += f"   اضافه شده توسط: {added_by}\n"
                            msg += f"   تاریخ: {added_at}\n\n"
                        send_message(chat_id, msg)
                    return

                if text == '/block_all_blocked':
                    count = db.block_all_blocked_users(chat_id)
                    send_message(chat_id, f"✅ {count} کاربر که ربات را بلاک کرده بودند، به لیست بلاک ادمین اضافه شدند.")
                    return

            # دستورات قابل استفاده توسط همه ادمین‌ها
            if text == '/announce':
                db.clear_announcement()
                send_message(chat_id, "✅ اطلاعیه حذف شد.")
                return
            if text.startswith('/announce '):
                msg = text[10:].strip()
                if msg:
                    db.set_announcement(msg)
                    send_message(chat_id, "✅ اطلاعیه با موفقیت ثبت شد.")
                else:
                    db.clear_announcement()
                    send_message(chat_id, "✅ اطلاعیه حذف شد.")
                return

            if text.startswith('/plan_status '):
                parts = text.split(maxsplit=3)
                if len(parts) < 3:
                    send_message(chat_id, "❌ فرمت: `/plan_status plan_id available|not_available [message]`")
                    return
                plan_id = parts[1]
                if plan_id not in PLANS:
                    send_message(chat_id, f"❌ پلن {plan_id} معتبر نیست.")
                    return
                status = parts[2].lower()
                message = parts[3] if len(parts) > 3 else None
                if status == 'available':
                    db.set_plan_availability(plan_id, True, admin_id=chat_id)
                    send_message(chat_id, f"✅ پلن {plan_id} اکنون موجود است.")
                elif status == 'not_available':
                    db.set_plan_availability(plan_id, False, message, admin_id=chat_id)
                    send_message(chat_id, f"✅ پلن {plan_id} ناموجود شد.")
                else:
                    send_message(chat_id, "❌ وضعیت باید available یا not_available باشد.")
                return

            if text == '/list_plans':
                status_dict = db.get_all_plans_status()
                msg = "📋 **وضعیت پلن‌ها:**\n\n"
                for plan_id, info in PLANS.items():
                    avail = status_dict.get(plan_id, {}).get("available", True)
                    status_text = "✅ موجود" if avail else "❌ ناموجود"
                    msg += f"🔹 {info['display']} ({plan_id}): {status_text}\n"
                send_message(chat_id, msg)
                return

            if text.startswith('/msg '):
                parts = text.split(maxsplit=2)
                if len(parts) < 3:
                    send_message(chat_id, "❌ فرمت: `/msg user_id message`")
                    return
                try:
                    target = int(parts[1])
                    msg_text = parts[2]
                    result = send_message(target, f"📩 **پیام از ادمین:**\n{msg_text}")
                    if result and result.get('ok'):
                        send_message(chat_id, f"✅ پیام به کاربر {target} ارسال شد.")
                    else:
                        error = result.get('description', 'نامشخص') if result else 'خطای ارتباطی'
                        send_message(chat_id, f"❌ ارسال ناموفق: {error}")
                except:
                    send_message(chat_id, "❌ شناسه کاربر نامعتبر.")
                return

            if text.startswith('/block '):
                parts = text.split()
                if len(parts) != 2:
                    send_message(chat_id, "❌ فرمت: `/block user_id`")
                    return
                try:
                    target = int(parts[1])
                    if target == MASTER_ADMIN_ID:
                        send_message(chat_id, "❌ نمی‌توانید ادمین اصلی را بلاک کنید.")
                        return
                    db.add_admin_block(target)
                    send_message(chat_id, f"✅ کاربر {target} بلاک شد.")
                    send_message(target, "🚫 شما توسط ادمین بلاک شدید.")
                except:
                    send_message(chat_id, "❌ شناسه نامعتبر.")
                return

            if text.startswith('/unblock '):
                parts = text.split()
                if len(parts) != 2:
                    send_message(chat_id, "❌ فرمت: `/unblock user_id`")
                    return
                try:
                    target = int(parts[1])
                    db.remove_admin_block(target)
                    send_message(chat_id, f"✅ کاربر {target} آنبلاک شد.")
                    send_message(target, "✅ دسترسی شما بازگردانده شد.")
                except:
                    send_message(chat_id, "❌ شناسه نامعتبر.")
                return

            if text == '/stats':
                s = db.get_stats()
                send_message(chat_id, f"""
📊 آمار ربات
👥 کاربران: {s['total_users']}
📦 سفارشات: {s['total_orders']}
✅ تأیید: {s['approved_orders']}
⏳ در انتظار بررسی: {s['pending_orders']}
❌ رد: {s['rejected_orders']}
💰 درآمد: {s['total_revenue']:,} تومان
""")
                return
            if text == '/update_volume':
                send_message(chat_id, "ℹ️ این دستور دیگر فعال نیست. مدیریت حجم حذف شده است.")
                return
            if text == '/broadcast':
                return bale_broadcast.home(chat_id)
            if text == '/blocked_users':
                blocked = db.get_blocked_users()
                if not blocked:
                    send_message(chat_id, "✅ هیچ کاربری ربات را بلاک نکرده.")
                else:
                    msg = "🚫 کاربرانی که ربات را بلاک کرده‌اند:\n\n"
                    for uid, btime in blocked:
                        info = db.get_user_info(uid)
                        name = info.get('first_name', 'نامشخص') if info else 'نامشخص'
                        msg += f"🆔 {uid} - {name}\n📅 {btime}\n\n"
                    send_message(chat_id, msg)
                return
            if text == '/blocked_list':
                blocked = db.get_admin_blocked_list()
                if not blocked:
                    send_message(chat_id, "✅ هیچ کاربری توسط ادمین بلاک نشده.")
                else:
                    msg = "🚫 لیست بلاک شده توسط ادمین:\n\n"
                    for uid, btime, reason in blocked:
                        info = db.get_user_info(uid)
                        name = info.get('first_name', 'نامشخص') if info else 'نامشخص'
                        msg += f"🆔 {uid} - {name}\n📅 {btime}\n"
                        if reason:
                            msg += f"📝 دلیل: {reason}\n"
                        msg += "\n"
                    send_message(chat_id, msg)
                return
            if text == '/cancel':
                state, _ = get_user_state(chat_id)
                if state == "waiting_broadcast":
                    delete_user_state(chat_id)
                    send_message(chat_id, "❌ broadcast لغو شد.")
                elif state == "waiting_receipt_description":
                    delete_user_state(chat_id)
                    send_message(chat_id, "❌ افزودن توضیحات لغو شد. فیش شما بدون توضیح ارسال شد.")
                    order_id = state_data.get("order_id") if state_data else None
                    if order_id:
                        send_receipt_to_admins(order_id, description=None)
                else:
                    send_message(chat_id, "در حالت خاصی نیستید.")
                return
            if text.startswith('/finduser '):
                search_term = text[10:].strip()
                if not search_term:
                    send_message(chat_id, "❌ لطفاً یک نام برای جستجو وارد کنید.\nمثال: `/finduser علی`")
                    return
                conn = sqlite3.connect(db.db_name, timeout=10)
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT user_id, first_name, last_name, username 
                    FROM users 
                    WHERE first_name LIKE ? OR last_name LIKE ? OR username LIKE ?
                    LIMIT 20
                ''', (f'%{search_term}%', f'%{search_term}%', f'%{search_term}%'))
                results = cursor.fetchall()
                conn.close()
                if results:
                    msg = f"🔍 نتایج جستجو برای «{search_term}»:\n\n"
                    for uid, fn, ln, un in results:
                        msg += f"🆔 {uid} - {fn} {ln}\n"
                        if un and un != "ندارد":
                            msg += f"   📱 یوزرنیم: @{un}\n"
                        msg += "\n"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, f"❌ هیچ کاربری با نام «{search_term}» یافت نشد.")
                return

        # ========== دستورات عادی کاربر ==========
        if text == '/start':
            delete_user_state(chat_id)
            send_message(chat_id, welcome_text(first_name), reply_markup=main_menu())
        elif text == '/myid':
            send_message(chat_id, f"🆔 شناسه شما: {chat_id}")
        elif text in ("🛍 خرید سرویس", "🛒 خرید سرویس"):
            send_message(chat_id, "لطفاً نوع سرویس موردنظر خود را انتخاب بفرمایید:", reply_markup=panel_type_menu())
        elif text == "👤 حساب کاربری":
            orders = db.get_user_orders(chat_id)
            total = len(orders)
            approved = len([o for o in orders if o[4]=='approved'])
            send_message(chat_id, f"""
👤 اطلاعات حساب کاربری

📱 نام: {first_name}
🆔 شناسه: {chat_id}
📦 تعداد سفارشات: {total}
✅ تأیید شده: {approved}
""", reply_markup=main_menu())
        elif text in ("📦 سفارش‌های من", "📦 سفارشات من"):
            orders = db.get_user_orders(chat_id)
            if orders:
                send_message(chat_id, "📦 سفارش‌های شما\n\nبرای مشاهده جزئیات، یک سفارش را انتخاب بفرمایید:", reply_markup=my_orders_menu(chat_id))
            else:
                send_message(chat_id, "هنوز سفارشی ثبت نکرده‌اید. برای شروع، بخش خرید را انتخاب بفرمایید.", reply_markup=main_menu())
        elif text == "💬 پشتیبانی":
            send_message(chat_id, f"""💬 پشتیبانی

برای پرسش درباره خرید، پرداخت یا اتصال می‌توانید به پشتیبانی پیام دهید.

📩 شناسه: {SUPPORT}""", reply_markup=main_menu())
        elif text == "📖 راهنمای خرید":
            send_message(chat_id, """📖 راهنمای خرید سرویس

۱. از منوی خرید، سرویس مناسب را انتخاب کنید.
۲. مبلغ سفارش را به شماره کارت نمایش‌داده‌شده واریز کنید.
۳. تصویر رسید را در همین گفت‌وگو ارسال بفرمایید.
۴. پس از بررسی پرداخت (حداکثر ۱۰ دقیقه)، لینک اتصال برای شما ارسال می‌شود.

🔐 لطفاً اطلاعات اتصال خود را با دیگران به اشتراک نگذارید.""", reply_markup=main_menu())
        elif text == "📢 اطلاعیه‌ها":
            announcement = customer_announcement(db.get_active_announcement())
            if announcement:
                send_message(chat_id, f"📢 اطلاعیه فروشگاه:\n{announcement}", reply_markup=main_menu())
            else:
                send_message(chat_id, "📢 در حال حاضر اطلاعیه‌ای وجود ندارد.", reply_markup=main_menu())
        elif text in {"🌐 سفارش طراحی وبسایت", "🤖 سفارش ساخت ربات", "📱 شماره مجازی", "🤝 دریافت نمایندگی"}:
            send_message(chat_id, 'در این ربات فقط سرویس VPN ارائه می‌شود. برای مشاهده بسته‌های موجود، «🛍 خرید سرویس» را انتخاب بفرمایید.', reply_markup=main_menu())
        else:
            send_message(chat_id, "لطفاً یکی از گزینه‌های منو را انتخاب بفرمایید.", reply_markup=main_menu())

    elif 'photo' in message:
        state, state_data = get_user_state(chat_id)
        if state == "waiting_receipt" and state_data and 'order_id' in state_data:
            order_id = state_data['order_id']
            photo_id = message['photo'][-1]['file_id']
            if not db.save_receipt(order_id, photo_id, chat_id):
                order=db.get_order_by_id(order_id)
                if order and order[1]==chat_id and not db.get_plan_status(order[2])[0]:
                    send_message(chat_id,receipt_stock_notice())
                else:
                    send_message(chat_id,'⚠️ سفارش متعلق به شما نیست، بسته شده یا قبلاً رسید دریافت کرده است.')
                delete_user_state(chat_id)
                return
            send_receipt_to_admins(order_id)
            delete_user_state(chat_id)
            send_message(chat_id, f'✅ رسید سفارش {order_ref(order_id)} دریافت شد.\n'
                                  'بررسی پرداخت حداکثر 10 دقیقه زمان می‌برد.')
        elif state == 'receipt_pending':
            send_message(chat_id, 'لطفاً ابتدا روی دکمه «ارسال رسید» در صفحه پرداخت بزنید و سپس تصویر را ارسال کنید.')
        elif db.is_admin(chat_id):
            state, _ = get_user_state(chat_id)
            if state == 'bbr_waiting':
                bale_broadcast.capture(chat_id,photo_id=message['photo'][-1]['file_id'],
                                       caption=message.get('caption',''))
            elif state == 'waiting_broadcast':
                delete_user_state(chat_id)
                bale_broadcast.begin(chat_id)
                send_message(chat_id,'لطفاً تصویر را دوباره بفرستید تا پیش‌نمایش شود.')

# ----------------------------------------------------------------------
# دریافت آپدیت و حلقه اصلی
# ----------------------------------------------------------------------
def get_updates(offset=None):
    url = f"{API_URL}/getUpdates"
    params = {"timeout": 60, "offset": offset} if offset else {"timeout": 60}
    try:
        resp = session.get(url, params=params, timeout=65)
        return resp.json()
    except Exception as e:
        logger.error(f"خطا در getUpdates: {e}")
        return None

def main():
    logger.info("ربات شروع به کار کرد...")
    bale_broadcast.start_worker()
    try:
        test = session.get(f"{API_URL}/getMe", timeout=5)
        if test.status_code == 200:
            logger.info("✅ ارتباط با سرور بله برقرار است.")
        else:
            logger.warning("⚠️ توکن ممکن است نامعتبر باشد.")
    except:
        logger.warning("⚠️ خطا در تست اتصال اولیه.")

    offset = None
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE, "r") as f:
            try:
                offset = int(f.read().strip())
                logger.info(f"🔄 ادامه از offset: {offset}")
            except:
                pass

    while True:
        try:
            updates = get_updates(offset)
            if updates and updates.get('ok'):
                results = updates.get('result', [])
                for upd in results:
                    upd_id = upd['update_id']
                    if upd_id in PROCESSED_IDS:
                        continue
                    PROCESSED_IDS.add(upd_id)
                    if len(PROCESSED_IDS) > MAX_PROCESSED_IDS:
                        PROCESSED_IDS.clear()
                    offset = upd_id + 1
                    with open(OFFSET_FILE, "w") as f:
                        f.write(str(offset))
                    if 'message' in upd:
                        handle_message(upd['message'])
                    elif 'callback_query' in upd:
                        handle_callback_query(upd['callback_query'])
            else:
                if updates and updates.get('error_code') == 409 and offset:
                    offset += 1
                    logger.warning(f"برخورد با خطای 409، افزایش offset به {offset}")
            time.sleep(0.1)
        except KeyboardInterrupt:
            bale_broadcast.stop_worker()
            logger.info("ربات متوقف شد.")
            break
        except Exception as e:
            logger.exception(f"خطای اصلی: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()