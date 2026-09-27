"""Exemplo de integração: prazo do orquestrador cancela e confirma encerramento."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fleet import service
from fleet.cli import wait
from fleet.store import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider',required=True,choices=('claude','codex'))
    parser.add_argument('--work-id',required=True)
    parser.add_argument('--cwd',required=True)
    parser.add_argument('--prompt-file',required=True,type=Path)
    parser.add_argument('--model',required=True)
    parser.add_argument('--effort',default='high')
    parser.add_argument('--permission',choices=('read-only','write'),default='read-only')
    parser.add_argument('--timeout',type=float,default=3600)
    args=parser.parse_args()
    store=Store()
    service.start(store)
    job=store.submit(args.provider,args.prompt_file.read_text(encoding='utf-8'),args.cwd,args.model,
                     effort=args.effort,permission=args.permission,request_id=args.work_id,
                     guards={'ENTERPRISE_EXECUTOR_DEPTH':'1','CROSS_REVIEW_DEPTH':'1','ENTERPRISE_WORK_ID':args.work_id})
    print(job,file=sys.stderr)
    code=wait(store,job,max(1,args.timeout))
    if code==124:
        store.cancel(job)
        cancelled=wait(store,job,110)
        if cancelled==124:
            print('Cancelamento solicitado, morte do runner ainda não confirmada: '+job,file=sys.stderr)
            return 2
    return code


if __name__=='__main__':
    raise SystemExit(main())
