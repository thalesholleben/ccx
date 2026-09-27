"""Supervisor sem UI: reconciliar, medir e despachar. Nunca repete prompts."""
import json
import time

from . import processes, providers, scheduler
from .runner import finish
from .store import ACTIVE, TERMINAL, event


def reconcile(store):
    now = time.time()
    for worker in store.rows('SELECT * FROM workers WHERE login=1'):
        if not processes.live(worker['pid'],worker['marker']):
            # The lock is authoritative if an independent login child still owns it.
            try:
                with store.worker_lock(worker['id']):
                    with store.transaction() as db:
                        db.execute('UPDATE workers SET login=0,pid=NULL,marker=NULL WHERE id=? AND pid=?', (worker['id'],worker['pid']))
            except BlockingIOError:
                pass
    for job in store.rows('SELECT jobs.* FROM jobs JOIN workers ON workers.job_id=jobs.id'):
        if processes.live(job['pid'],job['marker']):
            continue
        if job['state']=='starting' and now-job['started'] < 90:
            continue
        try:
            with store.worker_lock(job['worker_id']):
                with store.transaction() as db:
                    current = db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
                    if processes.live(current['pid'],current['marker']):
                        continue
                    if current['state'] not in TERMINAL:
                        state = 'cancelled' if current['cancel'] else 'needs_attention'
                        db.execute('UPDATE jobs SET state=?,reason=?,finished=? WHERE id=?',
                                   (state,'operator_cancelled' if current['cancel'] else 'runner_lost',now,job['id']))
                        db.execute('UPDATE reservations SET release_after=? WHERE job_id=?', (now+60,job['id']))
                        event(db,state,job['id'])
                    db.execute('UPDATE workers SET job_id=NULL,pid=NULL,marker=NULL WHERE job_id=?',(job['id'],))
            processes.remove_task(store.root,'job-'+job['id'])
        except BlockingIOError:
            continue


def status(store):
    data = store.one('SELECT * FROM service')
    data['alive'] = processes.live(data['pid'],data['marker'])
    data['healthy'] = data['alive'] and time.time()-data['heartbeat'] < 60 and not data['stop']
    return data


def start(store):
    with processes.file_lock(store.root / 'locks' / 'bootstrap.lock'):
        current = status(store)
        if current['alive']:
            if current['stop']:
                raise RuntimeError('service_stopping')
            if not current['healthy']:
                raise RuntimeError('service_unresponsive')
            return current
        with store.transaction() as db:
            db.execute('UPDATE service SET stop=0')
        processes.launch(store.root,'service',['_serve'])
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            current = status(store)
            if current['healthy']:
                return current
            time.sleep(.2)
        raise RuntimeError('service_start_unconfirmed: abra ccx-panel.cmd pelo Explorer.')


def stop(store):
    with store.transaction() as db:
        db.execute('UPDATE service SET stop=1')
        event(db,'service_stop_requested')


def poll_targets(store, now=None):
    now=time.time() if now is None else now
    jobs=store.rows("SELECT provider,cell_id,state,options FROM jobs WHERE state IN ('queued','starting','running','cancelling')")
    targets=[]
    for cell in store.rows('SELECT * FROM cells WHERE identity IS NOT NULL AND next_poll<=? ORDER BY next_poll,id',(now,)):
        active=any(job['state']!='queued' and job['cell_id']==cell['id'] for job in jobs)
        queued=any(job['state']=='queued' and job['provider']==cell['provider'] and
                   json.loads(job['options'])['cell'] in (None,cell['id']) for job in jobs)
        if active or (queued and not cell['paused']):
            targets.append(cell['id'])
    return targets


def cycle(store):
    reconcile(store)
    targets=poll_targets(store)
    if targets:
        providers.poll_cell(store,targets[0])
    job=scheduler.dispatch(store)
    if job:
        try:
            processes.launch(store.root,'job-'+job['id'],['_runner',job['id'],job['ticket']])
        except Exception:
            # Bootstrap may have created a delayed process: revoke ticket atomically.
            with store.transaction() as db:
                changed=db.execute("UPDATE jobs SET state='failed',reason='runner_bootstrap_failed',finished=? WHERE id=? AND state='starting' AND pid IS NULL",
                                   (time.time(),job['id'])).rowcount
                if changed:
                    db.execute('DELETE FROM reservations WHERE job_id=?',(job['id'],))
                    event(db,'runner_bootstrap_failed',job['id'])


def serve(store):
    with processes.file_lock(store.root / 'locks' / 'service.lock'):
        own = processes.identity()
        with store.transaction() as db:
            db.execute('UPDATE service SET pid=?,marker=?,heartbeat=?,job_flags=?',
                       (own['pid'],own['process_start'],time.time(),processes.job_flags()))
            event(db,'service_started')
        try:
            while True:
                try:
                    with store.transaction() as db:
                        db.execute('UPDATE service SET heartbeat=?',(time.time(),))
                        if db.execute('SELECT stop FROM service').fetchone()[0]:
                            break
                    cycle(store)
                except Exception as exc:
                    # A locked/corrupt job must not silently kill the supervisor.
                    try:
                        with store.transaction() as db:
                            event(db,'cycle_error_'+type(exc).__name__)
                    except Exception:
                        pass  # Locked DB: heartbeat goes stale; next iteration retries.
                    time.sleep(5)
                time.sleep(.5)
        finally:
            with store.transaction() as db:
                db.execute('UPDATE service SET pid=NULL,marker=NULL,heartbeat=0 WHERE pid=?',(own['pid'],))
                event(db,'service_stopped')
    return 0
