"""Lightweight adapter for the same secure service-editor wizard in Bale.

No network on import. Uses Bale's existing sendMessage function and state table.
"""
from __future__ import annotations

import html
import json
import re
from telegram_catalog_admin import CatalogAdminMixin


def bale_plain(text):
    """Bale code uses plain text; never display markup tags from Telegram admin UI."""
    text=re.sub(r'</?(?:b|code|i)>','',str(text))
    return html.unescape(text)


class BaleStates:
    def __init__(self,original):
        self.original=original
        self.path=original.db_name
    def get_state(self,uid):
        state,data=self.original.get_user_state(uid)
        if state and state.startswith('catalog_') and isinstance(data,dict):
            return state,json.dumps(data,ensure_ascii=False)
        return state,data
    def set_state(self,uid,state,data=None):
        if state.startswith('catalog_') and isinstance(data,str):data=json.loads(data)
        self.original.set_user_state(uid,state,data)
    def clear_state(self,uid):self.original.delete_user_state(uid)


class BaleCatalogAdmin(CatalogAdminMixin):
    def __init__(self,database,send_message):
        self.original=database
        self.db=BaleStates(database)
        self.sender=send_message
    def is_admin(self,uid):return self.original.is_admin(uid)
    def safe_send(self,uid,text,markup=None):return self.sender(uid,bale_plain(text),reply_markup=markup)
    def screen(self,uid,text,markup,callback_message=None):
        return self.safe_send(uid,text,markup)
    def admin_home(self,uid,msg=None):
        return self.screen(uid,'🛡 مرکز مدیریت بله\n\nمدیریت سرویس‌ها: /services\nفهرست سرویس‌ها: /list_plans',
            {'inline_keyboard': [[{'text':'📦 مدیریت سرویس‌ها','callback_data':'adm:catalog'}]]})
