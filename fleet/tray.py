"""Bandeja Win32 com fila para o Tk: callbacks nativos nunca chamam Tcl."""
import ctypes as ct
import hashlib
import queue
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path

user = ct.WinDLL('user32', use_last_error=True)
shell = ct.WinDLL('shell32', use_last_error=True)
kernel = ct.WinDLL('kernel32', use_last_error=True)
LRESULT = ct.c_ssize_t
WNDPROC = ct.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
CALLBACK, OPEN = 0x8001, 0x8002


class WindowClass(ct.Structure):
    _fields_ = [('style', wt.UINT), ('procedure', WNDPROC), ('class_extra', ct.c_int),
                ('window_extra', ct.c_int), ('instance', wt.HINSTANCE), ('icon', wt.HICON),
                ('cursor', wt.HANDLE), ('background', wt.HBRUSH), ('menu', wt.LPCWSTR),
                ('name', wt.LPCWSTR)]


class NotifyIcon(ct.Structure):
    _fields_ = [('size', wt.DWORD), ('window', wt.HWND), ('id', wt.UINT), ('flags', wt.UINT),
                ('callback', wt.UINT), ('icon', wt.HICON), ('tip', wt.WCHAR * 128),
                ('state', wt.DWORD), ('state_mask', wt.DWORD), ('info', wt.WCHAR * 256),
                ('version', wt.UINT), ('info_title', wt.WCHAR * 64), ('info_flags', wt.DWORD),
                ('guid', ct.c_byte * 16), ('balloon_icon', wt.HICON)]


def signature(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, list(args)
    return function


signature(kernel, 'CreateMutexW', wt.HANDLE, ct.c_void_p, wt.BOOL, wt.LPCWSTR)
signature(kernel, 'CloseHandle', wt.BOOL, wt.HANDLE)
signature(kernel, 'GetModuleHandleW', wt.HMODULE, wt.LPCWSTR)
signature(user, 'RegisterClassW', wt.ATOM, ct.POINTER(WindowClass))
signature(user, 'UnregisterClassW', wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
signature(user, 'CreateWindowExW', wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
          ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ct.c_void_p)
signature(user, 'DefWindowProcW', LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
signature(user, 'FindWindowW', wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
signature(user, 'PostMessageW', wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
signature(user, 'SendMessageW', LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
signature(user, 'DestroyWindow', wt.BOOL, wt.HWND)
signature(user, 'RegisterWindowMessageW', wt.UINT, wt.LPCWSTR)
signature(user, 'CreateIcon', wt.HICON, wt.HINSTANCE, ct.c_int, ct.c_int,
          wt.BYTE, wt.BYTE, ct.c_void_p, ct.c_void_p)
signature(user, 'DestroyIcon', wt.BOOL, wt.HICON)
signature(user, 'LoadImageW', wt.HANDLE, wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ct.c_int, ct.c_int, wt.UINT)
signature(user, 'GetAncestor', wt.HWND, wt.HWND, wt.UINT)
signature(user, 'SetForegroundWindow', wt.BOOL, wt.HWND)
signature(user, 'CreatePopupMenu', wt.HMENU)
signature(user, 'AppendMenuW', wt.BOOL, wt.HMENU, wt.UINT, ct.c_size_t, wt.LPCWSTR)
signature(user, 'TrackPopupMenu', wt.UINT, wt.HMENU, wt.UINT, ct.c_int, ct.c_int,
          ct.c_int, wt.HWND, ct.c_void_p)
signature(user, 'GetCursorPos', wt.BOOL, ct.POINTER(wt.POINT))
signature(user, 'DestroyMenu', wt.BOOL, wt.HMENU)
signature(user, 'GetMessageW', wt.BOOL, ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
signature(user, 'TranslateMessage', wt.BOOL, ct.POINTER(wt.MSG))
signature(user, 'DispatchMessageW', LRESULT, ct.POINTER(wt.MSG))
signature(user, 'PostQuitMessage', None, ct.c_int)
signature(shell, 'Shell_NotifyIconW', wt.BOOL, wt.DWORD, ct.POINTER(NotifyIcon))


class Tray:
    def __init__(self, root):
        digest = hashlib.sha256(str(root.resolve()).lower().encode()).hexdigest()[:16]
        self.name = 'CCX-Fleet-Panel-' + digest
        self.window = self.hwnd = self.icon = None
        self.registered = self.added = self.existing = False
        self.last_click = 0
        self.events, self.ready = queue.Queue(), threading.Event()
        self.failure, self.thread = None, None
        self.instance = kernel.GetModuleHandleW(None)
        self.mutex = kernel.CreateMutexW(None, False, 'Local\\' + self.name)
        if not self.mutex:
            raise OSError('panel_mutex_failed')
        if ct.get_last_error() == 183:
            # A segunda abertura traz o painel existente, sem um segundo ícone.
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    hwnd = user.FindWindowW(self.name, None)
                    if hwnd and user.PostMessageW(hwnd, OPEN, 0, 0):
                        self.existing = True
                        return
                    time.sleep(.05)
                raise OSError('panel_already_starting')
            finally:
                self.close()
        self.thread=threading.Thread(target=self.native_loop,daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or self.failure:
            self.close()
            raise OSError('tray_start_failed')

    def native_loop(self):
        try:
            self.callback = WNDPROC(self.message)
            self.window_class = WindowClass(procedure=self.callback, instance=self.instance, name=self.name)
            if not user.RegisterClassW(ct.byref(self.window_class)):
                raise OSError('tray_class_failed')
            self.registered = True
            self.hwnd = user.CreateWindowExW(0, self.name, 'CCX', 0, 0, 0, 0, 0,
                                              None, None, self.instance, None)
            if not self.hwnd:
                raise OSError('tray_window_failed')
            self.taskbar_created = user.RegisterWindowMessageW('TaskbarCreated')
            self.icon = self.create_icon()
            self.data = NotifyIcon(size=ct.sizeof(NotifyIcon), window=self.hwnd, id=1,
                                   flags=1 | 2 | 4, callback=CALLBACK, icon=self.icon,
                                   tip='CCX · clique para abrir ou ocultar o painel')
            self.add()
            self.ready.set()
            message=wt.MSG()
            while user.GetMessageW(ct.byref(message),None,0,0)>0:
                user.TranslateMessage(ct.byref(message))
                user.DispatchMessageW(ct.byref(message))
        except Exception as exc:
            self.failure=type(exc).__name__
        finally:
            self.ready.set()
            self.dispose_native()

    def create_icon(self):
        path = Path(__file__).resolve().parents[1] / 'assets' / 'ccx-icon.ico'
        icon = user.LoadImageW(None, str(path), 1, 32, 32, 0x10)
        if icon:
            return icon
        # Ícone geométrico de três capacidades, desenhado sem dependências.
        pixels = bytearray()
        for y in range(32):
            for x in range(32):
                bar = (6 <= x < 11 and 17 <= y < 26 or
                       14 <= x < 19 and 11 <= y < 26 or
                       22 <= x < 27 and 5 <= y < 26)
                corner = (x < 3 or x > 28) and (y < 3 or y > 28)
                pixels.extend((198, 228, 122, 255) if bar else (19, 16, 12, 0 if corner else 255))
        mask = ct.create_string_buffer(bytes(128))
        color = ct.create_string_buffer(bytes(pixels))
        icon = user.CreateIcon(self.instance, 32, 32, 1, 32, mask, color)
        if not icon:
            raise OSError('tray_icon_failed')
        return icon

    def attach(self, window):
        self.window = window
        self.pump()
        if not self.added:
            raise OSError('tray_unavailable')
        window.protocol('WM_DELETE_WINDOW', self.hide)

    def pump(self):
        while not self.events.empty():
            event=self.events.get_nowait()
            if event=='exit':
                self.window.destroy()
                return
            if event=='show': self.show()
            elif event=='hide': self.hide()
            elif event=='toggle': self.toggle()
        self.window.after(100,self.pump)

    def add(self):
        self.added = bool(shell.Shell_NotifyIconW(0, ct.byref(self.data)))
        return self.added

    def show(self):
        self.window.deiconify()
        self.window.lift()
        hwnd = user.GetAncestor(self.window.winfo_id(), 2)
        user.SetForegroundWindow(hwnd)

    def hide(self):
        self.window.withdraw()

    def toggle(self):
        if self.window.state() in ('withdrawn', 'iconic'):
            self.show()
        else:
            self.hide()

    def menu(self):
        menu = user.CreatePopupMenu()
        try:
            for number, title in [(1, 'Abrir painel'), (2, 'Ocultar painel'),
                                  (0, None), (3, 'Sair da bandeja (agentes continuam)')]:
                user.AppendMenuW(menu, 0x800 if number == 0 else 0, number, title)
            point = wt.POINT()
            user.GetCursorPos(ct.byref(point))
            user.SetForegroundWindow(self.hwnd)
            command = user.TrackPopupMenu(menu, 0x100 | 0x2, point.x, point.y, 0, self.hwnd, None)
            user.PostMessageW(self.hwnd, 0, 0, 0)
            self.command(command)
        finally:
            user.DestroyMenu(menu)

    def command(self, command):
        event={1:'show',2:'hide',3:'exit'}.get(command)
        if event: self.events.put(event)

    def message(self, hwnd, message, wparam, lparam):
        if message == 0x10:
            user.PostQuitMessage(0)
            return 0
        if message == OPEN:
            self.events.put('show')
            return 0
        if message == CALLBACK:
            if lparam == 0x202 and time.monotonic() - self.last_click > .4:
                self.last_click = time.monotonic()
                self.events.put('toggle')
            elif lparam in (0x205, 0x7B):
                self.menu()
            return 0
        if message == getattr(self,'taskbar_created',None):
            self.add()
            return 0
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    def dispose_native(self):
        if self.added:
            shell.Shell_NotifyIconW(2, ct.byref(self.data))
            self.added = False
        if self.hwnd:
            user.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.icon:
            user.DestroyIcon(self.icon)
            self.icon = None
        if self.registered:
            user.UnregisterClassW(self.name, self.instance)
            self.registered = False

    def close(self):
        if self.thread and self.thread.is_alive():
            if self.hwnd: user.PostMessageW(self.hwnd,0x10,0,0)
            self.thread.join(timeout=5)
        self.window = None
        if self.mutex:
            kernel.CloseHandle(self.mutex)
            self.mutex = None
