"""Offline regression tests for native Telegram menu, profile, and Tehran clock."""
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from customer_experience import (
    TELEGRAM_DESCRIPTION, TELEGRAM_SHORT_DESCRIPTION,
    jalali_datetime, BALE_USERNAME,
)
from telegram_v2 import apply_telegram_menu, USER_MENU_COMMANDS, time_diagnostic, Storefront


class NewMenuTests(unittest.TestCase):
    def test_native_menu_setup_and_scopes(self):
        api = Mock()
        apply_telegram_menu(api)
        self.assertEqual([call.args[0] for call in api.call.call_args_list],
                         ['setMyCommands', 'setMyCommands', 'setChatMenuButton'])
        self.assertEqual([c.args[1]['language_code'] for c in api.call.call_args_list[:2]], ['', 'fa'])
        self.assertEqual(api.call.call_args_list[2].args[1], {'menu_button': {'type': 'commands'}})
        cmds = [x['command'] for x in api.call.call_args_list[0].args[1]['commands']]
        self.assertEqual(cmds, [cmd for cmd, _ in USER_MENU_COMMANDS])
        self.assertEqual(len(cmds), len(set(cmds)))

    def test_prestart_has_bale_and_device_conditions(self):
        self.assertIn(BALE_USERNAME, TELEGRAM_DESCRIPTION)
        self.assertIn('دستگاه', TELEGRAM_DESCRIPTION)
        self.assertIn('قطعی', TELEGRAM_DESCRIPTION)
        self.assertLessEqual(len(TELEGRAM_DESCRIPTION), 512)
        self.assertLessEqual(len(TELEGRAM_SHORT_DESCRIPTION), 120)

    def test_menu_routes_work_mid_purchase(self):
        for command, method in [('/buy', 'catalog'), ('/orders', 'orders'),
                                ('/account','account'), ('/guide','guide'),
                                ('/support','support'), ('/news','news')]:
            db = Mock()
            api = Mock()
            store = Storefront(db, api, admin_ids={999}, card_number='', card_holder='', support_contact='')
            called = Mock()
            setattr(store, method, called)
            store.message({'chat': {'type':'private','id':123}, 'from':{'id':123,'first_name':'Test'},'text':command})
            db.clear_state.assert_called_once_with(123)
            called.assert_called_once_with(123)

    def test_time_is_explicit_tehran_and_respects_offset(self):
        self.assertEqual(jalali_datetime('2026-10-14 07:30:00'), '1405/07/22 | 11:00 (تهران)')
        self.assertEqual(jalali_datetime('2026-10-14T11:00:00+03:30'), '1405/07/22 | 11:00 (تهران)')
        self.assertEqual(jalali_datetime('2026-10-14T07:30:00Z'), '1405/07/22 | 11:00 (تهران)')

    def test_readonly_clock_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder)/'sample.db'
            with closing(sqlite3.connect(db)) as con, con:
                con.execute('CREATE TABLE tg_orders(id INTEGER PRIMARY KEY,created_at TEXT)')
                con.execute("INSERT INTO tg_orders(created_at) VALUES('2026-10-14 07:30:00')")
            out = io.StringIO()
            with redirect_stdout(out):
                time_diagnostic(db)
            msg = out.getvalue()
            self.assertIn('SQLite vs Python drift:', msg)
            self.assertIn('Tehran Jalali:', msg)
            self.assertIn('tg_orders display Tehran: 1405/07/22 | 11:00 (تهران)', msg)

    def test_clock_diagnostic_closes_all_connections(self):
        class TrackedConnection(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.was_closed = False

            def close(self):
                self.was_closed = True
                return super().close()

        opened = []
        original_connect = sqlite3.connect

        def connect_tracked(*args, **kwargs):
            kwargs['factory'] = TrackedConnection
            connection = original_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'tracked.db'
            with closing(original_connect(db)) as conn, conn:
                conn.execute('CREATE TABLE tg_orders (id INTEGER PRIMARY KEY, created_at TEXT)')
                conn.execute("INSERT INTO tg_orders(created_at) VALUES('2026-10-14 07:30:00')")
            with patch('telegram_v2.sqlite3.connect', side_effect=connect_tracked):
                with redirect_stdout(io.StringIO()):
                    time_diagnostic(db)
            self.assertEqual(len(opened), 2)
            self.assertTrue(all(con.was_closed for con in opened))

if __name__ == '__main__':
    unittest.main()
