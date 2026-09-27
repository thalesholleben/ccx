"""Bounded transport for existing protocol runners. No CLI or global login fallback."""
import subprocess
import time

import ccx
from . import service
from .store import Store, TERMINAL


def execute(provider, prompt, cwd, model, *, timeout=3600, root=None, on_submit=None, **options):
    store=Store(root)
    service.start(store)
    job_id=store.submit(provider,prompt,cwd,model,**options)
    deadline=time.monotonic()+max(.1,timeout)
    expired=False
    try:
        if on_submit: on_submit(job_id)
        while True:
            service.reconcile(store)
            job=store.one('SELECT * FROM jobs WHERE id=?',(job_id,))
            owned=store.rows('SELECT id FROM workers WHERE job_id=?',(job_id,))
            if job['state'] in TERMINAL and not owned:
                if expired:
                    raise subprocess.TimeoutExpired(['ccx',job_id],timeout)
                result=store.root/'jobs'/job_id/'result.json'
                text=ccx.read_json(result).get('text','') if result.is_file() else ''
                return subprocess.CompletedProcess(['ccx',job_id],0 if job['state']=='completed' else 1,
                                                   text,job['reason'])
            if time.monotonic()>=deadline:
                if expired:
                    raise RuntimeError('ccx_cancellation_unconfirmed:'+job_id)
                store.cancel(job_id)
                expired=True
                deadline=time.monotonic()+110
            time.sleep(.2)
    except BaseException:
        store.cancel(job_id)
        if not expired:deadline=time.monotonic()+110
        while True:
            service.reconcile(store)
            job=store.one('SELECT state FROM jobs WHERE id=?',(job_id,))
            if job['state'] in TERMINAL and not store.rows('SELECT id FROM workers WHERE job_id=?',(job_id,)):
                break
            if time.monotonic()>=deadline:
                raise RuntimeError('ccx_cancellation_unconfirmed:'+job_id)
            time.sleep(.2)
        raise
