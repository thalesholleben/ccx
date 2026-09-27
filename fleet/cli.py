"""Entrada pública da frota. Nenhum segredo é impresso em erros."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import ccx
from .store import Store, TERMINAL, GUARDS, plans
from . import providers, runner, service, terminal


def wait(store, job_id, timeout=0):
    deadline = time.monotonic()+timeout if timeout else float('inf')
    while True:
        service.reconcile(store)
        job = store.one('SELECT * FROM jobs WHERE id=?',(job_id,))
        if job['state'] in TERMINAL and not store.rows('SELECT id FROM workers WHERE job_id=?',(job_id,)):
            result = store.root / 'jobs' / job_id / 'result.json'
            if result.is_file():
                text = ccx.read_json(result).get('text','')
                print(text)
            if job['state'] != 'completed':
                print('Tarefa '+job['state']+': '+job['reason'],file=sys.stderr)
            return 0 if job['state']=='completed' else 1
        if time.monotonic() >= deadline:
            print('Tempo de espera encerrado; tarefa preservada: '+job_id,file=sys.stderr)
            return 124
        if job['state']=='queued' and not service.status(store)['alive']:
            print('Serviço parado; tarefa permanece na fila. Inicie com fleet service start: '+job_id,file=sys.stderr)
            return 3
        time.sleep(.3)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ccx fleet',description='Frota local de agentes por contas isoladas.')
    parser.add_argument('--root',type=Path,help='estado isolado (padrao ~/.ccx/fleet)')
    sub = parser.add_subparsers(dest='action',required=True)
    status = sub.add_parser('status')
    status.add_argument('--json',action='store_true')
    status.add_argument('--no-color',action='store_true')
    daemon = sub.add_parser('service')
    daemon.add_argument('operation',choices=('start','status','stop'))
    sub.add_parser('panel')
    cells = sub.add_parser('cell').add_subparsers(dest='operation',required=True)
    add = cells.add_parser('add')
    add.add_argument('name')
    add.add_argument('provider',choices=('claude','codex'))
    add.add_argument('--plan',choices=('plus','pro','max5','max20','custom'),help='padrao: Claude pro, Codex plus')
    add.add_argument('--weight',type=float)
    add.add_argument('--weekly-weight',type=float,default=1)
    add.add_argument('--reserve',type=float,default=10)
    cells.add_parser('list')
    config = cells.add_parser('configure')
    config.add_argument('name')
    config.add_argument('--name',dest='display_name',help='nome visível; preserva o ID e o login')
    config.add_argument('--plan',choices=('plus','pro','max5','max20','custom'))
    config.add_argument('--weight',type=float)
    config.add_argument('--weekly-weight',type=float)
    config.add_argument('--reserve',type=float)
    remove=cells.add_parser('remove',help='remove conta e perfis locais; preserva histórico de tarefas')
    remove.add_argument('name')
    remove.add_argument('--yes',action='store_true',required=True,help='confirma remoção dos perfis locais')
    for operation in ('pause','resume','login'):
        command = cells.add_parser(operation)
        command.add_argument('name')
        if operation=='login':
            command.add_argument('--worker')
    worker = sub.add_parser('worker').add_subparsers(dest='operation',required=True).add_parser('add')
    worker.add_argument('cell')
    for action in ('submit','run'):
        command = sub.add_parser(action)
        command.add_argument('provider',choices=('claude','codex'))
        command.add_argument('--prompt-file',type=Path,help='sem arquivo: le prompt do stdin')
        command.add_argument('--cwd',default=os.getcwd())
        command.add_argument('--model')
        command.add_argument('--effort',default='high')
        command.add_argument('--permission',default='read-only',choices=('read-only','write'))
        command.add_argument('--cost',type=float,default=15,help='pontos estimados em conta x1')
        command.add_argument('--priority',type=int,default=0)
        command.add_argument('--title',default='')
        command.add_argument('--request-id')
        command.add_argument('--cell')
        command.add_argument('--allowed-tools',help='Claude: lista separada por virgula')
        command.add_argument('--ephemeral',action='store_true')
        if action=='run':
            command.add_argument('--timeout',type=float,default=0)
    command = sub.add_parser('wait')
    command.add_argument('job')
    command.add_argument('--timeout',type=float,default=0)
    sub.add_parser('cancel').add_argument('job')
    sub.add_parser('_serve',help=argparse.SUPPRESS)
    internal = sub.add_parser('_runner',help=argparse.SUPPRESS)
    internal.add_argument('job')
    internal.add_argument('ticket')
    args = parser.parse_args(argv)
    try:
        store = Store(args.root, migrate=not (args.action=='service' and args.operation=='stop'))
        if args.action=='_serve':
            return service.serve(store)
        if args.action=='_runner':
            return runner.run(store,args.job,args.ticket)
        if args.action=='panel':
            from .panel import main as panel
            return panel(store)
        if args.action=='service':
            if args.operation=='start':
                service.start(store)
            elif args.operation=='stop':
                service.stop(store)
            print(json.dumps(service.status(store)))
        elif args.action=='status' or (args.action=='cell' and args.operation=='list'):
            snapshot = store.snapshot()
            if getattr(args,'json',False):
                print(json.dumps(snapshot,ensure_ascii=False,allow_nan=False))
            else:
                terminal.fleet(snapshot,getattr(args,'no_color',False))
        elif args.action=='cell':
            if args.operation=='add':
                worker=store.add_cell(None,args.provider,args.plan or plans(args.provider)[0],args.weight,args.weekly_weight,args.reserve,label=args.name)
                cell_id=store.worker(worker)['cell_id']
                print('Conta criada: '+cell_id)
                print('Worker criado: '+worker)
                print('Proximo passo: cell login '+cell_id)
            elif args.operation in ('pause','resume'):
                store.pause(args.name,args.operation=='pause')
            elif args.operation=='configure':
                store.configure(args.name,name=args.display_name,plan=args.plan,weight=args.weight,weekly_weight=args.weekly_weight,reserve=args.reserve)
            elif args.operation=='remove':
                store.remove_cell(args.name)
                print('Conta removida. Histórico de tarefas preservado.')
            elif args.operation=='login':
                workers = store.rows('SELECT id FROM workers WHERE cell_id=? ORDER BY id',(args.name,))
                selected = args.worker or (workers[0]['id'] if workers else '')
                if selected not in [w['id'] for w in workers]:
                    raise ValueError('worker_not_in_cell')
                providers.login(store,selected)
                print('Perfil autenticado e vinculado.')
        elif args.action=='worker':
            print(store.add_worker(args.cell))
        elif args.action in ('submit','run'):
            prompt = args.prompt_file.read_text(encoding='utf-8') if args.prompt_file else sys.stdin.read(200_001)
            if args.action=='run':
                service.start(store) # No new queued work if bootstrap fails.
            job = store.submit(args.provider,prompt,args.cwd,args.model or ('claude-opus-5-5' if args.provider=='claude' else 'gpt-6-sol'),
                effort=args.effort,permission=args.permission,cost=args.cost,priority=args.priority,title=args.title,
                request_id=args.request_id,preferred_cell=args.cell,guards={k:os.environ[k] for k in GUARDS if k in os.environ},
                allowed_tools=args.allowed_tools.split(',') if args.allowed_tools is not None else None,persist=not args.ephemeral)
            print(job,file=sys.stderr if args.action=='run' else sys.stdout)
            if args.action=='run':
                return wait(store,job,max(0,args.timeout))
        elif args.action=='wait':
            return wait(store,args.job,max(0,args.timeout))
        elif args.action=='cancel':
            store.cancel(args.job)
        return 0
    except KeyboardInterrupt:
        print('Cliente encerrado; tarefas permanecem na frota.',file=sys.stderr)
        return 130
    except Exception as exc:
        allowed = ('bootstrap_failed','service_start_unconfirmed','native_cli_missing','service_stopping','service_unresponsive')
        code = str(exc).split(':')[0]
        if code == 'upgrade_requires_service_stop':
            print('CCX: pare o serviço antes de atualizar (service stop), aguarde sua saída e inicie novamente. Os runners continuam vivos.',file=sys.stderr)
        elif code in terminal.NAME_ERRORS:
            print('CCX: '+terminal.NAME_ERRORS[code],file=sys.stderr)
        elif code in allowed:
            print('CCX: '+code+'. Abra ccx-panel.cmd pelo Explorer para iniciar fora do agente.',file=sys.stderr)
        else:
            print('CCX: operacao recusada ('+type(exc).__name__+'). Confira argumentos, perfil e estado da frota.',file=sys.stderr)
        return 2
