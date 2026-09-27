"""Exclusão do SO e processos com identidade e vida independentes da UI."""
from __future__ import annotations

import ctypes
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import ccx


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise BlockingIOError('resource_busy') from exc
    try:
        yield
    finally:
        stream.close()


def identity() -> dict:
    return {'pid': os.getpid(), 'process_start': ccx.process_start_marker(os.getpid())}


@contextmanager
def waiting_lock(path, timeout=30):
    deadline=time.monotonic()+timeout
    while True:
        manager=file_lock(path)
        try:
            manager.__enter__()
            break
        except BlockingIOError:
            if time.monotonic()>=deadline:
                raise
            time.sleep(.1)
    try:
        yield
    finally:
        manager.__exit__(None,None,None)


def live(pid: int | None, marker: str | None) -> bool:
    return bool(pid and ccx.owner_process_alive({'pid': pid, 'process_start': marker}))


if os.name == 'nt':
    from ctypes import wintypes as wt

    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wt.DWORD), ('min_working', ctypes.c_size_t),
                    ('max_working', ctypes.c_size_t), ('active', wt.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wt.DWORD),
                    ('scheduling', wt.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io', ctypes.c_uint64 * 6),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.IsProcessInJob.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)]
    kernel.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    kernel.CloseHandle.argtypes = [wt.HANDLE]


def job_flags() -> int:
    if os.name != 'nt':
        return 0
    inside = wt.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(inside)):
        raise OSError('job_membership_unknown')
    if not inside.value:
        return 0
    info = ExtendedLimits()
    if not kernel.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
        raise OSError('job_limits_unknown')
    return info.basic.flags


class ProcessTree:
    """Runner entra no Job antes de criar qualquer filho. Morte fecha a árvore."""
    def __init__(self):
        self.handle = None
        if os.name == 'nt':
            self.handle = kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise OSError('job_create_failed')
            info = ExtendedLimits()
            info.basic.flags = 0x2000
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                kernel.CloseHandle(self.handle)
                raise OSError('job_config_failed')
            if not kernel.AssignProcessToJobObject(self.handle, kernel.GetCurrentProcess()):
                kernel.CloseHandle(self.handle)
                raise OSError('job_assign_failed')
        elif os.getsid(0) != os.getpid():
            os.setsid()

    def terminate(self):
        if os.name == 'nt':
            if not kernel.TerminateJobObject(self.handle, 130):
                raise OSError('job_terminate_failed')
        else:
            os.killpg(os.getpgrp(), signal.SIGKILL)


def task_name(root: Path, purpose: str) -> str:
    digest = hashlib.sha256(str(root.resolve()).lower().encode()).hexdigest()[:12]
    return f'CCX-Fleet-{digest}-{purpose}'


def launch(root: Path, purpose: str, arguments: list[str], *, entry: Path | None = None) -> None:
    entry = entry or Path(__file__).resolve().parents[1] / 'ccx-fleet.py'
    executable = Path(sys.executable)
    if os.name == 'nt' and executable.with_name('pythonw.exe').is_file():
        executable = executable.with_name('pythonw.exe')
    argv = [str(executable), str(entry), '--root', str(root), *arguments]
    flags = job_flags()
    if os.name == 'nt' and flags & 0x2000 and not flags & (0x800 | 0x1000):
        script = Path(__file__).resolve().parents[1] / 'start-fleet-process.ps1'
        # PowerShell receives one string argument containing a Windows argv, not code.
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(script), '-TaskName', task_name(root, purpose),
             '-PythonPath', str(executable), '-Arguments', subprocess.list2cmdline(argv[1:]),
             '-WorkingDirectory', str(entry.parent)],
            capture_output=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise RuntimeError('bootstrap_failed: abra ccx-panel.cmd pelo Explorer e inicie o serviço.')
        return
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if flags & 0x800:
            options['creationflags'] |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    else:
        options['start_new_session'] = True
    child = subprocess.Popen(argv, cwd=entry.parent, **options)
    threading.Thread(target=child.wait, daemon=True).start()


def remove_task(root: Path, purpose: str) -> None:
    if os.name != 'nt':
        return
    subprocess.run(['schtasks.exe', '/Delete', '/TN', task_name(root, purpose), '/F'],
                   capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
