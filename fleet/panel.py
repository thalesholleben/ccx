"""Cliente Tk: fechar esta janela não envia stop nem cancelamento."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import providers, service, terminal
from .presentation import Shell, BG
from .store import plans, PLAN_WEIGHTS
STATE = {'ready':'Autenticada','waiting_auth':'Login pendente','expired_refreshable':'Renovação pendente','queued':'Na fila','starting':'Iniciando',
         'running':'Executando','cancelling':'Cancelando','cancelled':'Cancelada','completed':'Concluída',
         'failed':'Falhou','needs_attention':'Requer atenção'}


def plan_choices(provider, current=None):
    names={'plus':'Plus','pro':'Pro','max5':'Max 5','max20':'Max 20','custom':'Personalizado'}
    return {(names[plan]+f' x{PLAN_WEIGHTS[provider][plan]}' if plan!='custom' else names[plan]):plan
            for plan in plans(provider) if plan!='custom' or current=='custom'}


class Panel(Shell):
    def __init__(self, store):
        self.store, self.messages, self.busy = store, queue.Queue(), False
        self.refreshing = False
        self.snapshot = {'cells':[], 'jobs':[]}
        self.build_shell()
        self.window.after(50,self.tick)

    def start_service(self):
        self.action(lambda:service.start(self.store))

    def stop_service(self):
        self.action(lambda:service.stop(self.store))

    def state_label(self,state):
        return STATE.get(state,state)

    def refresh_limits(self):
        if self.refreshing:
            return
        self.refreshing=True
        self.refresh_button.configure(text='↻ Consultando...',state='disabled')
        self.notice.configure(text='Consultando limites...')
        def task():
            try:
                updated=providers.refresh_idle(self.store)
                message=(f'{updated} conta(s) com nova leitura.' if updated else
                         'Sem nova leitura. Confira o estado das contas.')
            except Exception:
                message='Falha na consulta. Tente novamente.'
            self.messages.put(('refresh_done',message))
        threading.Thread(target=task,daemon=True).start()

    def action(self,callback):
        def task():
            try:
                callback()
                self.messages.put(('notice','Operação concluída.'))
            except Exception as exc:
                message=terminal.NAME_ERRORS.get(str(exc),'Operação recusada ('+type(exc).__name__+'). Verifique a seleção, os campos e o estado do serviço.')
                self.messages.put(('error',message))
        threading.Thread(target=task,daemon=True).start()
        self.notice.configure(text='Executando...')

    def tick(self):
        while not self.messages.empty():
            kind,data = self.messages.get_nowait()
            if kind=='snapshot':
                self.busy=False
                self.render(data)
            elif kind=='read_error':
                self.busy=False
                self.health.configure(text='Estado indisponível. Tentando novamente...')
            elif kind=='refresh_done':
                self.refreshing=False
                self.refresh_button.configure(text='↻ Atualizar',state='normal')
                self.notice.configure(text=data)
            else:
                self.notice.configure(text=data)
                if kind=='error':
                    messagebox.showerror('CCX',data,parent=self.window)
        if not self.busy:
            self.busy=True
            def read():
                try:
                    self.messages.put(('snapshot',self.store.snapshot()))
                except Exception:
                    self.messages.put(('read_error',None))
            threading.Thread(target=read,daemon=True).start()
        self.window.after(1000,self.tick)

    def render(self,data):
        self.snapshot=data
        for cell in data['cells']:
            quota=' | '.join(f"{terminal.duration(w['seconds'])}: {w['used']:.0f}%" for w in cell['windows'][:2]) or 'Sem leitura'
            reserved=max(cell['reserved'].values(),default=0)
            values=(cell['provider'],f"{cell['plan']} / x{cell['weight']:g}",'Pausada' if cell['paused'] else STATE.get(cell['auth'],cell['auth']),
                    quota,f'{reserved:.1f}pp máx.',str(cell['active']),
                    ('Vencida' if data['at']-cell['observed']>600 else terminal.duration(data['at']-cell['observed'])) if cell['observed'] else 'Desconhecida')
            self.row(self.cells,cell['id'],cell.get('display_name') or cell['id'],values)
            self.cells.move(cell['id'],'','end')
        names={cell['id']:cell.get('display_name') or cell['id'] for cell in data['cells']}
        for job in data['jobs']:
            account=names.get(job['cell_id'],job['cell_id']) or (job['removed_cell_name']+' (removida)' if job.get('removed_cell_name') else 'Automática')
            self.row(self.jobs,job['id'],job['id'][:8],(terminal.clean(job['title']),STATE.get(job['state'],job['state']),account,job['reason']))
        cell_ids={cell['id'] for cell in data['cells']}
        for key in self.cells.get_children():
            if key not in cell_ids:
                self.cells.delete(key)
        ids={job['id'] for job in data['jobs']}
        for key in self.jobs.get_children():
            if key not in ids:
                self.jobs.delete(key)
        self.detail()
        self.update_shell(data)
        if not data['cells']:
            self.details.configure(text='Comece em “Adicionar conta”. Cada perfil exige login próprio; os logins atuais do computador permanecem separados.')

    def row(self,tree,key,text,values):
        if tree.exists(key):
            tree.item(key,text=text,values=values)
        else:
            tree.insert('', 'end',iid=key,text=text,values=values)

    def selected(self,tree):
        selected=tree.selection()
        if not selected:
            self.notice.configure(text='Selecione uma linha primeiro.')
            return None
        return selected[0]

    def detail(self):
        selected=self.cells.selection()
        cell=next((c for c in self.snapshot['cells'] if selected and c['id']==selected[0]),None)
        if cell:
            parts=[]
            for win in cell['windows']:
                factor=cell['weight'] if win['seconds']<=86400 and not win.get('model') else cell['weekly_weight']
                free=max(0,100-win['used']-cell['reserved'].get(win['key'],0)-cell['reserve'])*factor/100
                parts.append(f"{win.get('model') or terminal.duration(win['seconds'])}: folga estimada {free:.2f}x, reset {terminal.reset_label(win['reset'])}")
            self.details.configure(text=' | '.join(parts)+f"\n{len(cell['workers'])} perfis • sem teto fixo de agentes"+(f" • {cell['error']}" if cell['error'] else '')+f"\nID para integrações: {cell['id']}")
        else:
            self.details.configure(text='Selecione uma conta para ver limites, resets e perfis.')

    def job_detail(self):
        selected=self.jobs.selection()
        if selected:
            self.job_info.configure(text=str(self.store.root/'jobs'/selected[0]/'result.json'))

    def dialog(self,title,fields):
        dialog=tk.Toplevel(self.window)
        dialog.title(title)
        dialog.configure(bg=BG)
        dialog.transient(self.window)
        frame=ttk.Frame(dialog,padding=24)
        frame.pack(fill='both',expand=True)
        entries={}
        for label,key,default,choices in fields:
            ttk.Label(frame,text=label).pack(anchor='w',pady=(10,3))
            var=tk.StringVar(value=str(default))
            entry=ttk.Combobox(frame,textvariable=var,values=choices,state='readonly',width=50) if choices else ttk.Entry(frame,textvariable=var,width=52)
            entry.pack(fill='x')
            entries[key]=var
        return dialog,frame,entries

    def add_cell(self):
        choices=plan_choices('claude')
        dialog,frame,fields=self.dialog('Adicionar conta',[
            ('Nome da conta (ex.: Cliente A)','name','',None),('Provedor','provider','claude',['claude','codex']),
            ('Plano','plan',next(iter(choices)),tuple(choices))])
        plan_entry=next(widget for widget in frame.winfo_children()
                        if isinstance(widget,ttk.Combobox) and str(widget.cget('textvariable'))==str(fields['plan']))
        def provider_changed(*_):
            choices=plan_choices(fields['provider'].get())
            plan_entry.configure(values=tuple(choices))
            if fields['plan'].get() not in choices:
                fields['plan'].set(next(iter(choices)))
        fields['provider'].trace_add('write',provider_changed)
        def save():
            values={k:v.get() for k,v in fields.items()}
            plan=plan_choices(values['provider'])[values['plan']]
            self.action(lambda:self.store.add_cell(None,values['provider'],plan,label=values['name']))
            dialog.destroy()
        ttk.Button(frame,text='Adicionar conta',style='Accent.TButton',command=save).pack(fill='x',pady=(20,0))

    def add_worker(self):
        cell=self.selected(self.cells)
        if cell:
            self.action(lambda:self.store.add_worker(cell))

    def configure(self):
        cell_id=self.selected(self.cells)
        cell=next((c for c in self.snapshot['cells'] if c['id']==cell_id),None)
        if not cell:
            return
        choices=plan_choices(cell['provider'],cell['plan'])
        current=next(label for label,plan in choices.items() if plan==cell['plan'])
        dialog,frame,fields=self.dialog('Editar conta',[
            ('Nome da conta','name',cell.get('display_name') or cell['id'],None),
            ('Plano','plan',current,tuple(choices))])
        def save():
            name=fields['name'].get()
            plan=choices[fields['plan'].get()]
            self.action(lambda:self.store.configure(cell_id,name=name,plan=plan if plan!=cell['plan'] else None))
            dialog.destroy()
        ttk.Button(frame,text='Salvar',command=save).pack(fill='x',pady=15)

    def remove_cell(self):
        cell_id=self.selected(self.cells)
        cell=next((c for c in self.snapshot['cells'] if c['id']==cell_id),None)
        if cell and messagebox.askyesno('Remover conta',
                'Remover '+(cell.get('display_name') or cell_id)+' do CCX?\n\n'
                'Os perfis locais e seus logins serão apagados. O histórico das tarefas será preservado.\n'
                'Isso não cancela a assinatura no provedor.',parent=self.window,default='no'):
            self.action(lambda:self.store.remove_cell(cell_id))

    def login(self):
        cell_id=self.selected(self.cells)
        cell=next((c for c in self.snapshot['cells'] if c['id']==cell_id),None)
        if not cell:
            return
        workers=[w['id'] for w in cell['workers'] if not w['job_id'] and not w['login']]
        if not workers:
            self.notice.configure(text='Todos os perfis desta conta estão ocupados.')
            return
        dialog,frame,fields=self.dialog('Login oficial',[('Perfil','worker',workers[0],workers)])
        def open_login():
            executable=Path(sys.executable).with_name('python.exe') if sys.platform=='win32' else Path(sys.executable)
            subprocess.Popen([str(executable),str(Path(__file__).resolve().parents[1]/'ccx-fleet.py'),'--root',str(self.store.root),
                              'cell','login',cell_id,'--worker',fields['worker'].get()],
                             creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform=='win32' else 0)
            dialog.destroy()
        ttk.Label(frame,text='O login abre no terminal e no navegador do provedor.',style='Muted.TLabel').pack(pady=12)
        ttk.Button(frame,text='Abrir login',command=open_login).pack(fill='x')

    def pause(self):
        cell_id=self.selected(self.cells)
        cell=next((c for c in self.snapshot['cells'] if c['id']==cell_id),None)
        if cell:
            self.action(lambda:self.store.pause(cell_id,not cell['paused']))

    def cancel(self):
        job=self.selected(self.jobs)
        if job:
            self.action(lambda:self.store.cancel(job))

def main(store):
    tray=None
    if os.name=='nt':
        from .tray import Tray
        tray=Tray(store.root)
        if tray.existing:
            return 0
    try:
        panel=Panel(store)
        if tray:
            try:
                tray.attach(panel.window)
            except OSError:
                # Sem Explorer/bandeja, o X continua fechando uma janela normal.
                panel.notice.configure(text='Bandeja indisponível. Fechar a janela preserva os agentes.')
        panel.window.mainloop()
    finally:
        if tray: tray.close()
    return 0
