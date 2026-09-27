"""CLIs sintéticos para testes de processos reais, sem rede ou credenciais."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

if sys.argv[1]=='child':
    Path(sys.argv[2]).write_text(str(os.getpid()))
    time.sleep(180)
    raise SystemExit()
if sys.argv[1]=='cli':
    prompt=json.loads(sys.stdin.read())
    home=os.environ.get('CLAUDE_CONFIG_DIR') or os.environ.get('CODEX_HOME')
    info={'pid':os.getpid(),'home':home,'auth_override':bool(os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('OPENAI_API_KEY')),
          'guard':os.environ.get('CROSS_REVIEW_DEPTH'),'prompt':prompt.get('text')}
    Path(prompt['record']).write_text(json.dumps(info))
    if prompt.get('child'):
        subprocess.Popen([sys.executable,__file__,'child',prompt['child']])
    time.sleep(prompt.get('sleep',0))
    if prompt.get('missing'):
        print(json.dumps({'type':'system','subtype':'init','session_id':'synthetic-session'}),flush=True)
    else:
        if len(sys.argv)>2 and sys.argv[2]=='codex':
            Path(sys.argv[3]).write_text('Resposta sintética.',encoding='utf-8')
            print(json.dumps({'type':'thread.started','thread_id':'synthetic-session'}),flush=True)
            if prompt.get('error'):
                print(json.dumps({'type':'turn.failed','error':{'message':'synthetic'}}),flush=True)
            else:
                print(json.dumps({'type':'turn.completed'}),flush=True)
        else:
            if prompt.get('auth_error'):
                print(json.dumps({'type':'error','error':{'type':'authentication_error'}}),flush=True)
            print(json.dumps({'type':'result','subtype':'success','is_error':prompt.get('error',False),
                              'result':'Resposta sintética.','session_id':'synthetic-session'}),flush=True)
    raise SystemExit(prompt.get('exit',0))

from fleet import processes, providers, runner, service
from fleet.store import Store

args=sys.argv[1:]
root=args[args.index('--root')+1]
action=args[args.index('--root')+2:]
store=Store(root)
original_prepare=providers.prepare_idle
def prepare(store,worker):
    faults_path=store.root/'faults.json'
    faults=json.loads(faults_path.read_text()) if faults_path.exists() else {}
    if faults.get(worker)=='dead':
        def refresh(_):
            with (store.root/'refresh-count.txt').open('a') as log: log.write(worker+'\n')
            return None,'dead'
        providers.ccx.refresh_token=refresh
        return original_prepare(store,worker)
    if faults.get(worker)=='transient':
        raise providers.AuthError('refresh_unavailable')
providers.prepare_idle=prepare
providers.poll_cell=lambda *args: None
providers.command=lambda provider,options,cwd,output: [sys.executable,__file__,'cli',provider,str(output)]
original_launch=processes.launch
processes.launch=lambda root,purpose,arguments: original_launch(root,purpose,arguments,entry=Path(__file__))
if action[0]=='_serve':
    raise SystemExit(service.serve(store))
if action[0]=='_serve_failure':
    from fleet import scheduler
    calls=[]
    def dispatch(_):
        calls.append(1)
        if len(calls)==1: raise RuntimeError('synthetic-private-message')
        service.stop(store)
    scheduler.dispatch=dispatch
    raise SystemExit(service.serve(store))
if action[0]=='_runner':
    raise SystemExit(runner.run(store,action[1],action[2]))
if action[0]=='_bootstrap':
    tree=processes.ProcessTree()
    try:
        result=service.start(store)
        result['bootstrap']='started'
    except RuntimeError as exc:
        result={'bootstrap':'refused','reason':str(exc).split(':')[0]}
    (store.root/'bootstrap.json').write_text(json.dumps(result))
    os._exit(0) # Close caller's restrictive Job Object; service must survive it.
if action[0]=='dispatch':
    from fleet.scheduler import dispatch
    row=dispatch(store)
    print(row['id'] if row else 'none')
