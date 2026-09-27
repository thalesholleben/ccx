"""Status legível sem dependências; cor só em terminal, JSON em outro caminho."""
import math
import os
import shutil
import sys
import time
import unicodedata

import ccx


def clean(value):
    return ''.join(char for char in str(value) if not unicodedata.category(char).startswith('C'))


def visual(args):
    return bool(getattr(args, 'visual', False) or sys.stdout.isatty())


class Terminal:
    def __init__(self, no_color=False, width=None):
        self.width = max(24, min(width or shutil.get_terminal_size((100, 30)).columns, 120))
        self.color = sys.stdout.isatty() and not no_color and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
        if self.color and os.name=='nt':
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.GetStdHandle.argtypes=[wintypes.DWORD]
            kernel.GetStdHandle.restype=wintypes.HANDLE
            kernel.GetConsoleMode.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
            kernel.SetConsoleMode.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            handle=kernel.GetStdHandle(-11); mode=wintypes.DWORD()
            self.color=bool(kernel.GetConsoleMode(handle,ctypes.byref(mode)) and kernel.SetConsoleMode(handle,mode.value|4))
        self.encoding = getattr(sys.stdout,'encoding',None) or 'utf-8'
        try:
            '━●░█'.encode(self.encoding)
            self.unicode = True
        except UnicodeEncodeError:
            self.unicode = False

    def paint(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def line(self, text='', color=None):
        text = clean(text)[:self.width].encode(self.encoding,errors='replace').decode(self.encoding)
        print(self.paint(text,color) if color else text)

    def rule(self):
        self.line(('━' if self.unicode else '=') * self.width, '90')

    def bar(self, used, size=18):
        size = min(size, max(5,self.width-22))
        if not isinstance(used,(int,float)) or not math.isfinite(used) or not 0<=used<=100:
            return '['+'?'*size+']  sem leitura'
        filled = round(used/100*size)
        full,empty = ('█','░') if self.unicode else ('#','.')
        return '['+full*filled+empty*(size-filled)+f'] {used:5.1f}%'


def duration(seconds):
    if seconds <= 0:
        return 'aguarda leitura'
    if seconds >= 86400:
        return f'{seconds/86400:.1f}d'
    if seconds >= 3600:
        return f'{seconds/3600:.1f}h'
    return f'{max(1,round(seconds/60))}min'


def reset_label(reset, now=None):
    return 'ainda não informado' if reset is None else duration(reset-(time.time() if now is None else now))


def legacy(store, usage_map, err_map, active, provider, args, labels=None):
    out = Terminal(getattr(args,'no_color',False))
    out.rule()
    out.line('  LIMITES DAS CONTAS  /  '+provider.upper(), '1;36')
    out.line('  * conta ativa | barras = uso informado pelo CCX', '90')
    for key,slot in store['slots'].items():
        out.line()
        out.line(f"  {'*' if key==active else ' '} {key}  {slot.get('email','Conta')}" ,'1')
        usage = usage_map.get(key)
        for index,label in enumerate(('5h','7d','modelo') if provider=='claude' else ('5h','7d')):
            win = (usage or {}).get(label) or {}
            name = labels[index] if labels and index<len(labels) else label
            pct = win.get('pct')
            color = '31' if isinstance(pct,(int,float)) and pct>=90 else '33' if isinstance(pct,(int,float)) and pct>=70 else '32'
            out.line(f'    {name:6} {out.bar(pct)}',color)
            out.line(f"           reset {ccx.fmt_reset(usage,label).strip()}", '90')
        if err_map.get(key):
            out.line('    Leitura: '+clean(err_map[key]),'33')
        if provider=='claude' and (auth:=ccx.slot_auth_state(slot))!='unverified':
            out.line('    Autenticacao: '+auth,'33')
    out.rule()


def fleet(snapshot, no_color=False):
    out = Terminal(no_color)
    service = snapshot['service']
    alive = service['alive']
    healthy = alive and time.time()-service['heartbeat']<60 and not service['stop']
    out.rule()
    out.line('  CCX  /  FROTA DE AGENTES','1;36')
    label = 'EM EXECUCAO' if healthy else 'PARANDO' if alive and service['stop'] else 'SEM RESPOSTA' if alive else 'PARADO'
    out.line(f"  Servico {label} | agentes sem teto fixo", '32' if healthy else '33')
    out.line('  Fechar o painel preserva o servico e as tarefas.', '90')
    if not snapshot['cells']:
        out.line()
        out.line('  Nenhuma celula cadastrada.')
        out.line('  Use: cell add NOME claude --plan max20')
    for cell in snapshot['cells']:
        out.rule()
        state = 'PAUSADA' if cell['paused'] else cell['auth'].upper()
        out.line(f"  {cell['id']}  /  {cell['provider']}  /  {cell['plan']} x{cell['weight']:g}", '1')
        out.line(f"  {state} | agentes ativos {cell['active']} | perfis {len(cell['workers'])}")
        age = max(0,snapshot['at']-cell['observed'])
        out.line(f"  Leitura: {duration(age)} atras"+('  [VENCIDA]' if age>600 else '') if cell['observed'] else '  Leitura: desconhecida', '90')
        for win in cell['windows']:
            reserved = cell['reserved'].get(win['key'],0)
            name = win.get('model') or duration(win['seconds'])
            out.line(f"  {name}  {out.bar(win['used'])}", '31' if win['used']>=90 else '36')
            out.line(f"    Reservado {reserved:.1f}pp | margem {cell['reserve']:g}pp")
            weight = cell['weight'] if win['seconds']<=86400 and not win.get('model') else cell['weekly_weight']
            free = max(0,100-win['used']-reserved-cell['reserve'])*weight/100
            out.line(f"    Folga estimada {free:.2f}x | reset {reset_label(win['reset'],snapshot['at'])}", '90')
        if cell['error']:
            out.line('  Leitura: '+cell['error'],'33')
    out.rule()
    queued = sum(job['state']=='queued' for job in snapshot['jobs'])
    out.line(f'  TAREFAS RECENTES  /  {queued} na fila', '1')
    for job in snapshot['jobs'][:12]:
        out.line(f"  {job['id'][:8]}  {job['state']:15} {job['title']}")
        if job['reason']:
            out.line('            '+job['reason'],'90')
    out.rule()
    out.line('  Folga em equivalentes x1; estimativa, sem garantia de tokens.', '90')
