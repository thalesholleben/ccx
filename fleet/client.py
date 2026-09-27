"""Bounded transport for existing protocol runners. No CLI or global login fallback."""
import subprocess
import os
import sqlite3
import time

import ccx
from . import service
from .store import Store, TERMINAL, GUARDS


def busy(error):
    return getattr(error,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED)


def state(store,job_id):
    if not service.status(store)['alive']:
        service.reconcile(store)
    return store.one('SELECT jobs.*,workers.id AS owned_worker FROM jobs LEFT JOIN workers ON workers.job_id=jobs.id WHERE jobs.id=?',(job_id,))


def cancel_and_confirm(store,job_id,deadline):
    while True:
        try:
            store.cancel(job_id)
            job=state(store,job_id)
            if job['state'] in TERMINAL and not job['owned_worker']:return
        except sqlite3.OperationalError as error:
            if not busy(error):raise
        if time.monotonic()>=deadline:raise RuntimeError('ccx_cancellation_unconfirmed:'+job_id)
        time.sleep(.2)


def execute(provider, prompt, cwd, model, *, timeout=3600, root=None, on_submit=None, **options):
    options['guards']={**{key:os.environ[key] for key in GUARDS if key in os.environ},**(options.get('guards') or {})}
    try:
        return _execute(provider,prompt,cwd,model,timeout=timeout,root=root,on_submit=on_submit,**options)
    except sqlite3.Error as error:
        raise RuntimeError('ccx_database_error:'+type(error).__name__) from error


def _execute(provider, prompt, cwd, model, *, timeout, root, on_submit, **options):
    store=Store(root)
    service.start(store)
    job_id=store.submit(provider,prompt,cwd,model,**options)
    deadline=time.monotonic()+max(.1,timeout)
    try:
        if on_submit: on_submit(job_id)
        while True:
            if time.monotonic()>=deadline:
                raise subprocess.TimeoutExpired(['ccx',job_id],timeout)
            try:
                job=state(store,job_id)
            except sqlite3.OperationalError as error:
                if not busy(error):raise
                time.sleep(.2)
                continue
            if job['state'] in TERMINAL and not job['owned_worker']:
                result=store.root/'jobs'/job_id/'result.json'
                text=ccx.read_json(result).get('text','') if result.is_file() else ''
                return subprocess.CompletedProcess(['ccx',job_id],0 if job['state']=='completed' else 1,
                                                   text,job['reason'])
            time.sleep(.2)
    except BaseException:
        cancel_and_confirm(store,job_id,time.monotonic()+110)
        raise
