"""Visual Polish release checks. Pure/offline; no network and no live user data."""
import unittest
from contextlib import closing
from unittest.mock import Mock

from test_telegram_v2 import TelegramV2Test, config  # installs stub config and fake API
from telegram_v2 import Storefront
from telegram_ui import home, invoice, profile, plan, order_details, esc


class PresentationTests(unittest.TestCase):
    def test_home_focused_and_short(self):
        text = home()
        self.assertIn('اینترنت آزاد | پرو', text)
        self.assertIn('@Configgptbot', text)
        self.assertIn('@amiir_vip12', text)
        self.assertLess(len(text), 650)
        for unrelated in ('طراحی وبسایت', 'شماره مجازی', 'ساخت ربات'):
            self.assertNotIn(unrelated, text)

    def test_invoice_only_latin_digits_and_safe_dynamic_fields(self):
        text = invoice('pro121', '<b>Bad</b> ۲۵ گیگابایت', 275000,
                       '۶۱۰۴۳۳۸۹۶۰۸۳۰۳۸۴', 'نقابی')
        # The DB contains the canonical ASCII number; the output must not be mangled.
        self.assertIn('275,000', text)
        self.assertIn('&lt;b&gt;Bad&lt;/b&gt;', text)
        self.assertNotIn('<b>Bad</b>', text)
        self.assertIn('ارسال رسید', text)

    def test_plan_uses_real_duration_only_and_escapes_html(self):
        text = plan('<script>bad</script>', 'طلایی', '25 گیگابایت',
                    'یک کاربر', 275000, duration='30 روز')
        self.assertIn('30 روز', text)
        self.assertNotIn('<script>', text)
        self.assertIn('275,000', text)

    def test_profile_user_input_is_escaped(self):
        info = {'user_id': 123, 'first_name': '<b>Test</b>'}
        stats = {'total':2,'approved':1,'reviewing':1}
        text = profile(info, stats, {'id':4,'status':'under_review'})
        self.assertIn('&lt;b&gt;Test&lt;/b&gt;', text)
        self.assertIn('pro124', text)
        self.assertIn('در حال بررسی', text)


class LiveFlowOfflineTests(TelegramV2Test):
    def test_category_uses_one_batch_and_one_popularity_query(self):
        self.msg(111, '/start')
        with self.store.connection() as con:
            con.execute("UPDATE plan_status SET available=0 WHERE plan_id='GOLD_25GB'")
        batch = self.store.plan_statuses
        original = self.store.popular_plan_id
        from unittest.mock import patch
        with patch.object(self.store, 'plan_statuses', wraps=batch) as statuses, \
             patch.object(self.store, 'popular_plan_id', wraps=original) as popular:
            self.cb(111, 'category:0')
            self.assertEqual(statuses.call_count, 1)
            self.assertEqual(popular.call_count, 1)
        self.assertIn('بسته قابل سفارش', self.api.edits[-1][2])

    def test_invoice_remains_one_button_and_keeps_receipt_gate(self):
        self.msg(111, '/start')
        self.cb(111,'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        text, keyboard = self.api.edits[-1][2:4]
        self.assertIn('pro12', text)
        self.assertIn('<code>6104338960830384</code>', text)
        self.assertEqual(keyboard['inline_keyboard'], [[{'text':'📤 ارسال رسید','callback_data':f'receipt:{oid}'}]])
        self.msg(111, photo='NO_BUTTON')
        self.assertEqual(self.store.get_order(oid)['status'],'awaiting_receipt')

    def test_receipt_state_clears_when_navigating_away(self):
        self.msg(111, '/start')
        self.cb(111, 'buy:GOLD_25GB')
        oid = self.store.user_orders(111)[0]['id']
        self.cb(111, f'receipt:{oid}')
        self.assertEqual(self.store.get_state(111)[0], 'waiting_receipt')
        self.cb(111, 'catalog')
        self.assertEqual(self.store.get_state(111)[0], None)
        self.msg(111, photo='UNEXPECTED')
        self.assertEqual(self.store.get_order(oid)['status'],'awaiting_receipt')

    def test_not_available_cannot_be_purchased(self):
        with self.store.connection() as conn:
            conn.execute("UPDATE plan_status SET available=0 WHERE plan_id='GOLD_25GB'")
        self.msg(111,'/start')
        self.cb(111,'plan:GOLD_25GB')
        self.assertNotIn('buy:GOLD_25GB', repr(self.api.edits[-1][3]))
        self.cb(111,'buy:GOLD_25GB')
        self.assertEqual(self.store.user_stats(111)['total'],0)

    def test_bestseller_only_from_confirmed_sales(self):
        self.msg(111, '/start')
        self.assertIsNone(self.store.popular_plan_id())
        with self.store.connection() as conn:
            for i in range(3):
                uid = 700 + i
                conn.execute('INSERT INTO tg_users(user_id,first_name) VALUES(?,?)',(uid,'Tester'))
                conn.execute('INSERT INTO tg_orders(user_id,plan_id,amount,status) VALUES(?,?,?,?)',
                             (uid,'GOLD_25GB',500,'approved' if i>0 else 'cancelled'))
        self.assertIsNone(self.store.popular_plan_id())
        with self.store.connection() as conn:
            conn.execute("UPDATE tg_orders SET status='approved' WHERE plan_id='GOLD_25GB' AND status='cancelled'")
        self.assertEqual(self.store.popular_plan_id(), 'GOLD_25GB')
        self.cb(111, 'category:0')
        self.assertIn('⭐ پرفروش', str(self.api.edits[-1][3]))

    def test_account_has_last_order_button_and_safe_title(self):
        self.msg(111,'/start')
        self.cb(111,'buy:GOLD_25GB')
        self.cb(111,'account')
        txt = self.api.edits[-1][2]
        kb = self.api.edits[-1][3]
        self.assertIn('آخرین سفارش', txt)
        self.assertIn('pro12', txt)
        self.assertTrue(any(x['callback_data'].startswith('order:') for row in kb['inline_keyboard'] for x in row))

    def test_navigation_edits_message_not_spam(self):
        self.msg(111,'/start')
        self.cb(111,'catalog')
        self.cb(111,'category:0')
        self.cb(111,'home')
        self.assertEqual(len(self.api.messages),1)
        self.assertEqual(len(self.api.edits),3)
        self.assertEqual(len(self.api.answers),3)


if __name__ == '__main__':
    unittest.main()
