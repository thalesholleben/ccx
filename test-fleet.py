"""Regressões de contratos, reservas e ciclo de vida em processos reais."""
import base64
import contextlib
import importlib.util
import io
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import ccx
import ccx_codex
from fleet import processes, providers, runner, scheduler, service, terminal
from fleet.store import Store, canonical, overlaps

REPO=Path(__file__).resolve().parent
FIXTURE=REPO/'tests'/'fleet-fixture.py'


def eventually(predicate,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:
            return value
        time.sleep(.15)
    raise AssertionError('condition_timeout')


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ccx-fleet-tests-')
        self.base=Path(self.temp.name)
        self.store=Store(self.base/'state')

    def tearDown(self):
        service.stop(self.store)
        for job in self.store.rows("SELECT id FROM jobs WHERE state NOT IN ('completed','cancelled','failed','needs_attention')"):
            self.store.cancel(job['id'])
        try:
            eventually(lambda:not service.status(self.store)['alive'],15)
            eventually(lambda:self.settled(),15)
            def service_unlocked():
                try:
                    with processes.file_lock(self.store.root/'locks/service.lock'):return True
                except BlockingIOError:return False
            eventually(service_unlocked,15)
        finally:
            for job in self.store.rows('SELECT id FROM jobs'):
                processes.remove_task(self.store.root,'job-'+job['id'])
            processes.remove_task(self.store.root,'service')
            self.temp.cleanup()

    def settled(self):
        service.reconcile(self.store)
        return not self.store.rows('SELECT id FROM workers WHERE job_id IS NOT NULL AND pid IS NOT NULL') and not any(processes.live(j['pid'],j['marker']) for j in self.store.rows('SELECT * FROM jobs'))

    def cell(self,name='test',weight=1,workers=1,weekly=0):
        worker=self.store.add_cell(name,'claude','custom',weight=weight)
        self.store.bind(worker,'identity-'+name)
        for _ in range(workers-1):
            self.store.bind(self.store.add_worker(name),'identity-'+name)
        now=time.time()
        windows=[{'key':'five_hour','used':50,'reset':now+18000,'seconds':18000},
                 {'key':'seven_day','used':weekly,'reset':now+604800,'seconds':604800}]
        with self.store.transaction() as db:
            db.execute('UPDATE cells SET windows=?,observed=?,next_poll=? WHERE id=?',(json.dumps(windows),now,now+600,name))
        return worker

    def submit(self,**changes):
        values={'provider':'claude','prompt':'Teste','cwd':str(self.base),'model':'claude-opus-5-5'}
        values.update(changes)
        return self.store.submit(**values)

    def launch(self,job):
        processes.launch(self.store.root,'job-'+job['id'],['_runner',job['id'],job['ticket']],entry=FIXTURE)

    def start(self):
        original=processes.launch
        with patch('fleet.processes.launch',side_effect=lambda root,purpose,args:original(root,purpose,args,entry=FIXTURE)):
            return service.start(self.store)

    def test_weight_and_weekly_veto(self):
        for name,weight in [('pro',1),('max5',5),('max20',20)]:
            self.cell(name,weight)
        job=self.submit()
        selected=scheduler.dispatch(self.store)
        self.assertEqual(selected['cell_id'],'max20')
        reservation=json.loads(self.store.one('SELECT amounts FROM reservations WHERE job_id=?',(job,))['amounts'])
        self.assertEqual(reservation['five_hour'],.75)
        self.assertEqual(reservation['seven_day'],15)
        # Weekly is NOT multiplied by the per-session plan weight.
        cell=self.store.one("SELECT * FROM cells WHERE id='max20'")
        windows=json.loads(cell['windows']); windows[1]['used']=90
        self.assertEqual(scheduler.capacity(cell,windows,{},selected,time.time())[1],'draining')

    def test_atomic_multiprocess_reservations(self):
        self.cell(workers=8)
        for _ in range(12): self.submit()
        commands=[[sys.executable,str(FIXTURE),'--root',str(self.store.root),'dispatch'] for _ in range(12)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(lambda cmd:subprocess.run(cmd,capture_output=True,timeout=20),commands))
        self.assertTrue(all(r.returncode==0 for r in results))
        started=self.store.rows("SELECT * FROM jobs WHERE state='starting'")
        self.assertEqual(len(started),2) # 40pp available; 3 * 15 would overbook.
        self.assertEqual(len({j['worker_id'] for j in started}),2)

    def test_writer_overlap_and_reader_coexistence(self):
        self.cell(weight=20,workers=4)
        nested=self.base/'nested'; nested.mkdir()
        writer=self.submit(permission='write')
        self.assertEqual(scheduler.dispatch(self.store)['id'],writer)
        self.submit(cwd=str(nested))
        self.assertIsNone(scheduler.dispatch(self.store))
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(writer,))
        self.assertIsNone(scheduler.dispatch(self.store)) # terminal but runner not yet reconciled
        service.reconcile(self.store)
        self.assertIsNotNone(scheduler.dispatch(self.store))
        self.submit()
        self.assertIsNotNone(scheduler.dispatch(self.store))
        self.assertFalse(overlaps(canonical(self.base/'nested'),canonical(self.base/'nested-other')))

    def test_identity_and_profile_paths(self):
        first=self.cell('one')
        second=self.store.add_cell('two','claude')
        with self.assertRaises(ValueError): self.store.bind(second,'identity-one')
        home=self.store.home(first)
        ccx.write_json(home/'ccx-profile.json',{'worker':'other','provider':'claude'})
        with self.assertRaises(ValueError): self.store.home(first)

    def test_idempotency_validation(self):
        first=self.submit(request_id='abc')
        self.assertEqual(first,self.submit(request_id='abc'))
        with self.assertRaises(ValueError): self.submit(prompt='Other',request_id='abc')
        with self.assertRaises(ValueError): self.submit(cost=float('nan'))
        with self.assertRaises(ValueError): self.submit(guards={'OPENAI_API_KEY':'bad'})
        with self.assertRaises(ValueError): self.submit(allowed_tools=['Bash'])
        with self.assertRaises(ValueError): self.submit(provider='codex',effort='max')
        with self.assertRaises(ValueError): self.submit(effort='unsupported')
        with self.assertRaises(ValueError): self.submit(cwd='')

    def test_schema_one_migration_preserves_accounts(self):
        worker=self.cell()
        with self.store.connect() as db:
            db.execute('ALTER TABLE workers DROP COLUMN retry_after')
            db.execute('PRAGMA user_version=1')
        migrated=Store(self.store.root)
        self.assertEqual(migrated.worker(worker)['retry_after'],0)
        self.assertEqual(migrated.worker(worker)['cell_id'],'test')

    def test_simple_registration_and_plan_defaults(self):
        for plan,weight in [('pro',1),('max5',5),('max20',20),('custom',1)]:
            self.store.add_cell(plan,'claude',plan)
            cell=self.store.one('SELECT * FROM cells WHERE id=?',(plan,))
            self.assertEqual((cell['weight'],cell['weekly_weight'],cell['reserve'],cell['max_active']),
                             (weight,1,10,0))
        self.store.configure('pro',plan='max20')
        cell=self.store.one("SELECT * FROM cells WHERE id='pro'")
        self.assertEqual((cell['plan'],cell['weight'],cell['weekly_weight'],cell['reserve']),('max20',20,1,10))
        with self.assertRaises(ValueError): self.store.add_cell('invalid','codex','max20')
        with self.assertRaises(ValueError): self.store.configure('pro',plan='invalid')

    def test_schema_two_migration_preserves_work_and_removes_caps(self):
        worker=self.cell(weight=5)
        job=self.submit(cost=1)
        scheduler.dispatch(self.store)
        reservation=self.store.one('SELECT * FROM reservations WHERE job_id=?',(job,))
        with self.store.transaction() as db:
            db.execute("UPDATE cells SET max_active=2,plan='legacy-tier'")
            db.execute('UPDATE service SET max_active=8')
            db.execute('PRAGMA user_version=2')
        migrated=Store(self.store.root)
        self.assertEqual(migrated.worker(worker)['job_id'],job)
        self.assertEqual(migrated.one('SELECT * FROM reservations WHERE job_id=?',(job,)),reservation)
        self.assertEqual(migrated.one('SELECT * FROM cells')['weight'],5)
        self.assertEqual(migrated.one('SELECT plan FROM cells')['plan'],'custom')
        self.assertEqual(migrated.one('SELECT max_active FROM cells')['max_active'],0)
        self.assertEqual(migrated.one('SELECT max_active FROM service')['max_active'],0)
        with migrated.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],5)

    def test_label_migration_and_edit_preserve_auth_jobs_and_identity(self):
        worker=self.cell(weight=5);self.auth(worker)
        home=self.store.home(worker)
        credential=(home/'.credentials.json').read_bytes()
        job=self.submit(cost=1);scheduler.dispatch(self.store)
        before_worker=self.store.worker(worker)
        before_job=self.store.one('SELECT * FROM jobs WHERE id=?',(job,))
        before_reservation=self.store.one('SELECT * FROM reservations WHERE job_id=?',(job,))
        with self.store.transaction() as db:
            db.execute('DROP INDEX cells_display_name')
            db.execute('ALTER TABLE cells DROP COLUMN display_name')
            db.execute('PRAGMA user_version=3')
        migrated=Store(self.store.root)
        self.assertEqual(migrated.one('SELECT display_name FROM cells')['display_name'],'test')
        migrated.configure('test',name='Cliente correto')
        self.assertEqual(migrated.snapshot()['cells'][0]['display_name'],'Cliente correto')
        self.assertEqual(migrated.worker(worker),before_worker)
        self.assertEqual(migrated.one('SELECT * FROM jobs WHERE id=?',(job,)),before_job)
        self.assertEqual(migrated.one('SELECT * FROM reservations WHERE job_id=?',(job,)),before_reservation)
        self.assertEqual((migrated.home(worker)/'.credentials.json').read_bytes(),credential)
        self.assertEqual(migrated.one('SELECT weight FROM cells')['weight'],5)
        for invalid in ('','   ','bad\nlabel','x'*49):
            with self.assertRaises(ValueError):migrated.configure('test',name=invalid)
        other=migrated.add_cell(None,'codex','plus',label='Outra conta')
        internal=migrated.worker(other)['cell_id']
        self.assertTrue(internal.startswith('cell-'))
        self.assertNotEqual(internal,'Outra conta')
        with self.assertRaisesRegex(ValueError,'display_name_in_use'):migrated.configure(internal,name='Cliente correto',plan='custom',weight=8)
        current=migrated.one('SELECT * FROM cells WHERE id=?',(internal,))
        self.assertEqual((current['display_name'],current['plan'],current['weight']),('Outra conta','plus',1))

    def test_codex_plans_and_legacy_weight_preservation(self):
        from fleet.cli import main
        for name,plan,weight in [('plus','plus',1),('codex-pro','pro',5),('claude-pro','pro',1)]:
            provider='claude' if name=='claude-pro' else 'codex'
            self.store.add_cell(name,provider,plan)
            self.assertEqual(self.store.one('SELECT weight FROM cells WHERE id=?',(name,))['weight'],weight)
        worker=self.store.add_cell('legacy','codex','pro',weight=1)
        self.store.bind(worker,'synthetic-codex')
        home=self.store.home(worker);(home/'auth.json').write_text('{"synthetic":"preserve"}')
        before_worker=self.store.worker(worker)
        self.store.add_cell('calibrated','codex','pro',weight=3)
        with self.store.transaction() as db:db.execute('PRAGMA user_version=4')
        migrated=Store(self.store.root)
        self.assertEqual(migrated.one("SELECT plan,weight FROM cells WHERE id='legacy'"),{'plan':'plus','weight':1})
        self.assertEqual(migrated.one("SELECT plan,weight FROM cells WHERE id='calibrated'"),{'plan':'custom','weight':3})
        self.assertEqual(migrated.worker(worker),before_worker)
        self.assertEqual((home/'auth.json').read_text(),'{"synthetic":"preserve"}')
        migrated.configure('legacy',plan='pro')
        self.assertEqual(Store(self.store.root).one("SELECT plan,weight FROM cells WHERE id='legacy'"),{'plan':'pro','weight':5})
        for plan in ('max20','pro20'):
            with self.assertRaises(ValueError):migrated.configure('legacy',plan=plan)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--root',str(self.store.root),'cell','add','CLI default','codex']),0)
        self.assertEqual(migrated.one("SELECT plan,weight FROM cells WHERE display_name='CLI default'"),{'plan':'plus','weight':1})

    def test_remove_account_preserves_history_and_releases_identity(self):
        worker=self.cell();home=self.auth(worker)
        self.cell('other');other_home=self.store.home(self.store.one("SELECT id FROM workers WHERE cell_id='other'")['id'])
        job=self.submit(preferred_cell='test');scheduler.dispatch(self.store)
        with self.assertRaisesRegex(ValueError,'cell_busy'):self.store.remove_cell('test')
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(job,))
            db.execute('UPDATE workers SET job_id=NULL WHERE id=?',(worker,))
        result=self.store.root/'jobs'/job/'result.json';result.write_text('{"text":"preserved"}')
        self.store.remove_cell('test')
        self.assertFalse(home.exists());self.assertTrue(other_home.exists())
        self.assertFalse(self.store.rows("SELECT * FROM cells WHERE id='test'"))
        self.assertFalse(self.store.rows('SELECT * FROM workers WHERE id=?',(worker,)))
        self.assertFalse(self.store.rows('SELECT * FROM reservations WHERE job_id=?',(job,)))
        self.assertEqual(result.read_text(),'{"text":"preserved"}')
        history=self.store.snapshot()['jobs'][0]
        self.assertEqual((history['state'],history['cell_id'],history['worker_id'],history['removed_cell_name']),('completed',None,None,'test'))
        self.assertFalse((self.store.root/'locks'/('worker-'+worker+'.lock')).exists())
        self.store.add_cell('test','claude','pro',label='Replacement')
        self.store.remove_cell('test')
        self.assertEqual(self.store.snapshot()['jobs'][0]['removed_cell_name'],'test')

    def test_remove_account_guards_and_cleanup_failure(self):
        worker=self.cell(workers=2);home=self.store.home(worker)
        other=self.store.one('SELECT id FROM workers WHERE id!=?',(worker,))['id']
        other_home=self.store.home(other)
        with self.store.worker_lock(worker):
            with self.assertRaisesRegex(ValueError,'cell_busy'):self.store.remove_cell('test')
        with self.store.transaction() as db:db.execute('UPDATE workers SET login=1 WHERE id=?',(worker,))
        with self.assertRaisesRegex(ValueError,'cell_busy'):self.store.remove_cell('test')
        with self.store.transaction() as db:db.execute('UPDATE workers SET login=0 WHERE id=?',(worker,))
        job=self.submit(preferred_cell='test')
        with self.assertRaisesRegex(ValueError,'cell_has_queued_jobs'):self.store.remove_cell('test')
        self.assertTrue(home.exists())
        self.store.cancel(job)
        original_rmtree=shutil.rmtree
        def cleanup(path):
            if path==home:raise PermissionError('synthetic')
            original_rmtree(path)
        with patch('fleet.store.shutil.rmtree',side_effect=cleanup):
            with self.assertRaisesRegex(RuntimeError,'cell_removed_cleanup_pending'):self.store.remove_cell('test')
        self.assertFalse(self.store.rows("SELECT * FROM cells WHERE id='test'"))
        self.assertTrue(home.exists())
        self.assertFalse(other_home.exists())
        failure=self.store.snapshot()['events'][0]
        self.assertEqual((failure['kind'],failure['entity']),('cell_profile_cleanup_failed',home.relative_to(self.store.root).as_posix()))

    def test_remove_missing_and_unowned_profiles(self):
        worker=self.cell();home=self.store.home(worker)
        assert home.resolve().is_relative_to(self.store.root.resolve())
        shutil.rmtree(home)
        self.store.remove_cell('test')
        self.assertFalse(self.store.rows("SELECT id FROM cells WHERE id='test'"))
        worker=self.cell();home=self.store.home(worker)
        (home/'ccx-profile.json').write_text('{bad')
        with self.assertRaisesRegex(RuntimeError,'cell_removed_cleanup_pending'):self.store.remove_cell('test')
        self.assertTrue(home.exists())
        self.assertFalse(self.store.rows("SELECT id FROM cells WHERE id='test'"))
        self.assertEqual(self.store.snapshot()['events'][0]['entity'],home.relative_to(self.store.root).as_posix())

    def test_submit_revalidates_affinity_inside_removal_transaction(self):
        self.cell()
        original=self.store.transaction
        @contextlib.contextmanager
        def remove_before_insert():
            with patch.object(self.store,'transaction',original):
                self.store.remove_cell('test')
                with original() as db:yield db
        with patch.object(self.store,'transaction',remove_before_insert):
            with self.assertRaisesRegex(ValueError,'not_found'):self.submit(preferred_cell='test')
        self.assertFalse(self.store.rows('SELECT id FROM jobs'))
        self.assertFalse((self.store.root/'jobs').exists())

    def test_cli_generates_cell_id_and_edits_label(self):
        from fleet.cli import main
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['--root',str(self.base),'cell','add','Cliente A','claude','--plan','max20']),0)
        account=Store(self.base).snapshot()['cells'][0]
        internal=account['id']
        self.assertEqual(account['display_name'],'Cliente A')
        self.assertTrue(internal.startswith('cell-'))
        self.assertIn('cell login '+internal,output.getvalue())
        self.assertEqual(main(['--root',str(self.base),'cell','configure',internal,'--name','Cliente B']),0)
        updated=Store(self.base).snapshot()['cells'][0]
        self.assertEqual((updated['id'],updated['display_name'],updated['weight']),(internal,'Cliente B',20))
        error=io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main(['--root',str(self.base),'cell','add','cliente b','claude']),2)
        self.assertIn('Já existe uma conta com esse nome',error.getvalue())

    def test_labels_cannot_shadow_other_ids_or_names(self):
        self.store.add_cell('conta-a','claude',label='Zulu')
        worker=self.store.add_cell(None,'claude',label='Alpha')
        internal=self.store.worker(worker)['cell_id']
        before=self.store.rows('SELECT * FROM cells ORDER BY id')
        profiles=list((self.store.root/'profiles').rglob('ccx-profile.json'))
        with self.assertRaisesRegex(ValueError,'display_name_conflicts_id'):
            self.store.add_cell(None,'claude',label='CONTA-A')
        with self.assertRaisesRegex(ValueError,'display_name_conflicts_id'):
            self.store.configure(internal,name='CONTA-A',plan='max20')
        with self.assertRaisesRegex(ValueError,'cell_id_conflicts_name'):
            self.store.add_cell('alpha','claude',label='Outro label')
        with self.assertRaisesRegex(ValueError,'display_name_in_use'):
            self.store.add_cell(None,'claude',label='ALPHA')
        self.assertEqual(self.store.rows('SELECT * FROM cells ORDER BY id'),before)
        self.assertEqual(list((self.store.root/'profiles').rglob('ccx-profile.json')),profiles)
        self.assertEqual(len(self.store.rows('SELECT * FROM workers')),2)
        self.assertEqual([c['display_name'] for c in self.store.snapshot()['cells']],['Alpha','Zulu'])
        self.store.configure('conta-a',name='conta-a') # Its own ID remains a valid label.

    def test_upgrade_requires_old_service_exit_and_stop_remains_available(self):
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        try:
            marker=ccx.process_start_marker(child.pid)
            with self.store.transaction() as db:
                db.execute('UPDATE service SET pid=?,marker=?,max_active=8',(child.pid,marker))
                db.execute('PRAGMA user_version=2')
            with self.assertRaisesRegex(RuntimeError,'upgrade_requires_service_stop'):
                Store(self.store.root)
            with self.store.connect() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT max_active FROM service').fetchone()[0],8)
            from fleet.cli import main
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--root',str(self.store.root),'service','stop']),0)
            self.assertEqual(self.store.one('SELECT stop FROM service')['stop'],1)
            with self.assertRaisesRegex(RuntimeError,'upgrade_requires_service_stop'):
                Store(self.store.root)
        finally:
            child.terminate();child.wait(timeout=10)
        migrated=Store(self.store.root)
        self.assertEqual(migrated.one('SELECT max_active FROM service')['max_active'],0)

    def test_no_numeric_cap_and_exclusive_profiles_remain(self):
        self.cell(workers=70)
        for _ in range(71): self.submit(cost=.1)
        started=[scheduler.dispatch(self.store) for _ in range(70)]
        self.assertTrue(all(started))
        self.assertEqual(len({job['worker_id'] for job in started}),70)
        self.assertIsNone(scheduler.dispatch(self.store)) # All 70 distinct profiles occupied.
        self.assertEqual(self.store.one("SELECT reason FROM jobs WHERE state='queued'")['reason'],'workers_busy')
        amounts=[json.loads(row['amounts']) for row in self.store.rows('SELECT amounts FROM reservations')]
        self.assertAlmostEqual(sum(amount['five_hour'] for amount in amounts),7)

    def test_unknown_stale_expired_and_model_scope(self):
        self.cell()
        job=self.store.one('SELECT * FROM jobs WHERE id=?',(self.submit(),))
        cell=self.store.one('SELECT * FROM cells')
        windows=json.loads(cell['windows'])
        windows.append({'key':'sonnet','used':100,'reset':time.time()+900,'seconds':604800,'model':'sonnet'})
        self.assertIsNotNone(scheduler.capacity(cell,windows,{},job,time.time())[0])
        windows[-1]['model']='opus'
        self.assertIsNone(scheduler.capacity(cell,windows,{},job,time.time())[0])
        self.assertIsNone(scheduler.capacity(cell,[],{},job,time.time())[0])
        self.assertIsNone(scheduler.capacity(cell,windows,{},job,time.time()+601)[0])
        windows[0]['reset']=time.time()-1
        self.assertEqual(scheduler.capacity(cell,windows[:2],{},job,time.time())[1],'awaiting_reset_measurement')

    def test_usage_strict_validation(self):
        raw={'five_hour':{'utilization':50,'resets_at':'2027-01-01T00:00:00Z'}}
        self.assertEqual(providers.parse_usage('claude',raw)[0]['used'],50)
        for value in (float('nan'),float('inf'),-1,101,True,'50'):
            raw['five_hour']['utilization']=value
            with self.assertRaises(ValueError): providers.parse_usage('claude',raw)
        raw={'rate_limit':{'primary_window':{'used_percent':25,'reset_after_seconds':30,'limit_window_seconds':604800}}}
        self.assertEqual(providers.parse_usage('codex',raw)[0]['seconds'],604800)

    def test_idle_windows_and_duplicate_model_scopes(self):
        worker=self.cell()
        self.auth(worker,expired=False)
        raw={'five_hour':{'utilization':0,'resets_at':None},'seven_day':{'utilization':10,'resets_at':None},
             'seven_day_sonnet':{'utilization':0,'resets_at':None},'seven_day_unused':{'utilization':None,'resets_at':None},
             'limits':[{'kind':'weekly_scoped','percent':20,'resets_at':time.time()+900,'scope':{'model':{'display_name':'Opus'}}},
                       {'kind':'weekly_scoped','percent':20,'resets_at':time.time()+1800,'scope':{'model':{'display_name':'Opus 1M'}}}]}
        parsed=providers.parse_usage('claude',raw)
        self.assertIsNone(parsed[0]['reset'])
        scoped=[w for w in parsed if w['key']=='scoped:opus']
        self.assertEqual(len(scoped),1)
        self.assertEqual(scoped[0]['reset'],raw['limits'][1]['resets_at'])
        with patch('ccx.http_json',return_value=raw): providers.poll_cell(self.store,'test')
        self.assertGreater(self.store.one('SELECT observed FROM cells')['observed'],0)
        self.submit()
        self.assertIsNotNone(scheduler.dispatch(self.store))
        with contextlib.redirect_stdout(io.StringIO()): terminal.fleet(self.store.snapshot())

    def test_login_accepts_idle_quota_before_bind(self):
        worker=self.store.add_cell('idle','claude')
        self.auth(worker,expired=False)
        with self.store.transaction() as db:
            db.execute("UPDATE cells SET identity=NULL,auth='waiting_auth'")
        with patch('fleet.providers.processes.ProcessTree'),patch('fleet.providers.command',return_value=['synthetic']),patch('subprocess.call',return_value=0),\
             patch('ccx.http_json',return_value={'five_hour':{'utilization':0,'resets_at':None}}):
            providers.login(self.store,worker)
        self.assertEqual(self.store.one('SELECT auth FROM cells')['auth'],'ready')

    def auth(self,worker,expired=True):
        home=self.store.home(worker)
        ccx.write_json(home/'.claude.json',{'oauthAccount':{'accountUuid':'acct','organizationUuid':'org'}})
        ccx.write_json(home/'.credentials.json',{'claudeAiOauth':{'accessToken':'synthetic-old','refreshToken':'synthetic-refresh',
            'expiresAt':(time.time()+(-60 if expired else 3600))*1000},'mcpOAuth':{'preserve':True},'unknown':7})
        auth=providers.credentials('claude',home)
        with self.store.transaction() as db:
            db.execute('UPDATE cells SET identity=? WHERE id=?',(auth[5],self.store.worker(worker)['cell_id']))
        return home

    def test_refresh_exclusive_preserves_fields(self):
        worker=self.cell(); home=self.auth(worker)
        calls=[]
        def refresh(block):
            calls.append(1); time.sleep(.1)
            return {**block,'accessToken':'synthetic-new','expiresAt':(time.time()+3600)*1000},''
        def prepare():
            try:
                with self.store.worker_lock(worker): providers.prepare_idle(self.store,worker)
            except BlockingIOError: pass
        with patch('ccx.refresh_token',side_effect=refresh):
            with ThreadPoolExecutor(max_workers=5) as pool: list(pool.map(lambda _:prepare(),range(5)))
            prepare()
        self.assertEqual(len(calls),1)
        data=ccx.read_json(home/'.credentials.json')
        self.assertEqual(data['mcpOAuth'],{'preserve':True})
        self.assertEqual(data['unknown'],7)

    def test_expired_refresh_admits_and_http_debt(self):
        worker=self.cell(); home=self.auth(worker)
        self.submit()
        raw={'five_hour':{'utilization':30,'resets_at':time.time()+18000}}
        with patch('ccx.refresh_token',return_value=({'accessToken':'synthetic-new','expiresAt':(time.time()+3600)*1000},'')),patch('ccx.http_json',return_value=raw):
            providers.poll_cell(self.store,'test')
        selected=scheduler.dispatch(self.store)
        self.assertIsNotNone(selected)
        with self.store.transaction() as db:
            db.execute('UPDATE reservations SET release_after=?',(time.time()-1,))
        with patch('ccx.http_json',side_effect=HTTPError('synthetic',429,'rate',{},None)):
            providers.poll_cell(self.store,'test')
        self.assertEqual(len(self.store.rows('SELECT * FROM reservations')),1)
        with patch('ccx.http_json',return_value=raw): providers.poll_cell(self.store,'test')
        self.assertEqual(len(self.store.rows('SELECT * FROM reservations')),0)
        with patch('ccx.http_json',side_effect=HTTPError('synthetic',401,'auth',{},None)):
            providers.poll_cell(self.store,'test')
        self.assertEqual(self.store.one('SELECT auth FROM cells')['auth'],'waiting_auth')

    def test_refresh_incomplete_rejected(self):
        worker=self.cell(); self.auth(worker)
        with patch('ccx.refresh_token',side_effect=lambda old:(old,'')):
            with self.store.worker_lock(worker), self.assertRaises(providers.AuthError): providers.prepare_idle(self.store,worker)

    def test_codex_empty_refresh_response_rejected(self):
        worker=self.store.add_cell('codex','codex')
        home=self.store.home(worker)
        def jwt(data):
            return 'synthetic.'+base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')+'.signature'
        data={'tokens':{'account_id':'synthetic-account','access_token':jwt({'exp':time.time()-60}),'refresh_token':'synthetic-refresh',
                        'id_token':jwt({'https://api.openai.com/auth':{'chatgpt_account_id':'synthetic-account'}})},'preserve':True}
        ccx.write_json(home/'auth.json',data)
        self.store.bind(worker,providers.credentials('codex',home)[5])
        with self.store.worker_lock(worker),patch('ccx.http_json',return_value={}):
            with self.assertRaises(providers.AuthError): providers.prepare_idle(self.store,worker)
        self.assertEqual(ccx.read_json(home/'auth.json'),data)

    def test_failed_refresh_holds_queue_and_dead_grant_not_retried(self):
        worker=self.cell(); self.auth(worker)
        self.submit()
        with patch('ccx.refresh_token',return_value=(None,'transient')):
            providers.poll_cell(self.store,'test')
        self.assertIsNone(scheduler.dispatch(self.store))
        with patch('ccx.refresh_token',return_value=(None,'dead')) as refresh:
            with self.store.transaction() as db: db.execute('UPDATE workers SET retry_after=0')
            providers.poll_cell(self.store,'test')
            providers.poll_cell(self.store,'test')
            self.assertEqual(refresh.call_count,1)
        with patch('ccx.refresh_token',return_value=(None,'dead')):
            with self.store.worker_lock(worker), self.assertRaises(providers.AuthError): providers.prepare_idle(self.store,worker)

    def test_environment_and_arguments(self):
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'s','OPENAI_API_KEY':'s','CLAUDE_CONFIG_DIR':'wrong','CODEX_HOME':'wrong',
                                   'CROSS_REVIEW_DEPTH':'9','ENTERPRISE_WORK_ID':'old','GH_TOKEN':'synthetic','CLAUDE_CODE_GIT_BASH_PATH':'git-bash-path'}):
            env=providers.environment('claude',self.base,{'CROSS_REVIEW_DEPTH':'1'})
            without=providers.environment('claude',self.base,{})
        self.assertNotIn('OPENAI_API_KEY',env)
        self.assertNotIn('CODEX_HOME',env)
        self.assertEqual(env['CROSS_REVIEW_DEPTH'],'1')
        self.assertNotIn('CROSS_REVIEW_DEPTH',without)
        self.assertNotIn('ENTERPRISE_WORK_ID',env)
        self.assertNotIn('GH_TOKEN',env)
        self.assertEqual(env['CLAUDE_CODE_GIT_BASH_PATH'],'git-bash-path')
        options={'model':'claude-opus-5-5','effort':'high','permission':'read-only','persist':False}
        with patch('fleet.providers.executable',return_value='native.exe'):
            cmd=providers.command('claude',options,str(self.base),self.base/'out')
            self.assertIn('Read,Glob,Grep',cmd)
            self.assertNotIn('--dangerously-skip-permissions',cmd)
            self.assertIn('--verbose',cmd)

    def test_native_cli_resolution_without_installed_provider(self):
        directory=self.base/'synthetic-bin';directory.mkdir()
        native=directory/('codex.exe' if os.name=='nt' else 'codex')
        native.touch();native.chmod(0o700)
        with patch.dict(os.environ,{'PATH':str(directory)}):
            self.assertEqual(Path(providers.executable('codex')),native)
            native.unlink()
            with self.assertRaisesRegex(FileNotFoundError,'native_cli_missing'):
                providers.executable('codex')
            if os.name=='nt':
                (directory/'codex.cmd').write_text('@echo off\n')
                arch='aarch64' if os.environ.get('PROCESSOR_ARCHITECTURE')=='ARM64' else 'x86_64'
                packaged=directory/'node_modules/@openai/codex/node_modules/@openai/codex-win32-test/vendor'/arch/'bin/codex.exe'
                packaged.parent.mkdir(parents=True);packaged.touch()
                self.assertEqual(Path(providers.executable('codex')),packaged)

    def test_terminal_and_snapshot_privacy(self):
        self.cell(); self.submit(prompt='PRIVATE-PROMPT')
        snapshot=self.store.snapshot()
        self.assertNotIn('PRIVATE-PROMPT',json.dumps(snapshot))
        self.assertNotIn('identity-test',json.dumps(snapshot))
        self.assertEqual(terminal.clean('A\x1b[31m\rB'),'A[31mB')
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream),patch('shutil.get_terminal_size',return_value=os.terminal_size((32,30))):
            terminal.fleet(snapshot)
        self.assertTrue(all(len(line)<=32 for line in stream.getvalue().splitlines()))
        self.assertNotIn('\x1b',stream.getvalue())

    def test_cli_pure_json(self):
        self.cell()
        completed=subprocess.run([sys.executable,str(REPO/'ccx.py'),'fleet','--root',str(self.store.root),'status','--json'],capture_output=True,text=True)
        self.assertEqual(completed.returncode,0)
        self.assertEqual(json.loads(completed.stdout)['schema_version'],1)

    def test_fleet_stats_replaces_global_rotation(self):
        self.cell('new-account')
        with patch('ccx.collect',side_effect=AssertionError('legacy_called')),contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(ccx.main(['--root',str(self.store.root),'stats','--no-color']),0)
        self.assertIn('new-account',stream.getvalue())
        self.assertNotIn('TAREFAS',stream.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as stdout,contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(ccx.main(['hook']),0)
        self.assertEqual(stdout.getvalue()+stderr.getvalue(),'')
        for action in ('auto','switch','add'):
            with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(ccx.main([action]),2)

    def test_slim_status_keeps_three_accounts_and_reset_hours(self):
        for name in ('account-a','account-b','account-c'):self.cell(name)
        snapshot=self.store.snapshot()
        snapshot['cells'][1]['observed']=1
        for cell in snapshot['cells']:
            cell['windows'][0]['reset']=snapshot['at']+9000
            cell['windows'][1]['reset']=snapshot['at']+45*3600
        for width in (32,64,100,120):
            with contextlib.redirect_stdout(io.StringIO()) as stream:terminal.compact(snapshot,True,width)
            lines=stream.getvalue().splitlines()
            self.assertTrue(all(len(line)<=width for line in lines))
            self.assertNotIn('cache',stream.getvalue())
            self.assertIn('reset 2.5h',stream.getvalue())
            self.assertIn('reset 45.0h',stream.getvalue())
            if width>=90:
                self.assertEqual(len(lines),4)
                self.assertTrue(all(name in lines[1] for name in ('account-a','account-b','account-c')))
        snapshot['cells'][0]['windows'][0]['reset']=None
        snapshot['cells'][1]['windows'][0]['reset']=snapshot['at']-10
        snapshot['cells'][2]['windows']=[]
        with contextlib.redirect_stdout(io.StringIO()) as stream:terminal.compact(snapshot,True,100)
        self.assertIn('reset n/d',stream.getvalue())
        self.assertIn('reset pend.',stream.getvalue())

    def test_protocol_transport_preserves_permissions_schema_and_guards(self):
        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        for provider,mode in [('claude','bypassPermissions'),('codex','danger-full-access')]:
            job=self.store.submit(provider,'Test',str(self.base),'model',permission='write',cli_mode=mode,
                output_schema=schema,persist=False,skip_git_check=True,guards={'CROSS_REVIEW_DEPTH':'1','CROSS_REVIEW_WORK_ID':'test'},
                allowed_tools=['Read','Bash','Skill'] if provider=='claude' else None)
            options=json.loads(self.store.one('SELECT options FROM jobs WHERE id=?',(job,))['options'])
            with patch('fleet.providers.executable',return_value='native.exe'):
                args=providers.command(provider,options,str(self.base),self.base/'result.txt')
            self.assertIn(mode,args)
            self.assertEqual(options['guards']['CROSS_REVIEW_DEPTH'],'1')
            if provider=='codex':
                self.assertEqual(args.count('--skip-git-repo-check'),1)
                self.assertEqual(json.loads((self.base/'output-schema.json').read_text()),schema)
            else:self.assertEqual(json.loads(args[args.index('--json-schema')+1]),schema)
        with self.assertRaisesRegex(ValueError,'invalid_cli_permission'):
            self.store.submit('claude','Test',str(self.base),'model',permission='read-only',cli_mode='bypassPermissions')

    def test_review_compatibility_regressions(self):
        from fleet import client
        with patch('fleet.client.Store',side_effect=sqlite3.OperationalError('private database path')):
            with self.assertRaisesRegex(RuntimeError,'^ccx_database_error:OperationalError$'):
                client.execute('claude','Test',str(self.base),'model',root=self.store.root)
        with patch('fleet.client.service.start'),patch.dict(os.environ,{'ENTERPRISE_EXECUTOR_DEPTH':'1'}):
            with self.assertRaises(subprocess.TimeoutExpired):
                client.execute('claude','Test',str(self.base),'model',root=self.store.root,timeout=.1,
                    effort='xhigh',guards={'CROSS_REVIEW_DEPTH':'1'})
        row=self.store.one('SELECT * FROM jobs')
        options=json.loads(row['options'])
        self.assertEqual(row['state'],'cancelled')
        self.assertEqual(options['effort'],'xhigh')
        self.assertEqual(options['guards']['ENTERPRISE_EXECUTOR_DEPTH'],'1')
        with patch('fleet.providers.executable',return_value='native.exe'):
            argv=providers.command('claude',options,str(self.base),self.base/'output.txt')
        self.assertEqual(argv[argv.index('--effort')+1],'xhigh')
        with self.assertRaisesRegex(ValueError,'unsupported_tools'):
            self.submit(permission='write',allowed_tools=['Bash(git diff a,b)'])

    def test_status_refresh_respects_idle_profiles_and_backoff(self):
        from fleet.cli import main
        for name in ('ready','backoff','paused','busy'):
            self.cell(name)
        with self.store.transaction() as db:
            db.execute('UPDATE cells SET next_poll=0')
            db.execute("UPDATE cells SET next_poll=?,error='http_429' WHERE id='backoff'",(time.time()+300,))
            db.execute("UPDATE cells SET paused=1 WHERE id='paused'")
            db.execute("UPDATE workers SET login=1 WHERE cell_id='busy'")
        with patch('fleet.providers.prepare_idle',return_value=(None,)*5+('identity-ready',)),patch('fleet.providers.fetch',return_value=[]) as fetch:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--root',str(self.store.root),'status','--refresh','--json']),0)
            self.assertEqual(fetch.call_count,1)
        with self.store.transaction() as db:db.execute('UPDATE workers SET login=0')

    def test_compact_narrow_and_over_limit(self):
        self.cell()
        snapshot=self.store.snapshot()
        snapshot['cells'][0]['windows'][0]['used']=120
        for width in (8,20,24):
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                terminal.compact(snapshot,True,width)
            self.assertTrue(all(len(line)<=width for line in stream.getvalue().splitlines()))
        output=runner.Output('claude')
        output.accept({'type':'result','subtype':'success','is_error':False,'structured_output':{'ok':True}})
        self.assertEqual(json.loads(output.text),{'ok':True})
        self.assertFalse(output.failed)

    def test_protocol_timeout_cancels_queued_work(self):
        from fleet.client import execute
        with patch('fleet.client.service.start'),self.assertRaises(subprocess.TimeoutExpired):
            execute('claude','No account',str(self.base),'model',root=self.store.root,timeout=.1)
        job=self.store.one('SELECT state,cancel FROM jobs')
        self.assertEqual((job['state'],job['cancel']),('cancelled',1))

    def test_open_current_store_does_not_require_writer_lock(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.transaction():
                opened=pool.submit(Store,self.store.root)
                self.assertEqual(opened.result(timeout=2).path,self.store.path)

    @unittest.skipUnless(os.name=='nt','Windows bootstrap contract')
    def test_restrictive_job_bootstrap(self):
        result=subprocess.run([sys.executable,str(FIXTURE),'--root',str(self.store.root),'_bootstrap'],capture_output=True,timeout=65)
        self.assertEqual(result.returncode,0)
        report=json.loads((self.store.root/'bootstrap.json').read_text())
        if report['bootstrap']=='started':
            self.assertTrue(service.status(self.store)['healthy'])
        else:
            self.assertIn(report['reason'],('bootstrap_failed','service_start_unconfirmed'))
            self.assertFalse(service.status(self.store)['healthy'])
        print('restrictive_job_bootstrap='+report['bootstrap'])

    def test_parser_rejects_false_success(self):
        for provider,items in [('claude',[{'type':'result','subtype':'success','is_error':True,'result':'error'}]),
                               ('codex',[{'type':'error'},{'type':'turn.completed'}])]:
            output=runner.Output(provider)
            for item in items: output.accept(item)
            self.assertTrue(output.failed)

    def test_real_runner_unique_ticket_and_detached(self):
        self.cell()
        record=self.base/'observed.json'
        job=self.submit(prompt=json.dumps({'record':str(record),'sleep':1,'text':'unique'}),guards={'CROSS_REVIEW_DEPTH':'1'})
        row=scheduler.dispatch(self.store)
        self.launch(row)
        self.launch(row)
        eventually(record.exists)
        eventually(lambda:self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state']=='completed')
        eventually(self.settled)
        events=self.store.rows("SELECT * FROM events WHERE kind='runner_started'")
        self.assertEqual(len(events),1)
        observed=json.loads(record.read_text())
        self.assertEqual(observed['guard'],'1')
        self.assertFalse(observed['auth_override'])
        self.assertIn(str(self.store.root),observed['home'])

    def test_two_cells_survive_service_stop_and_restart(self):
        self.cell('one',20); self.cell('two',20)
        ids=[]
        for index in range(2):
            ids.append(self.submit(prompt=json.dumps({'record':str(self.base/f'observed-{index}.json'),'sleep':9}),preferred_cell=['one','two'][index]))
        initial=self.start()
        eventually(lambda:all(self.store.one('SELECT pid FROM jobs WHERE id=?',(job,))['pid'] for job in ids))
        self.assertTrue(processes.live(initial['pid'],initial['marker']))
        os.kill(initial['pid'],signal.SIGTERM) # Crash only our temporary fixture daemon.
        eventually(lambda:not service.status(self.store)['alive'])
        for job in ids:
            row=self.store.one('SELECT * FROM jobs WHERE id=?',(job,))
            self.assertTrue(processes.live(row['pid'],row['marker']))
        new=self.start()
        self.assertNotEqual(initial['pid'],new['pid'])
        service.stop(self.store) # Graceful stop also preserves both independent runners.
        eventually(lambda:all(self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state']=='completed' for job in ids))
        self.assertEqual(len(self.store.rows("SELECT * FROM events WHERE kind='runner_started'")),2)
        homes=[json.loads((self.base/f'observed-{i}.json').read_text())['home'] for i in range(2)]
        self.assertNotEqual(*homes)

    def test_cancel_terminates_descendants(self):
        self.cell()
        record=self.base/'parent.json'; child=self.base/'child.pid'
        job=self.submit(prompt=json.dumps({'record':str(record),'child':str(child),'sleep':120}))
        self.launch(scheduler.dispatch(self.store))
        eventually(child.exists)
        child_pid=int(child.read_text()); marker=ccx.process_start_marker(child_pid)
        self.store.cancel(job)
        eventually(self.settled)
        self.assertFalse(processes.live(child_pid,marker))
        self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state'],'cancelled')

    def test_missing_terminal_stays_attention(self):
        self.cell()
        job=self.submit(prompt=json.dumps({'record':str(self.base/'record.json'),'missing':True}))
        self.launch(scheduler.dispatch(self.store))
        eventually(lambda:self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state']=='needs_attention')

    def test_dead_runner_worker_is_blocked_and_healthy_worker_runs(self):
        first=self.cell(weight=20,workers=2)
        workers=self.store.rows('SELECT id FROM workers ORDER BY id')
        dead,good=workers[0]['id'],workers[1]['id']
        self.auth(dead)
        ccx.write_json(self.store.root/'faults.json',{dead:'dead'})
        jobs=[self.submit(prompt=json.dumps({'record':str(self.base/f'job-{i}.json')})) for i in range(3)]
        for index,job_id in enumerate(jobs):
            chosen=scheduler.dispatch(self.store)
            self.assertEqual(chosen['worker_id'],dead if index==0 else good)
            self.launch(chosen)
            expected='failed' if index==0 else 'completed'
            eventually(lambda:self.store.one('SELECT state FROM jobs WHERE id=?',(job_id,))['state']==expected)
            # Process death can occur between reconcile() and the live-PID check.
            # Admission needs the worker released as well as the process gone.
            eventually(lambda:self.settled() and not self.store.rows('SELECT id FROM workers WHERE job_id=?',(job_id,)))
        self.assertEqual(self.store.worker(dead)['auth'],'waiting_auth')
        self.assertEqual(len((self.store.root/'refresh-count.txt').read_text().splitlines()),1)

    def test_transient_preflight_backoff_and_cli_auth_failure(self):
        worker=self.cell(weight=20)
        ccx.write_json(self.store.root/'faults.json',{worker:'transient'})
        first=self.submit()
        self.launch(scheduler.dispatch(self.store))
        eventually(lambda:self.store.one('SELECT state FROM jobs WHERE id=?',(first,))['state']=='failed')
        eventually(lambda:self.settled() and not self.store.worker(worker)['job_id'])
        self.assertGreater(self.store.worker(worker)['retry_after'],time.time())
        second=self.submit(prompt=json.dumps({'record':str(self.base/'auth-failure.json'),'auth_error':True}))
        self.assertIsNone(scheduler.dispatch(self.store))
        ccx.write_json(self.store.root/'faults.json',{})
        with self.store.transaction() as db:
            db.execute('UPDATE workers SET retry_after=0')
            db.execute("UPDATE cells SET auth='ready'")
        self.launch(scheduler.dispatch(self.store))
        eventually(lambda:self.store.one('SELECT state FROM jobs WHERE id=?',(second,))['state']=='needs_attention')
        self.assertEqual(self.store.worker(worker)['auth'],'waiting_auth')

    def test_runner_waits_for_profile_lock(self):
        worker=self.cell()
        job=self.submit(prompt=json.dumps({'record':str(self.base/'lock.json')}))
        selected=scheduler.dispatch(self.store)
        with self.store.worker_lock(worker):
            self.launch(selected)
            time.sleep(1.4)
            self.assertEqual(self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state'],'starting')
        eventually(lambda:self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state']=='completed')

    def test_runner_lock_timeout_is_known_preflight_failure(self):
        self.cell(); job=self.submit(); selected=scheduler.dispatch(self.store)
        with patch('fleet.processes.waiting_lock',side_effect=BlockingIOError):
            self.assertEqual(runner.run(self.store,job,selected['ticket']),2)
        self.assertEqual(self.store.one('SELECT state,reason FROM jobs WHERE id=?',(job,)),{'state':'failed','reason':'profile_busy_before_start'})
        self.assertFalse(self.store.rows('SELECT * FROM reservations'))

    def test_idle_service_updates_usage_every_fifteen_minutes_without_jobs(self):
        worker=self.cell();self.auth(worker,expired=False)
        observed=self.store.one('SELECT observed FROM cells')['observed']
        raw={'five_hour':{'utilization':6,'resets_at':observed+18000}}
        with patch('ccx.http_json',return_value=raw) as http,patch('ccx.refresh_token') as refresh:
            with patch('time.time',return_value=observed+899):service.cycle(self.store)
            http.assert_not_called()
            with patch('time.time',return_value=observed+900):service.cycle(self.store)
            http.assert_called_once()
            updated=self.store.one('SELECT observed,windows FROM cells')
            self.assertEqual(updated['observed'],observed+900)
            self.assertEqual(json.loads(updated['windows'])[0]['used'],6)
            self.assertEqual(service.poll_targets(self.store,observed+1799),[])
            self.assertEqual(service.poll_targets(self.store,observed+1800),['test'])
        refresh.assert_not_called()
        self.assertEqual(self.store.rows('SELECT id FROM jobs'),[])

    def test_idle_poll_respects_paused_auth_backoff_and_keeps_queue_cadence(self):
        now=time.time()
        for name in ('ready','paused','login','backoff'):
            self.cell(name)
        with self.store.transaction() as db:
            db.execute('UPDATE cells SET observed=?,next_poll=0',(now-900,))
            db.execute("UPDATE cells SET paused=1 WHERE id='paused'")
            db.execute("UPDATE cells SET auth='waiting_auth' WHERE id='login'")
            db.execute("UPDATE cells SET error='http_429',next_poll=? WHERE id='backoff'",(now+240,))
        self.assertEqual(service.poll_targets(self.store,now),['ready'])
        self.assertEqual(service.poll_targets(self.store,now+240),['ready','backoff'])
        with self.store.transaction() as db:
            db.execute('UPDATE cells SET observed=?,next_poll=?',(now,now+240))
        self.submit(preferred_cell='ready')
        self.assertEqual(service.poll_targets(self.store,now+239),[])
        self.assertEqual(service.poll_targets(self.store,now+240),['ready'])

    def test_service_survives_cycle_exception(self):
        result=subprocess.run([sys.executable,str(FIXTURE),'--root',str(self.store.root),'_serve_failure'],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0)
        events=self.store.rows('SELECT kind FROM events')
        self.assertIn({'kind':'cycle_error_RuntimeError'},events)
        self.assertNotIn('synthetic-private-message',json.dumps(self.store.snapshot()))

    def test_wait_without_service_and_run_bootstrap_failure(self):
        from fleet.cli import wait,main
        job=self.submit()
        with contextlib.redirect_stderr(io.StringIO()): self.assertEqual(wait(self.store,job),3)
        with patch('fleet.service.start',side_effect=RuntimeError('bootstrap_failed')),patch('sys.stdin',io.StringIO('Test')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--root',str(self.store.root),'run','claude']),2)
        self.assertEqual(len(self.store.rows('SELECT id FROM jobs')),1)

    def test_real_codex_runner_contract(self):
        worker=self.store.add_cell('codex','codex')
        self.store.bind(worker,'synthetic-codex')
        now=time.time()
        with self.store.transaction() as db:
            db.execute('UPDATE cells SET windows=?,observed=?',(json.dumps([
                {'key':'primary','used':0,'reset':now+604800,'seconds':604800}]),now))
        record=self.base/'codex.json'
        job=self.submit(provider='codex',model='gpt-6-sol',prompt=json.dumps({'record':str(record)}))
        self.launch(scheduler.dispatch(self.store))
        eventually(lambda:self.store.one('SELECT state FROM jobs WHERE id=?',(job,))['state']=='completed')
        result=ccx.read_json(self.store.root/'jobs'/job/'result.json')
        self.assertEqual(result['session'],'synthetic-session')
        self.assertIn('codex',json.loads(record.read_text())['home'])

    def test_workspace_quota_consumer_uses_fleet_json(self):
        parser_path=REPO.parent/'gerentes'/'nucleo'/'cota.py'
        if not parser_path.is_file():self.skipTest('workspace consumer unavailable in public package')
        sys.path.insert(0,str(parser_path.parents[1]))
        from nucleo import cota as module
        self.cell()
        cfg={'caminhos':{'ccx_py':'ccx-fleet.py'},'cota':{'max_7d':75}}
        with patch.object(module.base,'rodar_json',return_value=(self.store.snapshot(),'')) as query:
            parsed=module.ler_claude(cfg)
        self.assertEqual(parsed,{'5h':50,'7d':0,'motivo':''})
        self.assertEqual(query.call_args.args[0][-3:],['status','--refresh','--json'])


if __name__=='__main__':
    unittest.main(verbosity=2)
