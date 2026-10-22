"""`python -m bugmonitor doctor` — explain what's running and what holds the database."""
from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import __version__
from .config import ROOT, load_settings
from .db import get_state
from .locks import FileLock


def _run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def _icloud_synced(path: Path) -> bool:
    if platform.system() != "Darwin":
        return False
    if "Mobile Documents" in str(path.resolve()):
        return True
    for p in [path, *path.parents]:
        if p == Path.home():
            break
        if "fileprovider" in _run(["xattr", str(p)]).lower():
            return True
    return False


def main() -> int:
    s = load_settings()
    db = s.db_path
    ok = True
    print(f"Jira Bug Monitor {__version__}")
    print(f"Code:     {ROOT}")
    print(f"Database: {db}")

    if _icloud_synced(db.parent):
        ok = False
        print("\n[!] The database folder is synced by iCloud Drive. iCloud can lock SQLite files.\n"
              "    Fix: in config.toml set\n"
              '      [storage]\n      db_path = "~/Library/Application Support/jira-bug-monitor/monitor.db"')

    print("\nRunning monitor processes:")
    procs = [l for l in _run(["ps", "-axo", "pid,etime,command"]).splitlines()
             if "bugmonitor" in l and "doctor" not in l]
    print("\n".join(f"  {l.strip()}" for l in procs) or "  none")
    runs = [l for l in procs if " run" in l]
    if len(runs) > 1:
        ok = False
        print("[!] More than one 'bugmonitor run' is active. Stop the extras (see pids above).")

    lock = FileLock(db.parent / "sync.lock")
    if lock.acquire():
        lock.release()
        print("\nSync: idle")
    else:
        print(f"\nSync: running now in pid {lock.holder_pid()}")

    if db.exists():
        files = [str(p) for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")) if p.exists()]
        holders = _run(["lsof", "-F", "pc", *files])
        if holders:
            pids, cur = [], None
            for line in holders.splitlines():
                if line.startswith("p"):
                    cur = line[1:]
                elif line.startswith("c") and cur:
                    pids.append(f"{cur} ({line[1:]})")
            print("Processes with the database open: " + (", ".join(dict.fromkeys(pids)) or "none"))

        conn = sqlite3.connect(db, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            print("Write test: OK")
        except sqlite3.OperationalError as e:
            ok = False
            print(f"Write test: FAILED ({e})")
            print("[!] Another process is holding a write lock. Stop the processes listed above\n"
                  "    (Ctrl+C in the start.sh window, or: kill <pid>), then quit and reopen\n"
                  "    Claude Desktop so its connection restarts with the current code.")
        print(f"Journal mode: {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
        print(f"Last sync finished: {get_state(conn, 'last_sync_finished') or 'never'}")
        err = get_state(conn, "last_sync_error")
        if err:
            print(f"Last sync error: {err}")
        conn.close()
    else:
        print("Database doesn't exist yet. Run ./start.sh first.")

    print("\nAll good." if ok else "\nSee the [!] items above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
