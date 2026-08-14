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
    return ccx.auto_monitor_alive()


def start_monitor() -> int:
    """Inicia o monitor e registra somente falhas precoces seguras."""
    command = [sys.executable, str(Path(__file__).with_name("ccx.py")), "auto"]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        try:
            code = child.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            ccx.auto_event("watchdog relancou o monitor")
            return 0
        ccx.auto_event(f"monitor encerrou antes de estabilizar (codigo {code})")
        return 1
    except Exception as exc:
        ccx.auto_event(f"watchdog nao iniciou o monitor ({type(exc).__name__})")
        return 1


def main() -> int:
    if not monitor_alive():
        return start_monitor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
