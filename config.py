import os
from dotenv import load_dotenv

load_dotenv()

# ------------------- توکن‌های ربات -------------------
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')

# ------------------- اطلاعات مالی -------------------
CARD_NUMBER = os.getenv('CARD_NUMBER', '')
CARD_HOLDER = os.getenv('CARD_HOLDER', '')

# ------------------- ادمین اصلی (مشترک) -------------------
MASTER_ADMIN_ID = int(os.getenv('MASTER_ADMIN_ID', '0'))

# ------------------- تعریف پلن‌ها (حجم به مگابایت) -------------------
PLANS = {
    # ========== پنل طلایی ==========
    "GOLD_25GB":  {"size": 25600, "price": 450000, "type": "طلایی", "users": "بدون محدودیت کاربر", "display": "طلایی - 25 گیگابایت"},
    "GOLD_50GB":  {"size": 51200, "price": 650000, "type": "طلایی", "users": "بدون محدودیت کاربر", "display": "طلایی - 50 گیگابایت"},
    "GOLD_UNLIMITED": {"size": 102400, "price": 950000, "type": "طلایی", "users": "بدون محدودیت کاربر", "display": "طلایی - نامحدود (مصرف منصفانه ۱۰۰ گیگ)"},

    # ========== پنل بین‌الملل ==========
    "INTERNATIONAL_25GB": {"size": 25600, "price": 275000, "type": "بین‌الملل", "users": "بدون محدودیت کاربر", "display": "بین‌الملل - 25 گیگابایت"},
    "INTERNATIONAL_50GB": {"size": 51200, "price": 450000, "type": "بین‌الملل", "users": "بدون محدودیت کاربر", "display": "بین‌الملل - 50 گیگابایت"},
    "INTERNATIONAL_UNLIMITED": {"size": 102400, "price": 850000, "type": "بین‌الملل", "users": "بدون محدودیت کاربر", "display": "بین‌الملل - نامحدود (مصرف منصفانه ۱۰۰ گیگ)"},

    # ========== پنل الترا ==========
    "ULTRA_10GB": {"size": 10240, "price": 200000, "type": "الترا", "users": "بدون محدودیت کاربر", "display": "الترا - 10 گیگابایت"},
    "ULTRA_20GB": {"size": 20480, "price": 350000, "type": "الترا", "users": "بدون محدودیت کاربر", "display": "الترا - 20 گیگابایت"},

    # ========== پنل نامحدود تک کاربره ==========
    "UNLIMITED_SINGLE": {
        "size": 102400,
        "price": 500000,
        "type": "تک کاربره نامحدود",
        "users": "تک کاربره",
        "display": "نامحدود تک کاربره - مصرف منصفانه ۱۰۰ گیگ"
    },
}