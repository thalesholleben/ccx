"""Bandeja/instância única reais no Windows; serviço próprio em estado temporário."""
import ctypes
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from fleet import service
from fleet.panel import Panel
from fleet.store import Store
from fleet.tray import Tray, CALLBACK, OPEN, shell, user

with tempfile.TemporaryDirectory(prefix='ccx-tray-smoke-') as temp:
    store=Store(Path(temp))
    service.start(store)
    panel=Panel(store)
    tray=Tray(store.root)
    errors=[]
    panel.window.report_callback_exception=lambda *args:errors.append(str(args[1]))
    def pump():
        for _ in range(6):
            panel.window.update()
            time.sleep(.04)
    try:
        tray.attach(panel.window)
        pump()
        assert tray.added and panel.window.state()=='normal'
        user.PostMessageW(tray.hwnd,CALLBACK,1,0x202)
        pump()
        assert panel.window.state()=='withdrawn', 'left_click_did_not_hide'
        assert service.status(store)['alive'], 'hide_stopped_service'
        # A entrada pública reabre a instância existente e termina, sem segunda UI.
        child=subprocess.run([sys.executable,str(REPO/'ccx-panel.py'),'--root',temp],
                             capture_output=True,timeout=15)
        pump()
        assert child.returncode==0, child.stderr.decode(errors='replace')
        assert panel.window.state()=='normal', 'second_launch_did_not_show'
        panel.window.eval(panel.window.protocol('WM_DELETE_WINDOW'))
        pump()
        assert panel.window.state()=='withdrawn', 'window_x_did_not_hide'
        tray.command(1); pump()
        assert panel.window.state()=='normal'
        tray.command(2); pump()
        assert panel.window.state()=='withdrawn'
        # Simula apenas a notificação do Explorer, sem reiniciar o desktop real.
        assert shell.Shell_NotifyIconW(2,ctypes.byref(tray.data))
        tray.added=False
        user.PostMessageW(tray.hwnd,tray.taskbar_created,0,0)
        pump()
        assert tray.added, 'icon_did_not_return'
        assert not errors, errors
        tray.command(3)
        panel.window.mainloop()
        tray.close()
        assert service.status(store)['alive'], 'tray_exit_stopped_service'
        assert not user.FindWindowW(tray.name,None), 'tray_window_leaked'
        again=Tray(store.root)
        assert not again.existing, 'singleton_handle_leaked'
        again.close()
    finally:
        tray.close()
        service.stop(store)
        deadline=time.monotonic()+15
        while service.status(store)['alive'] and time.monotonic()<deadline:
            time.sleep(.1)
        assert not service.status(store)['alive']
print('Bandeja: clique, X, reabertura única, menu, recuperação do ícone e saída preservam serviço.')
