"""Apresentação Tk da frota: nenhuma operação de serviço ou credencial."""
import math
import os
import time
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from . import terminal

BG, NAV, CARD, RAISED = '#0c1013', '#101619', '#151d22', '#1d2930'
TEXT, MUTED, ACCENT, LINE = '#edf4f1', '#a0b0b7', '#7ae4c6', '#263239'
WARN, BAD = '#edbd70', '#f09191'


def rounded(canvas, x, y, width, height, radius=14, **kwargs):
    r=min(radius,width/2,height/2)
    return canvas.create_polygon(x+r,y,x+width-r,y,x+width,y,x+width,y+r,
        x+width,y+height-r,x+width,y+height,x+width-r,y+height,x+r,y+height,
        x,y+height,x,y+height-r,x,y+r,x,y,smooth=True,**kwargs)


def label(canvas,x,y,text,*,font=('Segoe UI',10),fill=TEXT,anchor='nw',**kwargs):
    return canvas.create_text(x,y,text=terminal.clean(text),font=font,fill=fill,anchor=anchor,**kwargs)


def elapsed(age):
    if age<60: return 'agora'
    return f'há {int(age/60)}min' if age<3600 else f'há {age/3600:.1f}h'


class Capacity(tk.Canvas):
    def __init__(self,parent,owner):
        super().__init__(parent,bg=BG,highlightthickness=0,height=156,cursor='hand2')
        self.owner=owner
        self.data=[]
        assets=Path(__file__).resolve().parents[1]/'assets'
        self.provider_icons={}
        for provider,filename in [('claude','claude-logo.png'),('codex','openai-logo.png')]:
            if (assets/filename).is_file():
                self.provider_icons[provider]=tk.PhotoImage(master=self,file=str(assets/filename)).subsample(2)
        self.name_font=tkfont.Font(self,family='Segoe UI Semibold',size=11)
        self.bind('<Configure>',lambda _:self.draw())

    def draw(self):
        self.delete('all')
        width=max(400,self.winfo_width())
        cols=3 if width>=990 else 2 if width>=650 else 1
        if not self.data:
            self.configure(height=112)
            rounded(self,0,0,width,108,fill=CARD,outline=LINE)
            label(self,18,18,'Adicione sua primeira conta.',font=(self.owner.display,15,'bold'))
            label(self,18,49,'Escolha o provedor e o plano. Depois, faça login.',fill=MUTED)
            label(self,18,76,'Os limites aparecem após a autenticação.',font=('Segoe UI',9),fill=MUTED)
            return
        cells=self.data
        card_width=(width-10*(cols-1))/cols
        self.configure(height=math.ceil(len(cells)/cols)*166-10)
        for i,cell in enumerate(cells):
            x,y=(i%cols)*(card_width+10),(i//cols)*166
            tag='cell:'+cell['id']
            rounded(self,x,y,card_width,156,10,fill=CARD,outline=LINE,tags=tag)
            name=cell.get('display_name') or cell['id']
            icon=self.provider_icons.get(cell['provider'])
            left=x+46 if icon else x+16
            if icon:self.create_image(x+16,y+31,image=icon,anchor='w',tags=(tag,'provider-logo'))
            room=card_width-(left-x)-68
            if self.name_font.measure(name)>room:
                while name and self.name_font.measure(name+'…')>room:name=name[:-1]
                name+='…'
            label(self,left,y+12,name,font=self.name_font,tags=tag)
            rounded(self,x+card_width-56,y+10,40,23,6,fill=RAISED,outline='',tags=tag)
            label(self,x+card_width-36,y+21,f"x{cell['weight']:g}",font=(self.owner.mono,9),fill=ACCENT,anchor='center',tags=tag)
            known=cell['windows']
            main=min((w for w in known if not w.get('model') and w['seconds']<=86400),key=lambda w:w['seconds'],default=None)
            weekly=max((w for w in known if not w.get('model') and w['seconds']>86400),key=lambda w:w['seconds'],default=None)
            stale=not cell['observed'] or time.time()-cell['observed']>600
            auth='Pausada' if cell['paused'] else 'Login pendente' if cell['auth']!='ready' else 'Leitura vencida' if stale else 'Leitura atual'
            state_color=MUTED if cell['paused'] else WARN if auth!='Leitura atual' else ACCENT
            label(self,left,y+36,cell['provider'].upper()+' / '+cell['plan'].upper(),font=('Segoe UI',8),fill=MUTED,tags=tag)
            label(self,x+card_width-16,y+36,auth,font=('Segoe UI',8),fill=state_color,anchor='ne',tags=tag)
            bar_width=(card_width-48)/2
            for column,(title,window) in enumerate((('Sessão',main),('Semanal',weekly))):
                left=x+16+column*(bar_width+16)
                label(self,left,y+65,title,font=('Segoe UI',9),fill=MUTED,tags=tag)
                label(self,left+bar_width,y+65,f"{window['used']:.0f}%" if window else 'n/d',font=(self.owner.mono,10),anchor='ne',tags=tag)
                rounded(self,left,y+91,bar_width,5,2,fill=RAISED,outline='',tags=tag)
                if not window: continue
                used=window['used']; reserved=cell['reserved'].get(window['key'],0)
                if used:
                    rounded(self,left,y+91,max(2,bar_width*used/100),5,2,fill=BAD if used>=90 else WARN if used>=70 else ACCENT,outline='',tags=tag)
                if reserved:
                    start=left+bar_width*used/100
                    amount=bar_width*min(reserved,100-used)/100
                    if amount>0: rounded(self,start,y+91,amount,5,2,fill=WARN,outline='',tags=tag)
            self.create_line(x+16,y+111,x+card_width-16,y+111,fill=LINE,tags=tag)
            reference=main or weekly
            free=None
            if reference and not stale:
                factor=cell['weight'] if main else cell['weekly_weight']
                free=max(0,100-reference['used']-cell['reserved'].get(reference['key'],0)-cell['reserve'])*factor/100
            label(self,x+16,y+125,f'Folga {free:.2f}x' if free is not None else 'Folga não disponível',font=('Segoe UI',9),fill=ACCENT if free is not None else MUTED,tags=tag)
            label(self,x+card_width-16,y+125,f"{cell['active']} agentes  ›",font=('Segoe UI',9),anchor='ne',tags=tag)
            self.tag_bind(tag,'<Button-1>',lambda _,key=cell['id']:self.owner.open_account(key))


class Shell:
    def build_shell(self):
        if os.name == 'nt':
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('CCX.Fleet.Panel')
        self.window=tk.Tk()
        self.window.title('CCX | Frota de agentes')
        icon = Path(__file__).resolve().parents[1] / 'assets' / 'ccx-icon.ico'
        if os.name == 'nt' and icon.is_file():
            self.window.iconbitmap(default=str(icon))
            self.window.iconbitmap(str(icon))
        self.window.geometry('1280x820')
        self.window.minsize(900,600)
        self.window.configure(bg=BG)
        families=set(tkfont.families())
        self.display='Bahnschrift' if 'Bahnschrift' in families else 'Segoe UI Semibold'
        self.mono='Cascadia Code' if 'Cascadia Code' in families else 'Consolas'
        self.style=ttk.Style(self.window)
        self.style.theme_use('clam')
        s=self.style
        s.configure('.',background=BG,foreground=TEXT,font=('Segoe UI',10))
        s.configure('TFrame',background=BG)
        s.configure('TLabel',background=BG,foreground=TEXT)
        s.configure('Muted.TLabel',foreground=MUTED)
        s.configure('TButton',background=RAISED,foreground=TEXT,padding=(12,7),borderwidth=0,relief='flat')
        s.map('TButton',background=[('active','#2d3e47')],bordercolor=[('focus',ACCENT)])
        s.configure('Accent.TButton',background=ACCENT,foreground=BG,font=('Segoe UI Semibold',10))
        s.map('Accent.TButton',background=[('active','#a6f1db')],foreground=[('active',BG)])
        s.configure('Ghost.TButton',background=BG,foreground=MUTED)
        s.configure('Nav.TButton',background=NAV,foreground=MUTED,anchor='w',padding=(12,9))
        s.configure('Selected.Nav.TButton',background='#1d302d',foreground=ACCENT,anchor='w')
        s.configure('Treeview',background=CARD,fieldbackground=CARD,foreground=TEXT,rowheight=32,borderwidth=0,relief='flat')
        s.layout('Treeview',[('Treeview.treearea',{'sticky':'nswe'})])
        s.configure('Treeview.Heading',background=BG,foreground=MUTED,font=('Segoe UI',9),padding=(10,10),borderwidth=0,relief='flat')
        s.map('Treeview',background=[('selected','#25433d')],foreground=[('selected',TEXT)])
        for orientation in ('Vertical','Horizontal'):
            scrollbar=orientation+'.TScrollbar'
            s.layout(scrollbar,[(orientation+'.Scrollbar.trough',{'sticky':'ns' if orientation=='Vertical' else 'ew',
                'children':[(orientation+'.Scrollbar.thumb',{'sticky':'nswe'})]})])
            s.configure(scrollbar,background='#35464f',troughcolor=BG,bordercolor=BG,
                        lightcolor='#35464f',darkcolor='#35464f',borderwidth=0,width=8,arrowsize=8,gripcount=0)
            s.map(scrollbar,background=[('pressed',ACCENT),('active','#637c87')],
                  lightcolor=[('pressed',ACCENT),('active','#637c87')],
                  darkcolor=[('pressed',ACCENT),('active','#637c87')])
        s.configure('TEntry',fieldbackground=CARD,foreground=TEXT,insertcolor=TEXT,padding=8,bordercolor=LINE)
        s.map('TEntry',bordercolor=[('focus',ACCENT)])
        s.configure('TCombobox',fieldbackground=CARD,background=RAISED,foreground=TEXT,arrowcolor=ACCENT,padding=8)
        s.map('TCombobox',fieldbackground=[('readonly',CARD)],foreground=[('readonly',TEXT)])
        s.configure('TLabelframe',background=BG,bordercolor=LINE)
        self.window.option_add('*TCombobox*Listbox.background',CARD)
        self.window.option_add('*TCombobox*Listbox.foreground',TEXT)
        self.sidebar=tk.Frame(self.window,bg=NAV,width=170)
        self.sidebar.pack(side='left',fill='y')
        self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar,text='CCX',bg=NAV,fg=TEXT,font=(self.display,25,'bold'),anchor='w').pack(fill='x',padx=18,pady=(18,2))
        self.brand_caption=tk.Label(self.sidebar,text='FROTA DE AGENTES',bg=NAV,fg=MUTED,font=('Segoe UI',8),anchor='w')
        self.brand_caption.pack(fill='x',padx=20,pady=(0,22))
        self.nav={}
        self.view_names={'overview':'Capacidade','accounts':'Contas','jobs':'Tarefas','activity':'Atividade'}
        for key,text in self.view_names.items():
            button=ttk.Button(self.sidebar,text=text,style='Nav.TButton',command=lambda key=key:self.show_view(key))
            button.pack(fill='x',padx=12,pady=3)
            self.nav[key]=button
        self.sidebar_state=tk.Label(self.sidebar,text='●  Serviço parado',bg=NAV,fg=MUTED,font=('Segoe UI',9),anchor='w')
        self.sidebar_state.pack(side='bottom',fill='x',padx=18,pady=(0,18))
        tk.Label(self.sidebar,text='EXECUÇÃO LOCAL',bg=NAV,fg=MUTED,font=('Segoe UI',8),anchor='w').pack(side='bottom',fill='x',padx=18,pady=(0,8))
        main=ttk.Frame(self.window)
        main.pack(side='left',fill='both',expand=True)
        top=ttk.Frame(main,padding=(20,12))
        top.pack(fill='x')
        self.breadcrumb=ttk.Label(top,text='Workspace  /  Visão geral',style='Muted.TLabel')
        self.breadcrumb.pack(side='left')
        self.start_button=ttk.Button(top,text='Iniciar serviço',style='Accent.TButton',command=self.start_service)
        self.start_button.pack(side='right')
        ttk.Button(top,text='Parar admissão',style='Ghost.TButton',command=self.stop_service).pack(side='right',padx=12)
        self.health=ttk.Label(top,text='Lendo estado...',style='Muted.TLabel')
        self.health.pack(side='right',padx=18)
        tk.Frame(main,height=1,bg=LINE).pack(fill='x')
        content=ttk.Frame(main)
        content.pack(fill='both',expand=True)
        self.scroll=tk.Canvas(content,bg=BG,highlightthickness=0)
        self.scroll.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(content,orient='vertical',command=self.scroll.yview)
        scroll.pack(side='right',fill='y')
        self.scroll.configure(yscrollcommand=scroll.set)
        self.body=ttk.Frame(self.scroll,padding=(20,10,20,16))
        self.body_id=self.scroll.create_window(0,0,window=self.body,anchor='nw')
        self.scroll.bind('<Configure>',lambda e:self.scroll.itemconfigure(self.body_id,width=e.width))
        self.body.bind('<Configure>',lambda _:self.scroll.configure(scrollregion=self.scroll.bbox('all')))
        self.window.bind('<MouseWheel>',self.wheel,add='+')
        self.window.bind('<Configure>',self.adapt_layout,add='+')
        self.views={key:ttk.Frame(self.body) for key in self.view_names}
        self.build_overview(self.views['overview'])
        self.build_accounts(self.views['accounts'])
        self.build_jobs(self.views['jobs'])
        self.build_activity(self.views['activity'])
        footer=ttk.Frame(main,padding=(20,8))
        footer.pack(fill='x')
        self.notice=ttk.Label(footer,text='Pronto.',style='Muted.TLabel')
        self.notice.pack(side='left')
        ttk.Label(footer,text='Fechar o painel mantém as tarefas em execução.',style='Muted.TLabel').pack(side='right')
        self.show_view('overview')

    def adapt_layout(self,event):
        if event.widget is not self.window: return
        compact=event.width<1200
        self.sidebar.configure(width=154 if compact else 170)
        if event.width<1150:
            self.health.pack_forget()
        elif not self.health.winfo_manager():
            self.health.pack(side='right',padx=18)

    def wheel(self,event):
        if isinstance(event.widget,(tk.Text,ttk.Treeview,ttk.Combobox)):
            return
        self.scroll.yview_scroll(int(-event.delta/120),'units')

    def show_view(self,key):
        for view in self.views.values(): view.pack_forget()
        self.views[key].pack(fill='both',expand=True)
        self.current_view=key
        self.breadcrumb.configure(text='Workspace  /  '+self.view_names[key])
        for name,button in self.nav.items():
            button.configure(style='Selected.Nav.TButton' if name==key else 'Nav.TButton')
        self.scroll.yview_moveto(0)

    def heading(self,parent,title,subtitle='',action=None):
        frame=ttk.Frame(parent)
        frame.pack(fill='x',pady=(6,10))
        ttk.Label(frame,text=title,font=(self.display,18,'bold')).pack(side='left')
        if action:
            text,callback=action
            ttk.Button(frame,text=text,style='Ghost.TButton',command=callback).pack(side='right')
        if subtitle: ttk.Label(parent,text=subtitle,style='Muted.TLabel').pack(anchor='w',pady=(0,12))

    def build_overview(self,parent):
        self.heading(parent,'Capacidade da frota',action=('＋ Adicionar conta',self.add_cell))
        self.metrics=tk.Canvas(parent,bg=BG,highlightthickness=0,height=54)
        self.metrics.pack(fill='x',pady=(0,14))
        self.metrics.bind('<Configure>',lambda _:self.draw_metrics())
        self.capacity=Capacity(parent,self)
        self.capacity.pack(fill='x')
        legend=ttk.Frame(parent)
        legend.pack(fill='x',pady=(10,0))
        ttk.Label(legend,text='●  Uso medido',foreground=ACCENT).pack(side='left')
        ttk.Label(legend,text='●  Reservas',foreground=WARN).pack(side='left',padx=18)
        ttk.Label(legend,text='Folga estimada em x1 · limites semanais se aplicam',style='Muted.TLabel').pack(side='right')

    def draw_metrics(self):
        if not hasattr(self,'snapshot'): return
        data=self.snapshot
        if 'service' not in data: return
        self.metrics.delete('all')
        width=max(500,self.metrics.winfo_width()); unit=width/3
        cells=data['cells']; jobs=data['jobs']
        active=sum(c['active'] for c in cells)
        queued=sum(j['state']=='queued' for j in jobs)
        ready=sum(c['auth']=='ready' and not c['paused'] and 0<=data['at']-c['observed']<=600 for c in cells)
        stats=[('CONTAS COM LEITURA',f'{ready:02d}',f'de {len(cells)} cadastradas'),
               ('AGENTES ATIVOS',f'{active:02d}','sem teto fixo'),
               ('FILA RECENTE',f'{queued:02d}','aguardando execução')]
        for i,(title,value,context) in enumerate(stats):
            x=i*unit
            if i: self.metrics.create_line(x-6,4,x-6,48,fill=LINE)
            label(self.metrics,x+8,4,title,font=('Segoe UI',8),fill=MUTED)
            label(self.metrics,x+8,23,value,font=(self.mono,18),fill=ACCENT if i==0 else TEXT)
            label(self.metrics,x+56,30,context,font=('Segoe UI',9),fill=MUTED,width=max(100,unit-70))

    def build_accounts(self,parent):
        self.heading(parent,'Contas da frota','Cada conta tem sua própria capacidade. Perfis da mesma conta compartilham os limites.',('＋ Adicionar conta',self.add_cell))
        self.cells=self.table(parent,[('provider','PROVEDOR',85),('plan','PLANO / PESO',110),('state','ESTADO',130),
                                     ('quota','USO MEDIDO',195),('reserved','RESERVAS',110),('load','AGENTES',85),('age','LEITURA',95)],9)
        self.cells.bind('<<TreeviewSelect>>',lambda _:self.detail())
        toolbar=ttk.Frame(parent)
        toolbar.pack(fill='x',pady=18)
        for text,callback in [('Fazer login',self.login),('Novo perfil',self.add_worker),('Editar conta',self.configure),('Pausar / retomar',self.pause),('Remover conta',self.remove_cell)]:
            ttk.Button(toolbar,text=text,command=callback).pack(side='left',padx=(0,10))
        self.details=ttk.Label(parent,text='Selecione uma conta para ver limites, resets e perfis.',style='Muted.TLabel',wraplength=750,justify='left')
        self.details.pack(anchor='w',pady=14)

    def build_jobs(self,parent):
        self.heading(parent,'Tarefas','Execuções enviadas pelos agentes e integrações, da fila ao resultado.')
        self.jobs=self.table(parent,[('title','TAREFA',280),('state','ESTADO',150),('cell','CONTA',135),('reason','MOTIVO / ESPERA',250)],10)
        self.jobs.bind('<<TreeviewSelect>>',lambda _:self.job_detail())
        ttk.Button(parent,text='Cancelar tarefa selecionada',command=self.cancel).pack(anchor='w',pady=18)
        self.job_info=ttk.Label(parent,text='Selecione uma tarefa para localizar seu resultado.',style='Muted.TLabel',wraplength=800)
        self.job_info.pack(anchor='w',pady=10)

    def build_activity(self,parent):
        self.heading(parent,'Atividade','Eventos recentes do serviço. Conteúdo das tarefas permanece privado.')
        self.events=self.table(parent,[('at','HORÁRIO',120),('kind','EVENTO',240),('entity','REFERÊNCIA',330)],12)

    def table(self,parent,columns,height,expand=False):
        frame=ttk.Frame(parent)
        frame.pack(fill='both' if expand else 'x',expand=expand)
        tree=ttk.Treeview(frame,columns=[c[0] for c in columns],height=height,selectmode='browse')
        tree.heading('#0',text='CONTA' if columns[0][0]=='provider' else 'ID')
        tree.column('#0',width=155 if columns[0][0]=='provider' else 90,minwidth=80,anchor='w')
        for key,title,width in columns:
            tree.heading(key,text=title,anchor='w')
            tree.column(key,width=width,minwidth=65,anchor='w')
        y=ttk.Scrollbar(frame,orient='vertical',command=tree.yview)
        x=ttk.Scrollbar(frame,orient='horizontal',command=tree.xview)
        tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        tree.grid(row=0,column=0,sticky='nsew'); y.grid(row=0,column=1,sticky='ns'); x.grid(row=1,column=0,sticky='ew')
        frame.columnconfigure(0,weight=1); frame.rowconfigure(0,weight=1)
        return tree

    def open_account(self,key):
        self.show_view('accounts')
        self.cells.selection_set(key)
        self.cells.see(key)
        self.cells.focus(key)
        self.cells.focus_set()
        self.detail()

    def update_shell(self,data):
        daemon=data['service']
        healthy=daemon['alive'] and data['at']-daemon['heartbeat']<60 and not daemon['stop']
        state='Serviço ativo' if healthy else 'Parando' if daemon['alive'] and daemon['stop'] else 'Sem resposta' if daemon['alive'] else 'Serviço parado'
        self.health.configure(text='●  '+state,foreground=ACCENT if healthy else MUTED)
        self.sidebar_state.configure(text='●  '+state,fg=ACCENT if healthy else MUTED)
        self.start_button.configure(text='Serviço ativo' if healthy else 'Iniciar serviço',state='disabled' if healthy else 'normal')
        self.capacity.data=data['cells']; self.capacity.draw(); self.draw_metrics()
        self.events.delete(*self.events.get_children())
        names={cell['id']:cell.get('display_name') or cell['id'] for cell in data['cells']}
        for i,item in enumerate(data['events'][:50]):
            reference=names.get(item['entity'],item['entity'])
            self.events.insert('', 'end',text=f'{i+1:02d}',values=(time.strftime('%H:%M:%S',time.localtime(item['at'])),item['kind'],reference[:48] or 'Serviço'))
