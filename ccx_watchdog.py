#!/usr/bin/env python3
"""Mantem o ``ccx.py auto`` vivo sem depender do terminal do VS Code.

O watchdog nao consulta usage. Ele so observa o heartbeat do lock e relanca o
monitor em um processo destacado quando ele morreu.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import ccx


def monitor_alive() -> bool:
    lock = ccx.STORE.parent / "auto.lock"
    try:
        if not lock.is_dir():
            return False
        # Um processo suspenso pode deixar o heartbeat velho por alguns
        # segundos, mas seu PID ainda existe. Nao crie um segundo monitor nesse
        # caso; o lock com dono morto e retomado pelo claude_lock imediatamente.
        pid = ccx.lock_owner_pid(lock)
        if pid is not None:
            return ccx.process_alive(pid)
        return time.time() - lock.stat().st_mtime < ccx.LOCK_STALE_S
    except FileNotFoundError:
        return False


def start_monitor() -> None:
    command = [sys.executable, str(Path(__file__).with_name("ccx.py")), "auto"]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    ccx.auto_event("watchdog relancou o monitor")


def main() -> int:
    if not monitor_alive():
        start_monitor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
