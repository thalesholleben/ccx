"""Abre, redimensiona e fecha Tk real com dados sintéticos. Pillow só no teste."""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fleet.panel import Panel
from fleet.store import Store

with tempfile.TemporaryDirectory(prefix='ccx-panel-smoke-') as temp:
    store=Store(Path(temp))
    now=time.time()
    for name,provider,plan,used in [('pessoal-pro','claude','pro',38),('agentes-max5','claude','max5',74),
                      ('principal-max20','claude','max20',92),('codex-plus','codex','plus',32),('codex-pro','codex','pro',71)]:
        worker=store.add_cell(name,provider,plan)
        store.bind(worker,'synthetic-'+name)
        with store.transaction() as db:
            db.execute('UPDATE cells SET windows=?,observed=? WHERE id=?',(json.dumps([
                {'key':'five_hour','used':used,'reset':now+10800,'seconds':18000},
                {'key':'seven_day','used':28,'reset':now+172800,'seconds':604800}]),now,name))
    store.submit('claude','Sintético',temp,'claude-opus-5-5',title='Revisar arquitetura da aplicação')
    panel=Panel(store)
    panel.render(store.snapshot())
    errors=[]
    panel.window.report_callback_exception=lambda *args:errors.append(''.join(__import__('traceback').format_exception(*args)))
    def grab(window=None):
        from PIL import ImageGrab
        import ctypes
        from ctypes import wintypes
        user=ctypes.WinDLL('user32')
        user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
        user.GetAncestor.restype=wintypes.HWND
        return ImageGrab.grab(window=user.GetAncestor((window or panel.window).winfo_id(),2))
    def capture(suffix,window=None):
        panel.render(store.snapshot())
        panel.window.update_idletasks()
        if len(sys.argv)>1:
            target=Path(sys.argv[1])
            shot=grab(window)
            shot.save(target.with_name(target.stem+'-'+suffix+'.png'))
        if panel.current_view=='overview':
            assert not hasattr(panel,'hero') and not hasattr(panel,'recent')
            assert len(panel.capacity.find_withtag('provider-logo'))==len(panel.snapshot['cells'])
            for cell in panel.snapshot['cells']:
                assert panel.capacity.find_withtag('cell:'+cell['id']), 'account_hidden'
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)
    def registration():
        captured=[]
        original=panel.dialog
        def record(*args):
            result=original(*args); captured.append(result); return result
        panel.dialog=record
        try: panel.add_cell()
        finally: panel.dialog=original
        dialog,frame,fields=captured[0]
        assert set(fields)=={'name','provider','plan'}
        assert len([w for w in descendants(dialog) if isinstance(w,ttk.Entry)])==3
        assert not any('Nova tarefa' in w.cget('text') for w in descendants(panel.window) if isinstance(w,ttk.Button))
        fields['name'].set('cadastro-simples')
        fields['plan'].set('Max 20 x20')
        fields['provider'].set('codex')
        assert fields['plan'].get()=='Plus x1'
        plan_entry=next(w for w in descendants(dialog) if isinstance(w,ttk.Combobox) and str(w.cget('textvariable'))==str(fields['plan']))
        assert tuple(plan_entry.cget('values'))==('Plus x1','Pro x5')
        fields['plan'].set('Pro x5')
        dialog.update_idletasks()
        next(w for w in descendants(dialog) if isinstance(w,ttk.Button) and w.cget('text')=='Adicionar conta').invoke()
    def check_registration():
        cell=store.one("SELECT * FROM cells WHERE display_name='cadastro-simples'")
        assert cell['id'].startswith('cell-') and cell['id']!='cadastro-simples'
        assert (cell['provider'],cell['plan'],cell['weight'],cell['weekly_weight'],cell['reserve'],cell['max_active'])==('codex','pro',5,1,10,0)
        panel.render(store.snapshot())
    def edit_account():
        panel.open_account('principal-max20')
        captured=[];original=panel.dialog
        def record(*args):
            result=original(*args);captured.append(result);return result
        panel.dialog=record
        try:panel.configure()
        finally:panel.dialog=original
        dialog,frame,fields=captured[0]
        assert set(fields)=={'name','plan'}
        fields['name'].set('Cliente correto')
        next(w for w in descendants(dialog) if isinstance(w,ttk.Button) and w.cget('text')=='Salvar').invoke()
    def check_edit():
        cell=store.one("SELECT * FROM cells WHERE id='principal-max20'")
        assert cell['display_name']=='Cliente correto' and cell['auth']=='ready' and cell['weight']==20
        panel.render(store.snapshot())
        assert panel.cells.item('principal-max20','text')=='Cliente correto'
        assert list(panel.cells.get_children())==[c['id'] for c in store.snapshot()['cells']]
    def later(delay,callback):
        panel.window.after(delay,callback)
    def remove_account():
        cell=store.one("SELECT * FROM cells WHERE display_name='cadastro-simples'")
        panel.open_account(cell['id'])
        with patch('fleet.panel.messagebox.askyesno',return_value=False):panel.remove_cell()
        assert store.one('SELECT id FROM cells WHERE id=?',(cell['id'],))
        with patch('fleet.panel.messagebox.askyesno',return_value=True):panel.remove_cell()
    def check_removed():
        assert not store.rows("SELECT * FROM cells WHERE display_name='cadastro-simples'")
        panel.render(store.snapshot())
        assert len(panel.cells.get_children())==5
        assert panel.details.cget('text')=='Selecione uma conta para ver limites, resets e perfis.'
    def check_refresh():
        panel.show_view('overview')
        entered,released=threading.Event(),threading.Event()
        def query(_):
            entered.set()
            assert released.wait(3), 'UI did not release background query'
            return 5
        def wait_done():
            deadline=time.monotonic()+4
            while panel.refreshing and time.monotonic()<deadline:
                panel.window.update()
                time.sleep(.01)
            assert not panel.refreshing, 'Refresh did not finish'
            assert not panel.refresh_button.instate(['disabled'])
        with patch('fleet.panel.providers.refresh_idle',side_effect=query) as refresh:
            panel.refresh_button.invoke()
            try:
                assert entered.wait(1)
                assert panel.refresh_button.instate(['disabled'])
                panel.refresh_button.invoke()
                panel.refresh_limits()
                painted=[]
                panel.window.after_idle(lambda:painted.append(True))
                panel.window.update()
                assert painted, 'Refresh blocked the UI'
                assert refresh.call_count==1, 'Duplicate refresh started'
            finally:
                released.set()
            wait_done()
            assert panel.notice.cget('text')=='5 conta(s) com nova leitura.'
        with patch('fleet.panel.providers.refresh_idle',return_value=0):
            panel.refresh_button.invoke()
            wait_done()
            assert panel.notice.cget('text').startswith('Sem nova leitura.')
        with patch('fleet.panel.providers.refresh_idle',side_effect=RuntimeError('synthetic-private-error')):
            panel.refresh_button.invoke()
            wait_done()
            assert panel.notice.cget('text')=='Falha na consulta. Tente novamente.'
        assert panel.refresh_button.cget('text')=='↻ Atualizar'
    later(100,registration)
    later(400,check_registration)
    later(500,lambda:panel.window.geometry('1440x820+0+0'))
    later(800,lambda:capture('1440'))
    later(1000,lambda:panel.window.geometry('1280x820+0+0'))
    later(1500,lambda:capture('1280'))
    later(1700,lambda:panel.window.geometry('1000x760+0+0'))
    later(2200,lambda:capture('1000'))
    later(2400,lambda:panel.open_account('principal-max20'))
    later(2600,lambda:capture('accounts'))
    later(2800,lambda:panel.nav['jobs'].invoke())
    later(3000,lambda:capture('jobs'))
    later(3200,lambda:panel.nav['activity'].invoke())
    later(3400,lambda:capture('activity'))
    later(3600,edit_account)
    later(4000,check_edit)
    later(4200,remove_account)
    later(4600,check_removed)
    def finish():
        try:check_refresh()
        finally:panel.window.destroy()
    later(4800,finish)
    panel.window.mainloop()
    if errors:
        raise AssertionError(errors)
print('Tk: cadastro, contas, 1440/1280/1000, refresh assíncrono (sucesso/espera/erro), navegação e fechamento sem erro.')
