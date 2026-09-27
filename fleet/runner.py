"""Um runner por ticket, dono exclusivo de seu perfil e da árvore de CLI."""
import json
import subprocess
import threading
import time

import ccx
from . import processes, providers
from .store import event


class Output:
    def __init__(self, provider):
        self.provider = provider
        self.terminal = False
        self.failed = False
        self.text = ''
        self.session = None
        self.auth_error = None

    def accept(self, item):
        kind = item.get('type')
        error=item.get('error')
        code=error.get('code',error.get('type')) if isinstance(error,dict) else error
        status=item.get('status_code')
        if isinstance(error,dict): status=error.get('status_code',status)
        if code in ('authentication_error','invalid_grant','unauthorized','token_expired','invalid_api_key') or kind=='authentication_error' or status in (401,403):
            self.auth_error='http_403' if status==403 else 'http_401'
            self.failed=True
        if kind=='error': self.failed=True
        if self.provider == 'claude':
            if kind == 'system' and item.get('subtype') == 'init':
                self.session = item.get('session_id')
            if kind == 'result':
                if self.terminal:
                    self.failed = True
                self.terminal = True
                self.failed |= item.get('is_error') is not False or item.get('subtype') != 'success'
                self.text = item.get('result', '')
                if isinstance(item.get('structured_output'),dict):
                    self.text = json.dumps(item['structured_output'],ensure_ascii=False)
                self.session = item.get('session_id', self.session)
        else:
            if kind == 'thread.started':
                self.session = item.get('thread_id')
            if kind in ('error', 'turn.failed'):
                self.failed = True
            if kind == 'turn.completed':
                if self.terminal:
                    self.failed = True
                self.terminal = True
        if not isinstance(self.text, str) or len(self.text) > 8_000_000:
            self.failed, self.text = True, ''
        if self.session is not None and (not isinstance(self.session, str) or len(self.session) > 120 or
                                        any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in self.session)):
            self.session = None


def consume(stream, output):
    try:
        while True:
            line = stream.readline(8_000_001)
            if not line:
                return
            if len(line) > 8_000_000:
                output.failed = True
                # Drain excess without parsing or retaining arbitrary output.
                while line and not line.endswith(b'\n'):
                    line = stream.readline(8_000_001)
                continue
            if line.strip():
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError()
                    output.accept(item)
                except (ValueError, TypeError, AttributeError):
                    output.failed = True
    except Exception:
        output.failed = True


def finish(store, job_id, state, reason='', code=None, session=None, *, auth_error=None, attempted=True):
    with store.transaction() as db:
        db.execute('UPDATE jobs SET state=?,reason=?,finished=?,exit_code=?,session=? WHERE id=?',
                   (state,reason,time.time(),code,session,job_id))
        if attempted:
            db.execute('UPDATE reservations SET release_after=? WHERE job_id=?', (time.time()+60,job_id))
        else:
            db.execute('DELETE FROM reservations WHERE job_id=?',(job_id,))
        if auth_error:
            worker=db.execute('SELECT worker_id FROM jobs WHERE id=?',(job_id,)).fetchone()[0]
            providers.auth_failure(db,worker,auth_error)
        event(db, state, job_id)


def run(store, job_id, ticket):
    job = store.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    try:
        with processes.waiting_lock(store.root/'locks'/f"worker-{job['worker_id']}.lock"):
            return run_locked(store,job,ticket)
    except BlockingIOError:
        with store.transaction() as db:
            changed=db.execute("UPDATE jobs SET state='failed',reason='profile_busy_before_start',finished=? WHERE id=? AND ticket=? AND state='starting' AND pid IS NULL",
                               (time.time(),job_id,ticket)).rowcount
            if changed:
                db.execute('DELETE FROM reservations WHERE job_id=?',(job_id,))
                event(db,'profile_busy_before_start',job_id)
        return 2


def run_locked(store, job, ticket):
    job_id=job['id']
    own = processes.identity()
    # Lock acquired before claiming ticket; duplicate runners never touch a running CLI.
    with store.transaction() as db:
        current=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if current['state']=='starting' and current['ticket']==ticket and not current['pid'] and current['cancel']:
            db.execute("UPDATE jobs SET state='cancelled',finished=?,reason='operator_cancelled' WHERE id=?",(time.time(),job_id))
            db.execute('DELETE FROM reservations WHERE job_id=?',(job_id,))
            return 130
        claimed = db.execute("UPDATE jobs SET state='running',pid=?,marker=? WHERE id=? AND ticket=? AND state='starting' AND pid IS NULL AND cancel=0",
                             (own['pid'],own['process_start'],job_id,ticket)).rowcount
        if not claimed:
            return 3
        db.execute('UPDATE workers SET pid=?,marker=? WHERE id=?', (own['pid'],own['process_start'],job['worker_id']))
        event(db,'runner_started',job_id)
    tree = None
    spawned = False
    try:
        tree = processes.ProcessTree()
        options = json.loads(job['options'])
        home = store.home(job['worker_id'])
        providers.prepare_idle(store,job['worker_id'])
        folder = store.root / 'jobs' / job_id
        prompt = ccx.read_json(folder / 'input.json')['prompt']
        result_path = folder / 'last-message.txt'
        argv = providers.command(job['provider'],options,job['cwd'],result_path)
        child = subprocess.Popen(argv, cwd=job['cwd'], env=providers.environment(job['provider'],home,options['guards']),
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW if __import__('os').name=='nt' else 0)
        spawned = True
        output = Output(job['provider'])
        reader = threading.Thread(target=consume,args=(child.stdout,output),daemon=True)
        reader.start()
        def send():
            try:
                child.stdin.write(prompt.encode('utf-8'))
                child.stdin.close()
            except (OSError, BrokenPipeError):
                output.failed = True
        writer = threading.Thread(target=send,daemon=True)
        writer.start()
        while child.poll() is None:
            current = store.one('SELECT cancel FROM jobs WHERE id=?', (job_id,))
            if current['cancel']:
                with store.transaction() as db:
                    db.execute("UPDATE jobs SET state='cancelling',reason='operator_cancelled' WHERE id=?",(job_id,))
                tree.terminate()  # Includes this runner; reconciliation confirms actual death.
                return 130
            time.sleep(.25)
        writer.join(2)
        reader.join(5)
        if reader.is_alive() or writer.is_alive():
            output.failed = True
        if job['provider'] == 'codex' and result_path.is_file() and result_path.stat().st_size <= 8_000_000:
            output.text = result_path.read_text(encoding='utf-8')
        success = child.returncode == 0 and output.terminal and not output.failed and bool(output.text.strip())
        ccx.write_json(folder / 'result.json', {'text':output.text, 'session':output.session})
        cancelled = store.one('SELECT cancel FROM jobs WHERE id=?',(job_id,))['cancel']
        reason = '' if success else 'cli_exit_nonzero' if child.returncode else 'cli_terminal_missing' if not output.terminal else 'cli_reported_error' if output.failed else 'cli_empty_result'
        finish(store,job_id,'cancelled' if cancelled else ('completed' if success else 'needs_attention'),
               'operator_cancelled' if cancelled else reason,child.returncode,output.session,auth_error=output.auth_error)
        return 0 if success and not cancelled else 1
    except providers.AuthError as exc:
        finish(store,job_id,'failed',str(exc),auth_error=str(exc),attempted=spawned)
        return 1
    except Exception:
        finish(store,job_id,'needs_attention' if spawned else 'failed',
               'runner_interrupted' if spawned else 'preflight_failed',attempted=spawned)
        return 1
    finally:
        # Process exit closes the Job handle and kills any leftover descendants.
        # Keep the worker reserved until a reconciler verifies process death.
        pass
