"""One Railway service / two polling bots / one SQLite volume.

Fails closed: no in-container default DB, and no implicit empty DB creation.
Only run this after a current production backup and verified volume migration.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROCESSES = []
STOP = False

def _signal(_signum, _frame):
    global STOP
    STOP = True

def _preflight():
    volume_raw = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '').strip()
    db_raw = os.getenv('BOT_DB_PATH', '').strip()
    if not volume_raw or not db_raw:
        raise RuntimeError('Volume or BOT_DB_PATH missing; refusing ephemeral/empty DB.')
    volume = Path(volume_raw)
    db = Path(db_raw)
    if not volume.is_absolute() or not db.is_absolute():
        raise RuntimeError('Volume and BOT_DB_PATH must be absolute paths.')
    volume = volume.resolve()
    db = db.resolve()
    if db == volume or volume not in db.parents:
        raise RuntimeError('DB file must be inside the Railway volume.')
    if not db.is_file() or db.stat().st_size == 0:
        raise RuntimeError('Production DB not found; refusing to create a new DB.')
    needed = ('BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'MASTER_ADMIN_ID', 'TG_ADMIN_IDS')
    missing = [key for key in needed if not os.getenv(key, '').strip()]
    if missing:
        raise RuntimeError('Required environment variables missing: ' + ', '.join(missing))
    with sqlite3.connect('file:' + db.as_posix() + '?mode=ro', uri=True, timeout=10) as con:
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite quick_check failed')
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'users', 'orders', 'admins'}.issubset(tables):
            raise RuntimeError('DB is not the production Bale database.')
    # On the first switch copy the current Bale offset into the volume if available.
    # Never automatically copy old offsets: doing so could silently restore stale data.
    offsets = {'BALE_OFFSET_PATH': volume/'last_update.txt',
               'TG_OFFSET_PATH': volume/'telegram_last_update.txt'}
    for envname, default in offsets.items():
        offset = Path(os.getenv(envname, str(default))).resolve()
        if offset.parent != volume:
            raise RuntimeError(envname + ' must point to the volume root.')
        os.environ[envname] = str(offset)
    print('PRECHECK PASS: persistent DB verified, shared volume configured', flush=True)
    print('DB_PATH:', db, 'DB_SIZE:', db.stat().st_size, flush=True)
    print('Note: offsets must be migrated manually at cutover.', flush=True)
    return db

def main():
    global STOP
    try:
        _preflight()
    except Exception as exc:
        print('PRECHECK FAILED:', exc, file=sys.stderr, flush=True)
        return 2
    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    commands = [(sys.executable, '-u', str(ROOT/'bot.py')),
                (sys.executable, '-u', str(ROOT/'telegram_v2.py'))]
    try:
        for cmd in commands:
            process = subprocess.Popen(cmd, cwd=str(ROOT), env=os.environ.copy())
            PROCESSES.append(process)
            print('Started:', Path(cmd[-1]).name, 'pid:', process.pid, flush=True)
        while not STOP:
            for process in PROCESSES:
                if process.poll() is not None:
                    print('Bot process exited:', process.pid, 'return_code:', process.returncode,
                          file=sys.stderr, flush=True)
                    STOP = True
                    break
            if not STOP:
                time.sleep(1)
        return 1 if any(p.poll() is not None for p in PROCESSES) else 0
    finally:
        for p in PROCESSES:
            if p.poll() is None:
                p.terminate()
        deadline = time.monotonic() + 12
        for p in PROCESSES:
            try:
                p.wait(timeout=max(.1, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

if __name__ == '__main__':
    sys.exit(main())
