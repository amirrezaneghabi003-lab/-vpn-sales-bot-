import requests
import json
import time
import os
import logging
import threading
import sqlite3
from datetime import datetime
from database import Database
from config import TELEGRAM_BOT_TOKEN, CARD_NUMBER, CARD_HOLDER, MASTER_ADMIN_ID, PLANS

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
db = Database()
OFFSET_FILE = "telegram_last_update.txt"   # فایل مجزا برای تلگرام
session = requests.Session()
PROCESSED_IDS = set()
MAX_PROCESSED_IDS = 5000

# ----------------------------------------------------------------------
# توابع کمکی (دقیقاً مشابه نسخه بله، اما با API_URL جدید)
# ----------------------------------------------------------------------
def set_user_state(user_id, state, data=None):
    db.set_user_state(user_id, state, data)

def get_user_state(user_id):
    return db.get_user_state(user_id)

def delete_user_state(user_id):
    db.delete_user_state(user_id)

def send_message(chat_id, text, reply_markup=None):
    url = f"{API_URL}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
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
        logger.exception(f"استثنا در send_message: {e}")
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
        logger.exception(f"خطا در send_photo: {e}")
        return None

def send_broadcast_message(admin_chat_id, content, is_photo=False, caption=""):
    def broadcast_worker():
        users = db.get_all_users()
        total = len(users)
        if total == 0:
            send_message(admin_chat_id, "❌ هیچ کاربری در دیتابیس وجود ندارد.")
            return
        send_message(admin_chat_id, f"⏳ شروع ارسال پیام به {total} کاربر...")
        success = 0
        fail = 0
        failed_users = []
        blocked_users = []

        for idx, user_id in enumerate(users):
            if db.is_user_blocked_by_admin(user_id):
                continue
            try:
                if is_photo:
                    res = send_photo(user_id, content, caption=caption)
                else:
                    res = send_message(user_id, content)
                if res and res.get('ok'):
                    success += 1
                else:
                    fail += 1
                    if res and 'blocked' in res.get('description', '').lower():
                        blocked_users.append(user_id)
                        db.add_blocked_user(user_id)
                    else:
                        failed_users.append(user_id)
            except Exception as e:
                fail += 1
                failed_users.append(user_id)
                logger.error(f"خطا در broadcast به {user_id}: {e}")
            time.sleep(0.1)
            if (idx+1) % 100 == 0:
                send_message(admin_chat_id, f"📊 وضعیت: {idx+1}/{total} | موفق: {success} | ناموفق: {fail}")
        report = f"""
✅ عملیات broadcast پایان یافت.
📨 کل کاربران: {total}
✅ موفق: {success}
❌ ناموفق: {fail}
"""
        send_message(admin_chat_id, report)
        if failed_users:
            txt = "🚫 کاربرانی که پیام دریافت نکردند:\n"
            for uid in failed_users[:30]:
                info = db.get_user_info(uid)
                name = info.get('first_name', 'نامشخص') if info else 'نامشخص'
                txt += f"🆔 {uid} - {name}\n"
            if len(failed_users) > 30:
                txt += f"\n... و {len(failed_users)-30} کاربر دیگر"
            send_message(admin_chat_id, txt)
        if blocked_users:
            txt = "🚫 کاربرانی که ربات را بلاک کرده‌اند:\n"
            for uid in blocked_users[:30]:
                info = db.get_user_info(uid)
                name = info.get('first_name', 'نامشخص') if info else 'نامشخص'
                txt += f"🆔 {uid} - {name}\n"
            if len(blocked_users) > 30:
                txt += f"\n... و {len(blocked_users)-30} کاربر دیگر"
            send_message(admin_chat_id, txt)
    thread = threading.Thread(target=broadcast_worker, daemon=True)
    thread.start()

# ----------------------------------------------------------------------
# منوها (دقیقاً مشابه نسخه بله)
# ----------------------------------------------------------------------
def main_menu():
    keyboard = {
        "keyboard": [
            [{"text": "🛒 خرید سرویس"}],
            [{"text": "👤 حساب کاربری"}, {"text": "📦 سفارشات من"}],
            [{"text": "💬 پشتیبانی"}, {"text": "ℹ️ راهنما"}, {"text": "📢 اطلاعیه‌ها"}]
        ],
        "resize_keyboard": True
    }
    return keyboard

def panel_type_menu():
    keyboard = {
        "inline_keyboard": [
            [{"text": "🥈 پنل نقره‌ای (بدون محدودیت کاربر)", "callback_data": "panel_silver"}],
            [{"text": "🥇 پنل طلایی (بدون محدودیت کاربر)", "callback_data": "panel_gold"}],
            [{"text": "💰 پنل اقتصادی (بدون محدودیت کاربر - هر گیگ ۲۵۰,۰۰۰ تومان)", "callback_data": "panel_economic"}],
            [{"text": "🌐 پنل بین‌الملل (اینترنت بین‌الملل - ۵۰ گیگ ۶۰۰,۰۰۰ تومان)", "callback_data": "panel_international"}],
            [{"text": "⚡ پنل الترا (نت ملی و بین‌الملل - ساب‌دار با لوکیشن آلمان، آمریکا، هلند، فنلاند)", "callback_data": "panel_ultra"}],
            [{"text": "🔙 بازگشت", "callback_data": "back_main"}]
        ]
    }
    return keyboard

def plans_menu(panel_type):
    keyboard = {"inline_keyboard": []}
    status_dict = db.get_all_plans_status()
    for plan_id, info in PLANS.items():
        if (panel_type == "silver" and info['type'] == "نقره‌ای") or \
           (panel_type == "gold" and info['type'] == "طلایی") or \
           (panel_type == "economic" and info['type'] == "اقتصادی") or \
           (panel_type == "international" and info['type'] == "بین‌الملل") or \
           (panel_type == "ultra" and info['type'] == "الترا"):
            plan_status = status_dict.get(plan_id, {})
            available = plan_status.get("available", True)
            if available:
                btn_text = f"{info['display']} - {info['price']:,} تومان"
                callback = f"plan_{plan_id}"
            else:
                btn_text = f"⛔ {info['display']} - نا موجود"
                callback = f"unavailable_{plan_id}"
            keyboard["inline_keyboard"].append([{
                "text": btn_text,
                "callback_data": callback
            }])
    keyboard["inline_keyboard"].append([{"text": "🔙 بازگشت به انتخاب پنل", "callback_data": "back_panel_select"}])
    return keyboard

def my_orders_menu(chat_id):
    orders = db.get_user_orders(chat_id)
    if not orders:
        return None
    keyboard = {"inline_keyboard": []}
    status_emoji = {'pending': '⏳', 'approved': '✅', 'rejected': '❌', 'cancelled': '🚫'}
    for order in orders[:10]:
        plan = PLANS.get(order[2], {})
        panel_name = f"{plan.get('type','')} {order[2].split('_')[-1]}" if plan else order[2]
        emoji = status_emoji.get(order[4], '❓')
        keyboard["inline_keyboard"].append([{
            "text": f"{emoji} #{order[0]} - {panel_name}",
            "callback_data": f"order_{order[0]}"
        }])
    keyboard["inline_keyboard"].append([{"text": "🔙 بازگشت", "callback_data": "back_main"}])
    return keyboard

def cancel_order_menu(order_id):
    return {"inline_keyboard": [[
        {"text": "✅ بله، لغو شود", "callback_data": f"cancel_yes_{order_id}"},
        {"text": "❌ خیر", "callback_data": "back_orders"}
    ]]}

def admin_order_menu(order_id):
    return {"inline_keyboard": [[
        {"text": "✅ تأیید", "callback_data": f"approve_{order_id}"},
        {"text": "❌ رد", "callback_data": f"reject_{order_id}"}
    ]]}

# ----------------------------------------------------------------------
# مدیریت کال‌بک‌ها (دقیقاً مشابه نسخه بله)
# ----------------------------------------------------------------------
def handle_callback_query(callback_query):
    chat_id = callback_query['message']['chat']['id']
    if not db.is_admin(chat_id) and db.is_user_blocked_by_admin(chat_id):
        return
    data = callback_query['data']
    try:
        session.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": callback_query['id']}, timeout=5)
    except:
        pass

    if data == "panel_silver":
        send_message(chat_id, "🥈 بسته‌های پنل نقره‌ای (بدون محدودیت کاربر):", reply_markup=plans_menu("silver"))
    elif data == "panel_gold":
        send_message(chat_id, "🥇 بسته‌های پنل طلایی (بدون محدودیت کاربر):", reply_markup=plans_menu("gold"))
    elif data == "panel_economic":
        send_message(chat_id, "💰 بسته‌های پنل اقتصادی بدون محدودیت کاربر - هر گیگ ۲۵۰,۰۰۰ تومان):", reply_markup=plans_menu("economic"))
    elif data == "panel_international":
        send_message(chat_id, "🌐 بسته‌های پنل بین‌الملل (اینترنت بین‌الملل - ۵۰ گیگ ۶۰۰,۰۰۰ تومان):", reply_markup=plans_menu("international"))
    elif data == "panel_ultra":
        send_message(chat_id, "⚡ پنل الترا (نت ملی و بین‌الملل)\n\n✨ ویژگی‌ها:\n✅ بدون محدودیت کاربر\n✅ سرورهای دارای ساب (Subscription)\n✅ لوکیشن‌های متنوع: آلمان 🇩🇪، آمریکا 🇺🇸، هلند 🇳🇱، فنلاند 🇫🇮\n\nبسته‌های موجود:", reply_markup=plans_menu("ultra"))
    elif data == "back_panel_select":
        send_message(chat_id, "لطفاً نوع پنل مورد نظر خود را انتخاب کنید:", reply_markup=panel_type_menu())
    elif data.startswith("unavailable_"):
        plan_id = data[11:]
        plan = PLANS.get(plan_id, {})
        _, msg = db.get_plan_status(plan_id)
        if not msg:
            msg = f"⛔ متأسفانه پنل {plan.get('display', plan_id)} در حال حاضر موجود نیست. لطفاً بعداً مراجعه کنید."
        send_message(chat_id, msg)
    elif data.startswith("plan_"):
        plan_id = data[5:]
        if plan_id in PLANS:
            available, _ = db.get_plan_status(plan_id)
            if not available:
                send_message(chat_id, "❌ این پلن در حال حاضر موجود نیست.")
                return
            plan = PLANS[plan_id]
            order_id = db.create_order(chat_id, plan_id, plan['price'])
            text = f"""
✅ سفارش شما ثبت شد!

🏷️ پنل: {plan['type']}
📦 بسته: {plan['display']}
👥 نوع: {plan['users']}
💰 مبلغ قابل پرداخت: {plan['price']:,} تومان

💳 شماره کارت: {CARD_NUMBER}
👤 به نام: {CARD_HOLDER}

لطفاً پس از واریز، عکس فیش واریزی را ارسال کنید.
⏱ زمان بررسی: حداکثر 6 ساعت
"""
            send_message(chat_id, text)
            set_user_state(chat_id, "waiting_receipt", {"order_id": order_id})
    elif data == "back_main":
        send_message(chat_id, "منوی اصلی:", reply_markup=main_menu())
    elif data == "back_orders":
        orders = db.get_user_orders(chat_id)
        if orders:
            send_message(chat_id, "📦 سفارشات شما:", reply_markup=my_orders_menu(chat_id))
        else:
            send_message(chat_id, "شما هنوز سفارشی ثبت نکرده‌اید.", reply_markup=main_menu())
    elif data.startswith("order_"):
        oid = int(data[6:])
        order = db.get_order_by_id(oid)
        if order and order[1] == chat_id:
            plan = PLANS.get(order[2], {})
            status_text = {'pending':'⏳ در انتظار', 'approved':'✅ تأیید', 'rejected':'❌ رد', 'cancelled':'🚫 لغو'}
            text = f"""
📦 جزئیات سفارش #{order[0]}

🏷️ پنل: {plan.get('type','نامشخص')}
📦 بسته: {plan.get('display','نامشخص')}
👥 نوع: {plan.get('users','نامشخص')}
📊 وضعیت: {status_text.get(order[4], 'نامشخص')}
💰 مبلغ: {order[3]:,} تومان
📅 تاریخ: {order[5]}
"""
            if order[4] == 'pending':
                kb = {"inline_keyboard": [
                    [{"text": "🚫 لغو سفارش", "callback_data": f"cancel_confirm_{oid}"}],
                    [{"text": "🔙 بازگشت", "callback_data": "back_orders"}]
                ]}
                send_message(chat_id, text, reply_markup=kb)
            else:
                kb = {"inline_keyboard": [[{"text": "🔙 بازگشت", "callback_data": "back_orders"}]]}
                send_message(chat_id, text, reply_markup=kb)
    elif data.startswith("cancel_confirm_"):
        oid = int(data[15:])
        send_message(chat_id, "⚠️ آیا از لغو این سفارش اطمینان دارید؟", reply_markup=cancel_order_menu(oid))
    elif data.startswith("cancel_yes_"):
        oid = int(data[11:])
        db.cancel_order(oid)
        send_message(chat_id, "✅ سفارش لغو شد.", reply_markup=main_menu())
    elif data.startswith("approve_"):
        if db.is_admin(chat_id):
            oid = int(data[8:])
            if db.get_order_by_id(oid):
                set_user_state(chat_id, "send_link", {"order_id": oid})
                send_message(chat_id, f"✅ لطفاً لینک سرویس را برای سفارش #{oid} ارسال کنید:")
            else:
                send_message(chat_id, f"❌ سفارش #{oid} یافت نشد.")
    elif data.startswith("reject_"):
        if db.is_admin(chat_id):
            oid = int(data[7:])
            db.reject_order(oid)
            order = db.get_order_by_id(oid)
            if order:
                send_message(order[1], f"❌ سفارش #{oid} شما رد شد. با پشتیبانی تماس بگیرید.")
                send_message(chat_id, f"✅ سفارش #{oid} رد شد.")

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

    if 'text' in message:
        text = message['text']
        state, state_data = get_user_state(chat_id)
        if state:
            if state == "send_link" and db.is_admin(chat_id):
                order_id = state_data['order_id']
                service_link = text.strip()
                try:
                    db.approve_order(order_id, service_link)
                    logger.info(f"سفارش {order_id} تأیید شد.")
                except Exception as e:
                    send_message(chat_id, f"❌ خطا در تأیید سفارش: {e}")
                    delete_user_state(chat_id)
                    return
                order = db.get_order_by_id(order_id)
                if order:
                    user_id = order[1]
                    res = send_message(user_id, f"""
✅ سفارش شما تأیید شد!

🎉 لینک سرویس:
{service_link}

از خرید شما متشکریم! 🙏
""")
                    if res and res.get('ok'):
                        send_message(chat_id, f"✅ لینک به کاربر سفارش #{order_id} ارسال شد.")
                    else:
                        send_message(chat_id, f"❌ ارسال لینک به کاربر ناموفق بود!")
                delete_user_state(chat_id)
                return
            elif state == "waiting_broadcast" and db.is_admin(chat_id):
                if text == '/cancel':
                    delete_user_state(chat_id)
                    send_message(chat_id, "❌ عملیات broadcast لغو شد.")
                    return
                send_broadcast_message(chat_id, text, is_photo=False)
                delete_user_state(chat_id)
                return
            elif state == "waiting_receipt" and state_data:
                pass

        # ========== دستورات ادمین ==========
        if db.is_admin(chat_id) and text.startswith('/'):
            # دستورات فقط برای ادمین اصلی
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
                    db.set_plan_availability(plan_id, True)
                    send_message(chat_id, f"✅ پلن {plan_id} اکنون موجود است.")
                elif status == 'not_available':
                    db.set_plan_availability(plan_id, False, message)
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
⏳ در انتظار: {s['pending_orders']}
❌ رد: {s['rejected_orders']}
💰 درآمد: {s['total_revenue']:,} تومان
""")
                return
            if text == '/update_volume':
                send_message(chat_id, "ℹ️ این دستور دیگر فعال نیست. مدیریت حجم حذف شده است.")
                return
            if text == '/broadcast':
                set_user_state(chat_id, "waiting_broadcast", None)
                send_message(chat_id, "📢 پیام همگانی را ارسال کنید (متن یا عکس).\nبرای لغو /cancel")
                return
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
                else:
                    send_message(chat_id, "در حالت خاصی نیستید.")
                return
            if text.startswith('/finduser '):
                search_term = text[10:].strip()
                if not search_term:
                    send_message(chat_id, "❌ لطفاً یک نام برای جستجو وارد کنید.\nمثال: `/finduser علی`")
                    return
                conn = sqlite3.connect('bot_data.db')
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
            welcome_msg = f"""
🌟 به ربات فروش سرویس اینترنت پرسرعت خوش آمدید!

✅ پنل‌های نقره‌ای، طلایی، اقتصادی، بین‌الملل و الترا (بدون محدودیت کاربر)
✅ بدون ضریب – حجم واقعی و شفاف  
✅ پشتیبانی ۲۴ ساعته در پیام‌رسان بله و تلگرام   
✅ ارسال لینک سرویس بلافاصله پس از تأیید فیش
✅ در صورت خاموش بودن ربات به ادمین پیام بدید ‎@amirvip3

از منوی زیر استفاده کنید:
"""
            announcement = db.get_active_announcement()
            if announcement:
                welcome_msg += f"\n\n📢 **اطلاعیه:**\n{announcement}"
            send_message(chat_id, welcome_msg, reply_markup=main_menu())
        elif text == '/myid':
            send_message(chat_id, f"🆔 شناسه شما: `{chat_id}`")
        elif text == "🛒 خرید سرویس":
            send_message(chat_id, "لطفاً نوع پنل مورد نظر خود را انتخاب کنید:", reply_markup=panel_type_menu())
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
        elif text == "📦 سفارشات من":
            orders = db.get_user_orders(chat_id)
            if orders:
                send_message(chat_id, "📦 سفارشات شما:", reply_markup=my_orders_menu(chat_id))
            else:
                send_message(chat_id, "شما هنوز سفارشی ثبت نکرده‌اید.", reply_markup=main_menu())
        elif text == "💬 پشتیبانی":
            send_message(chat_id, """
💬 پشتیبانی

آیدی تلگرام: @amiir_vip12
ساعت پاسخگویی: 9 صبح تا 12 شب
""", reply_markup=main_menu())
        elif text == "ℹ️ راهنما":
            send_message(chat_id, """
ℹ️ راهنما

1️⃣ خرید سرویس (انتخاب پنل نقره‌ای، طلایی، اقتصادی، بین‌الملل یا الترا)
2️⃣ واریز به کارت و ارسال فیش
3️⃣ دریافت لینک پس از تأیید

⏱ زمان بررسی: حداکثر 3 ساعت 
""", reply_markup=main_menu())
        elif text == "📢 اطلاعیه‌ها":
            announcement = db.get_active_announcement()
            if announcement:
                send_message(chat_id, f"📢 **اطلاعیه رسمی ربات:**\n{announcement}", reply_markup=main_menu())
            else:
                send_message(chat_id, "📢 در حال حاضر اطلاعیه‌ای وجود ندارد.", reply_markup=main_menu())
        else:
            send_message(chat_id, "❓ دستور نامعتبر. لطفاً از منو استفاده کنید.", reply_markup=main_menu())

    elif 'photo' in message:
        state, state_data = get_user_state(chat_id)
        if state == "waiting_receipt" and state_data and 'order_id' in state_data:
            order_id = state_data['order_id']
            photo_id = message['photo'][-1]['file_id']
            db.save_receipt(order_id, photo_id)
            send_message(chat_id, "✅ فیش شما دریافت شد. در حال بررسی...", reply_markup=main_menu())
            order = db.get_order_by_id(order_id)
            if order:
                plan = PLANS.get(order[2], {})
                admin_text = f"""
🔔 سفارش جدید #{order[0]}
👤 کاربر: {order[1]}
🏷️ پنل: {plan.get('type','نامشخص')} - {plan.get('display','')}
💰 مبلغ: {order[3]:,} تومان
"""
                for admin_id, _, _ in db.get_all_admins():
                    send_message(admin_id, admin_text)
                    send_photo(admin_id, photo_id, caption=f"فیش سفارش #{order[0]}")
                    send_message(admin_id, "لطفاً سفارش را بررسی کنید:", reply_markup=admin_order_menu(order_id))
            delete_user_state(chat_id)
        elif db.is_admin(chat_id):
            state, _ = get_user_state(chat_id)
            if state == "waiting_broadcast":
                photo_id = message['photo'][-1]['file_id']
                caption = message.get('caption', '')
                send_broadcast_message(chat_id, photo_id, is_photo=True, caption=caption)
                delete_user_state(chat_id)

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
    logger.info("ربات تلگرام شروع به کار کرد...")
    try:
        test = session.get(f"{API_URL}/getMe", timeout=5)
        if test.status_code == 200:
            logger.info("✅ ارتباط با سرور تلگرام برقرار است.")
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

    first_round = True
    notified_users = set()

    while True:
        try:
            updates = get_updates(offset)
            if updates and updates.get('ok'):
                results = updates.get('result', [])
                if first_round and results:
                    for upd in results:
                        cid = None
                        if 'message' in upd:
                            cid = upd['message']['chat']['id']
                        elif 'callback_query' in upd:
                            cid = upd['callback_query']['message']['chat']['id']
                        if cid and cid not in notified_users:
                            notified_users.add(cid)
                            send_message(cid, "🔄 ربات دوباره آنلاین شد. درخواست شما در حال پردازش است.")
                    first_round = False

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
            logger.info("ربات تلگرام متوقف شد.")
            break
        except Exception as e:
            logger.exception(f"خطای اصلی: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()