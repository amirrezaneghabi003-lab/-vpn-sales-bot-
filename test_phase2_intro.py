"""Pure offline tests for the pre-start profile and the two-robot welcome message."""
import unittest
from unittest.mock import Mock
from customer_experience import (
    TELEGRAM_DESCRIPTION, TELEGRAM_SHORT_DESCRIPTION, welcome_text,
    BALE_USERNAME, SUPPORT,
)
from telegram_v2 import apply_telegram_profile


class IntroTextTests(unittest.TestCase):
    def test_profile_description_valid_and_focused(self):
        self.assertLessEqual(len(TELEGRAM_DESCRIPTION), 512)
        self.assertLessEqual(len(TELEGRAM_SHORT_DESCRIPTION), 120)
        self.assertIn('VPN', TELEGRAM_DESCRIPTION)
        self.assertIn('اختلال اینترنت', TELEGRAM_DESCRIPTION)
        self.assertIn('قطعی‌های مقطعی', TELEGRAM_DESCRIPTION)
        for unrelated in ('طراحی وبسایت', 'شماره مجازی', 'ساخت ربات', 'نمایندگی'):
            self.assertNotIn(unrelated, TELEGRAM_DESCRIPTION)

    def test_welcome_mentions_support_and_bale(self):
        w = welcome_text('Someone <malicious>')
        self.assertIn('ربات اینترنت آزاد | پرو', w)
        self.assertIn(BALE_USERNAME, w)
        self.assertIn(SUPPORT, w)
        self.assertIn('خرید سرویس', w)
        self.assertNotIn('Someone', w)
        self.assertIn('10 دقیقه', w)

    def test_apply_profile_four_calls_no_extra_mutation(self):
        api = Mock()
        apply_telegram_profile(api)
        self.assertEqual(api.call.call_count, 4)
        methods = [c.args[0] for c in api.call.call_args_list]
        self.assertEqual(methods, ['setMyDescription', 'setMyShortDescription',
                                   'setMyDescription', 'setMyShortDescription'])
        self.assertEqual([c.args[1]['language_code'] for c in api.call.call_args_list],
                         ['', '', 'fa', 'fa'])
        self.assertEqual(api.call.call_args_list[0].args[1]['description'], TELEGRAM_DESCRIPTION)

    def test_prestart_text_requires_explicit_admin_action(self):
        # Profile configuration must not silently modify the bot on ordinary startup.
        from pathlib import Path
        source = (Path(__file__).parent / 'telegram_v2.py').read_text(encoding='utf-8')
        self.assertIn('if args.set_profile:', source)
        self.assertIn('if args.preview_profile:', source)
        self.assertEqual(source.count('apply_telegram_profile(TelegramAPI('), 1)


if __name__ == '__main__':
    unittest.main()
