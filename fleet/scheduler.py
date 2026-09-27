"""Admissão, escolha e reserva numa única transação."""
import json
import time
import uuid

from .store import ACTIVE, Store, event, overlaps

FRESH_SECONDS = 600


def applicable(window, model):
    scope = window.get('model')
    if not scope or scope == '*':
        return True
    return scope.lower() in model.lower()


def capacity(cell, windows, reserved, job, now):
    if cell['paused']:
        return None, 'paused'
    if cell['auth'] != 'ready':
        return None, cell['auth']
    if not windows or not 0 <= now - cell['observed'] <= FRESH_SECONDS:
        return None, 'quota_stale'
    options = json.loads(job['options'])
    amounts, pressures = {}, []
    primary = None
    for window in windows:
        if not applicable(window, options['model']):
            continue
        if window['reset'] is not None and window['reset'] <= now:
            return None, 'awaiting_reset_measurement'
        factor = cell['weight'] if window['seconds'] <= 86400 and not window.get('model') else cell['weekly_weight']
        cost = job['cost'] / factor
        age = max(0, now - cell['observed'])
        projected = window['used'] + window.get('burn', 0) * (age + 60)
        free = 100 - projected - cell['reserve']
        after = reserved.get(window['key'], 0) + cost
        if after > free:
            return None, 'draining'
        amounts[window['key']] = cost
        pressures.append(after / max(free, .001))
        if window['seconds'] <= 86400 and not window.get('model'):
            primary = pressures[-1]
    if not amounts:
        return None, 'quota_unknown'
    return (amounts, primary if primary is not None else max(pressures)), ''


def dispatch(store: Store, now=None):
    now = time.time() if now is None else now
    with store.transaction() as db:
        service = db.execute('SELECT * FROM service').fetchone()
        if service['stop']:
            return None
        active = [dict(row) for row in db.execute('SELECT jobs.* FROM jobs JOIN workers ON workers.job_id=jobs.id')]
        # Terminal runners retain their worker until process death is reconciled.
        cells = [dict(row) for row in db.execute('SELECT * FROM cells ORDER BY id')]
        for job in db.execute("SELECT * FROM jobs WHERE state='queued' AND cancel=0 ORDER BY priority DESC,created,id").fetchall():
            options = json.loads(job['options'])
            if any(overlaps(job['cwd'], other['cwd']) and
                   ('write' in (options['permission'],json.loads(other['options'])['permission'])) for other in active):
                db.execute("UPDATE jobs SET reason='workspace_busy' WHERE id=?", (job['id'],))
                continue
            candidates, reasons = [], []
            for cell in cells:
                if cell['provider'] != job['provider'] or options['cell'] not in (None,cell['id']):
                    continue
                workers = db.execute('SELECT * FROM workers WHERE cell_id=? ORDER BY id', (cell['id'],)).fetchall()
                free = [worker for worker in workers if not worker['job_id'] and not worker['login'] and worker['auth']=='ready' and worker['retry_after']<=now]
                if not free:
                    reasons.append('workers_busy' if any(worker['auth']=='ready' for worker in workers) else 'waiting_auth')
                    continue
                reserved = {}
                for row in db.execute('SELECT amounts FROM reservations WHERE cell_id=?', (cell['id'],)):
                    for key,value in json.loads(row['amounts']).items():
                        reserved[key] = reserved.get(key,0) + value
                windows = json.loads(cell['windows'])
                result, reason = capacity(cell, windows, reserved, job, now)
                if result is None:
                    reasons.append(reason)
                    continue
                amounts, pressure = result
                reset = min((window['reset'] for window in windows if window['reset'] is not None),default=float('inf'))
                candidates.append((pressure, reset, cell['last_dispatch'],cell['id'],free[0]['id'],amounts))
            if not candidates:
                db.execute('UPDATE jobs SET reason=? WHERE id=?', (reasons[0] if reasons else 'no_cells', job['id']))
                continue
            _,_,_,cell_id,worker_id,amounts = min(candidates, key=lambda item:item[:5])
            ticket = uuid.uuid4().hex
            db.execute("UPDATE jobs SET state='starting',reason='',worker_id=?,cell_id=?,ticket=?,started=? WHERE id=?",
                       (worker_id,cell_id,ticket,now,job['id']))
            db.execute('UPDATE workers SET job_id=? WHERE id=?', (job['id'],worker_id))
            db.execute('INSERT INTO reservations(job_id,cell_id,amounts) VALUES(?,?,?)', (job['id'],cell_id,json.dumps(amounts)))
            db.execute('UPDATE cells SET last_dispatch=? WHERE id=?', (now,cell_id))
            event(db, 'dispatched', job['id'])
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        return None
