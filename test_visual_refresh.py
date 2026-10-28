"""Visual storefront + safe navigation regressions. No API calls or production DB."""
import unittest
from unittest.mock import Mock
from customer_experience import TELEGRAM_DESCRIPTION, TELEGRAM_SHORT_DESCRIPTION, welcome_text, telegram_welcome_html, BALE_USERNAME, SUPPORT
import test_telegram_v2  # ensures offline test config is installed before Telegram import
from telegram_v2 import Storefront

class VisualRefreshTests(unittest.TestCase):
    def test_marketing_copy_present_before_after_start(self):
        for copy in (TELEGRAM_DESCRIPTION, welcome_text(), telegram_welcome_html()):
            for item in ('ربات اینترنت آزاد | پرو', 'کیفیت', 'محدودیت', BALE_USERNAME, SUPPORT):
                self.assertIn(item, copy)
            for obsolete in ('طراحی وبسایت', 'شماره مجازی', 'نمایندگی فروش', 'ساخت ربات'):
                self.assertNotIn(obsolete, copy)
        self.assertIn('«شروع»', TELEGRAM_DESCRIPTION)
        self.assertIn('«خرید سرویس»', welcome_text())
        self.assertIn('قطعی', TELEGRAM_DESCRIPTION)
        self.assertLessEqual(len(TELEGRAM_DESCRIPTION), 512)
        self.assertLessEqual(len(TELEGRAM_SHORT_DESCRIPTION), 120)

    def test_html_template_is_safe_static_text(self):
        doc = telegram_welcome_html()
        self.assertEqual(doc.count('<b>'), doc.count('</b>'))
        self.assertNotIn('<script', doc)
        self.assertIn('10 دقیقه', doc)

    def test_inline_home_has_clean_two_column_navigation(self):
        app = Storefront(Mock(), Mock(), admin_ids={999}, card_number='', card_holder='', support_contact='')
        app.welcome(123)
        sent = app.api.send.call_args
        markup = sent.args[2]
        self.assertEqual([len(r) for r in markup['inline_keyboard']], [1, 2, 2])
        callbacks = [v['callback_data'] for row in markup['inline_keyboard'] for v in row]
        self.assertEqual(callbacks, ['catalog','orders:0','account','guide','support'])
        self.assertIn('<b>', sent.args[1])

    def test_category_compact_and_edit_existing_screen(self):
        app = Storefront(Mock(), Mock(), admin_ids=set(), card_number='', card_holder='', support_contact='')
        app.catalog(123, {'chat': {'id':123}, 'message_id':5})
        app.api.edit.assert_called_once()
        markup = app.api.edit.call_args.args[3]
        self.assertEqual(markup['inline_keyboard'][-1][0]['callback_data'], 'home')
        self.assertTrue(all(len(row) <= 2 for row in markup['inline_keyboard']))

    def test_new_nav_callbacks_never_trigger_purchase(self):
        app = Storefront(Mock(), Mock(), admin_ids=set(), card_number='', card_holder='', support_contact='')
        app.api.answer.return_value = True
        app.db.user_stats.return_value = {'total': 0, 'approved': 0, 'reviewing': 0}
        app.callback({'id':'test','from':{'id':222},'message':{'chat':{'id':222,'type':'private'}, 'message_id':1},'data':'account'})
        app.db.open_order.assert_not_called()
        app.db.get_user_order.assert_not_called()
        app.api.edit.assert_called_once()

if __name__ == '__main__': unittest.main()
