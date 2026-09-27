"""Estado durável da frota; segredos OAuth nunca entram no banco."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import ccx
from . import processes

TERMINAL = ('completed', 'failed', 'cancelled', 'needs_attention')
ACTIVE = ('starting', 'running', 'cancelling')
ID = re.compile(r'[a-z0-9][a-z0-9-]{0,47}\Z')
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}\Z')
GUARDS = ('ENTERPRISE_EXECUTOR_DEPTH', 'CROSS_REVIEW_DEPTH', 'ENTERPRISE_WORK_ID')
PLAN_WEIGHTS = {'pro': 1, 'max5': 5, 'max20': 20, 'custom': 1}


def plans(provider):
    return ('pro', 'custom') if provider == 'codex' else tuple(PLAN_WEIGHTS)


def number(value, low=0, high=100):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('invalid_number')
    return value


def canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def overlaps(first: str, second: str) -> bool:
    try:
        common = os.path.commonpath([first, second])
        return common in (first, second)
    except ValueError:
        return False


def event(db, kind: str, entity: str = ''):
    db.execute('INSERT INTO events(at,kind,entity) VALUES(?,?,?)', (time.time(), kind, entity))
    db.execute('DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-2000 FROM events)')


class Store:
    def __init__(self, root: Path | str | None = None, *, migrate=True):
        self.root = Path(root or Path.home() / '.ccx' / 'fleet').expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'fleet.sqlite3'
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise ValueError('unsupported_schema')
            if version in (1, 2):
                # The old scheduler interprets max_active=0 as zero slots. Let
                # service stop open the old schema, but never migrate under it.
                if not migrate:
                    return
                owner = db.execute('SELECT pid,marker FROM service').fetchone()
                if owner and processes.live(owner['pid'], owner['marker']):
                    raise RuntimeError('upgrade_requires_service_stop')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cells(
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, plan TEXT NOT NULL,
                    weight REAL NOT NULL, weekly_weight REAL NOT NULL, reserve REAL NOT NULL,
                    max_active INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
                    identity TEXT, auth TEXT NOT NULL DEFAULT 'waiting_auth',
                    windows TEXT NOT NULL DEFAULT '[]', observed REAL NOT NULL DEFAULT 0,
                    next_poll REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    last_dispatch REAL NOT NULL DEFAULT 0, UNIQUE(provider,identity));
                CREATE TABLE IF NOT EXISTS workers(
                    id TEXT PRIMARY KEY, cell_id TEXT NOT NULL REFERENCES cells(id),
                    auth TEXT NOT NULL DEFAULT 'waiting_auth', pid INTEGER, marker TEXT,
                    job_id TEXT, login INTEGER NOT NULL DEFAULT 0,
                    retry_after REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE, fingerprint TEXT NOT NULL,
                    provider TEXT NOT NULL, title TEXT NOT NULL, cwd TEXT NOT NULL,
                    options TEXT NOT NULL, cost REAL NOT NULL, priority INTEGER NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                    started REAL, finished REAL, worker_id TEXT REFERENCES workers(id),
                    cell_id TEXT REFERENCES cells(id), ticket TEXT, pid INTEGER, marker TEXT,
                    cancel INTEGER NOT NULL DEFAULT 0, session TEXT, exit_code INTEGER);
                CREATE TABLE IF NOT EXISTS reservations(
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), cell_id TEXT NOT NULL REFERENCES cells(id),
                    amounts TEXT NOT NULL, release_after REAL);
                CREATE TABLE IF NOT EXISTS service(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), pid INTEGER, marker TEXT,
                    heartbeat REAL NOT NULL DEFAULT 0, stop INTEGER NOT NULL DEFAULT 0,
                    max_active INTEGER NOT NULL DEFAULT 0, job_flags INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO service(singleton) VALUES(1);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL NOT NULL,
                    kind TEXT NOT NULL, entity TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state,priority,created);
            ''')
            db.execute('BEGIN IMMEDIATE')
            try:
                if 'retry_after' not in {row[1] for row in db.execute('PRAGMA table_info(workers)')}:
                    db.execute('ALTER TABLE workers ADD COLUMN retry_after REAL NOT NULL DEFAULT 0')
                if db.execute('PRAGMA user_version').fetchone()[0] < 3:
                    # Retired columns remain for old data; zero denotes no numeric cap.
                    db.execute('UPDATE cells SET max_active=0')
                    db.execute('UPDATE service SET max_active=0')
                    for cell in db.execute('SELECT id,provider,plan FROM cells').fetchall():
                        if cell['plan'] not in plans(cell['provider']):
                            db.execute("UPDATE cells SET plan='custom' WHERE id=?", (cell['id'],))
                db.execute('PRAGMA user_version=3')
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        if not rows:
            raise ValueError('not_found')
        return rows[0]

    def add_cell(self, name, provider, plan='custom', weight=None, weekly_weight=1, reserve=10):
        if not ID.fullmatch(name) or provider not in ('claude', 'codex') or plan not in plans(provider):
            raise ValueError('invalid_cell')
        weight = PLAN_WEIGHTS[plan] if weight is None else weight
        number(weight, .1, 100)
        number(weekly_weight, .1, 100)
        number(reserve, 0, 80)
        with self.transaction() as db:
            db.execute('INSERT INTO cells(id,provider,plan,weight,weekly_weight,reserve,max_active) VALUES(?,?,?,?,?,?,?)',
                       (name, provider, plan, weight, weekly_weight, reserve, 0))
            event(db, 'cell_added', name)
        return self.add_worker(name)

    def add_worker(self, cell_id):
        cell = self.one('SELECT * FROM cells WHERE id=?', (cell_id,))
        worker = uuid.uuid4().hex
        home = self.root / 'profiles' / cell['provider'] / worker
        home.mkdir(parents=True, mode=0o700)
        ccx.write_json(home / 'ccx-profile.json', {'worker': worker, 'provider': cell['provider']})
        # Instruction pointer only: no settings, hooks, credentials or history copied.
        instruction = home / ('CLAUDE.md' if cell['provider']=='claude' else 'AGENTS.md')
        skills = Path.home() / ('.claude' if cell['provider']=='claude' else '.agents') / 'skills'
        ccx.write_json(home / 'profile-policy.json', {'managed_by':'ccx-fleet','skills':str(skills),'exclusive_profile':True})
        temp = instruction.with_suffix('.tmp')
        temp.write_text('Este perfil pertence à frota CCX. Use somente pelo CCX.\n'
                        'Siga as instruções AGENTS.md/CLAUDE.md do diretório de trabalho.\n'
                        'Skills globais disponíveis em '+str(skills)+'. Leia a SKILL.md aplicável antes de usá-la.\n'
                        'Antes de invocar outro agente, executor ou revisor, leia '+str(skills / 'ccx' / 'SKILL.md')+'.\n'
                        'Essa leitura não autoriza delegação nem remove guardas de recursão.\n'
                        'Não invoque rotação global CCX nem altere autenticação deste perfil.\n',encoding='utf-8')
        os.replace(temp,instruction)
        with self.transaction() as db:
            db.execute('INSERT INTO workers(id,cell_id) VALUES(?,?)', (worker, cell_id))
            event(db, 'worker_added', worker)
        return worker

    def worker(self, worker_id):
        return self.one('SELECT workers.*, cells.provider, cells.identity FROM workers JOIN cells ON cells.id=workers.cell_id WHERE workers.id=?', (worker_id,))

    def home(self, worker_id):
        worker = self.worker(worker_id)
        expected = self.root / 'profiles' / worker['provider'] / worker_id
        if canonical(expected) != os.path.normcase(str(expected)):
            raise ValueError('profile_redirected')
        marker = ccx.read_json(expected / 'ccx-profile.json')
        if marker != {'worker': worker_id, 'provider': worker['provider']}:
            raise ValueError('profile_not_owned')
        return expected

    def worker_lock(self, worker_id):
        return processes.file_lock(self.root / 'locks' / f'worker-{worker_id}.lock')

    def bind(self, worker_id, identity):
        with self.transaction() as db:
            worker = db.execute('SELECT * FROM workers WHERE id=?', (worker_id,)).fetchone()
            cell = db.execute('SELECT * FROM cells WHERE id=?', (worker['cell_id'],)).fetchone()
            if cell['identity'] and cell['identity'] != identity:
                raise ValueError('identity_mismatch')
            other = db.execute('SELECT id FROM cells WHERE provider=? AND identity=? AND id!=?',
                               (cell['provider'], identity, cell['id'])).fetchone()
            if other:
                raise ValueError('identity_already_registered')
            db.execute("UPDATE cells SET identity=?,auth='ready',next_poll=0 WHERE id=?", (identity, cell['id']))
            db.execute("UPDATE workers SET auth='ready',retry_after=0 WHERE id=?", (worker_id,))

    def pause(self, cell_id, paused):
        with self.transaction() as db:
            if db.execute('UPDATE cells SET paused=? WHERE id=?', (int(paused), cell_id)).rowcount != 1:
                raise ValueError('not_found')
            event(db, 'cell_paused' if paused else 'cell_resumed', cell_id)

    def configure(self, cell_id, *, plan=None, weight=None, weekly_weight=None, reserve=None):
        if plan is not None:
            cell = self.one('SELECT provider FROM cells WHERE id=?', (cell_id,))
            if plan not in plans(cell['provider']):
                raise ValueError('invalid_plan')
            weight = PLAN_WEIGHTS[plan] if weight is None else weight
        values = {key:value for key,value in locals().items() if key in ('weight','weekly_weight','reserve') and value is not None}
        for key,value in values.items():
            number(value,0 if key=='reserve' else .1,80 if key=='reserve' else 100)
        if plan is not None:
            values['plan'] = plan
        if not values:
            return
        with self.transaction() as db:
            if db.execute('UPDATE cells SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),cell_id)).rowcount!=1:
                raise ValueError('not_found')
            event(db,'cell_configured',cell_id)

    def submit(self, provider, prompt, cwd, model, *, effort='high', permission='read-only',
               cost=15, priority=0, title='', request_id=None, preferred_cell=None,
               guards=None, allowed_tools=None, persist=True):
        if provider not in ('claude', 'codex') or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 200_000:
            raise ValueError('invalid_prompt_or_provider')
        efforts = ('low','medium','high','max') if provider=='claude' else ('low','medium','high','xhigh')
        if not MODEL.fullmatch(model) or effort not in efforts:
            raise ValueError('invalid_model_or_effort')
        if permission not in ('read-only', 'write') or type(priority) is not int or not 0 <= priority <= 10:
            raise ValueError('invalid_permission_or_priority')
        number(cost, .1, 80)
        if not str(cwd).strip():
            raise ValueError('cwd_required')
        cwd = canonical(cwd)
        if not Path(cwd).is_dir():
            raise ValueError('cwd_missing')
        if len(title) > 100 or any(ord(char) < 32 for char in title):
            raise ValueError('invalid_title')
        if request_id and not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', request_id):
            raise ValueError('invalid_request_id')
        guards = guards or {}
        if any(key not in GUARDS or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', str(val)) for key, val in guards.items()):
            raise ValueError('invalid_guards')
        if allowed_tools is not None and (provider != 'claude' or not isinstance(allowed_tools, list) or
                any(tool not in ('Read','Edit','Write','Bash','Glob','Grep','Skill') for tool in allowed_tools)):
            raise ValueError('unsupported_tools')
        if provider == 'claude' and permission == 'read-only' and allowed_tools is not None and any(tool not in ('Read','Glob','Grep') for tool in allowed_tools):
            raise ValueError('read_only_tools_required')
        if preferred_cell:
            cell = self.one('SELECT * FROM cells WHERE id=?', (preferred_cell,))
            if cell['provider'] != provider:
                raise ValueError('provider_mismatch')
        options = {'model': model, 'effort': effort, 'permission': permission, 'cell': preferred_cell,
                   'guards': guards, 'allowed_tools': allowed_tools, 'persist': bool(persist)}
        fingerprint = hashlib.sha256(json.dumps([provider,prompt,cwd,options,cost,priority,title], sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            if request_id:
                previous = db.execute('SELECT id,fingerprint FROM jobs WHERE request_id=?', (request_id,)).fetchone()
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise ValueError('request_id_conflict')
                    return previous['id']
            job_id = uuid.uuid4().hex
            folder = self.root / 'jobs' / job_id
            folder.mkdir(parents=True, mode=0o700)
            ccx.write_json(folder / 'input.json', {'prompt': prompt})
            db.execute('''INSERT INTO jobs(id,request_id,fingerprint,provider,title,cwd,options,cost,priority,state,created)
                       VALUES(?,?,?,?,?,?,?,?,?,'queued',?)''',
                       (job_id,request_id,fingerprint,provider,title or f'Tarefa {provider}',cwd,json.dumps(options),cost,priority,time.time()))
            event(db, 'queued', job_id)
        return job_id

    def cancel(self, job_id):
        with self.transaction() as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise ValueError('not_found')
            if job['state'] in TERMINAL:
                return
            state = 'cancelled' if job['state'] == 'queued' else job['state']
            db.execute('UPDATE jobs SET cancel=1,state=?,reason=? WHERE id=?', (state,'operator_cancelled',job_id))
            event(db, 'cancel_requested', job_id)

    def snapshot(self):
        service = self.one('SELECT * FROM service')
        service['alive'] = processes.live(service['pid'], service['marker'])
        cells = self.rows('SELECT * FROM cells ORDER BY provider,id')
        for cell in cells:
            cell.pop('identity', None)
            cell['windows'] = json.loads(cell['windows'])
            reservations = self.rows('SELECT amounts FROM reservations WHERE cell_id=?', (cell['id'],))
            cell['reserved'] = {}
            for row in reservations:
                for key, amount in json.loads(row['amounts']).items():
                    cell['reserved'][key] = cell['reserved'].get(key, 0) + amount
            cell['workers'] = self.rows('SELECT id,auth,job_id,login FROM workers WHERE cell_id=?', (cell['id'],))
            cell['active'] = sum(bool(worker['job_id']) for worker in cell['workers'])
        jobs = self.rows('SELECT id,title,provider,cwd,state,reason,created,started,finished,cell_id,worker_id,cancel,session,exit_code FROM jobs ORDER BY created DESC LIMIT 100')
        return {'schema_version':1, 'at':time.time(), 'service':service, 'cells':cells, 'jobs':jobs,
                'events':self.rows('SELECT at,kind,entity FROM events ORDER BY id DESC LIMIT 50')}
