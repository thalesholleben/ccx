"""Adapters dos CLIs oficiais; nenhum grant global é lido ou modificado."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import ccx
import ccx_codex
from .store import number, event, GUARDS
from . import processes


class AuthError(ValueError):
    pass


def executable(provider):
    if os.name == 'nt' and provider == 'codex':
        shim = shutil.which('codex.cmd')
        if shim:
            package = Path(shim).parent / 'node_modules' / '@openai' / 'codex'
            matches = list(package.glob('node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe'))
            matches += list(package.glob('node_modules/@openai/codex-win32-*/vendor/*/codex/codex.exe'))
            matches += list(package.glob('vendor/*/codex/codex.exe'))
            arch = 'aarch64' if os.environ.get('PROCESSOR_ARCHITECTURE') == 'ARM64' else 'x86_64'
            matches = [path for path in matches if arch in str(path)]
            if len(matches) == 1:
                return str(matches[0])
    native = shutil.which(provider + ('.exe' if os.name == 'nt' else ''))
    if native:
        return native
    raise FileNotFoundError('native_cli_missing')


def environment(provider, home, guards=None):
    # A service may have been started by another agent; inherit runtime paths, not its secrets/guards.
    allowed = {'PATH','PATHEXT','SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','TEMP','TMP','TMPDIR',
               'USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','USERNAME','USERDOMAIN','APPDATA','LOCALAPPDATA',
               'PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432','PROGRAMDATA','ALLUSERSPROFILE',
               'PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS','OS','TERM','LANG','LC_ALL','TZ',
               'PYTHONUTF8','PYTHONIOENCODING','CLAUDE_CODE_GIT_BASH_PATH','SSL_CERT_FILE','REQUESTS_CA_BUNDLE'}
    env = {key:value for key,value in os.environ.items() if key.upper() in allowed}
    env['CLAUDE_CONFIG_DIR' if provider == 'claude' else 'CODEX_HOME'] = str(home)
    env.update({key: str(value) for key, value in (guards or {}).items() if key in GUARDS})
    return env


def _credentials(provider, home):
    path = home / ('.credentials.json' if provider == 'claude' else 'auth.json')
    data = ccx.read_json(path)
    block = data.get('claudeAiOauth' if provider == 'claude' else 'tokens')
    if not isinstance(block, dict):
        raise AuthError('waiting_auth')
    if provider == 'claude':
        account = ccx.read_json(home / '.claude.json').get('oauthAccount', {})
        parts = [account.get('accountUuid'), account.get('organizationUuid')]
        access, expiry = block.get('accessToken'), block.get('expiresAt')
        if expiry is not None:
            expiry = number(expiry, 1, 1e15) / 1000
    else:
        account = ccx_codex.identity_from_tokens(block)
        parts = [account.get('account_id'), account.get('workspace_id') or 'default']
        access = block.get('access_token')
        expiry = ccx_codex.jwt_claims(access or '').get('exp')
    if not isinstance(access, str) or not access or not all(isinstance(p, str) and p for p in parts):
        raise AuthError('waiting_auth')
    if expiry is None:
        raise AuthError('expiry_unknown')
    number(expiry, 1, 1e12)
    fingerprint = hashlib.sha256(json.dumps([provider, *parts]).encode()).hexdigest()
    return path, data, block, access, expiry, fingerprint, account


def credentials(provider, home):
    try:
        return _credentials(provider,home)
    except AuthError:
        raise
    except (ccx.CorruptFile, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AuthError('invalid_credentials') from exc


def _prepare_idle(store, worker_id):
    """Caller MUST hold worker_lock across this operation and any subsequent CLI."""
    worker = store.worker(worker_id)
    home = store.home(worker_id)
    path, data, block, access, expiry, fingerprint, _ = credentials(worker['provider'], home)
    if worker['identity'] and worker['identity'] != fingerprint:
        raise AuthError('identity_mismatch')
    if expiry <= time.time() + 120:
        refresh = ccx.refresh_token if worker['provider'] == 'claude' else ccx_codex.refresh_tokens
        new, error = refresh(block)
        if error or not isinstance(new, dict):
            raise AuthError('waiting_auth' if error == 'dead' else 'refresh_unavailable')
        key = 'claudeAiOauth' if worker['provider'] == 'claude' else 'tokens'
        if worker['provider'] == 'claude':
            new_access, new_expiry = new.get('accessToken'), new.get('expiresAt', 0) / 1000
        else:
            new_access = new.get('access_token')
            new_expiry = ccx_codex.jwt_claims(new_access or '').get('exp', 0)
            old_identity, new_identity = ccx_codex.identity_from_tokens(block), ccx_codex.identity_from_tokens(new)
            if any(old_identity.get(key) != new_identity.get(key) for key in ('account_id','workspace_id')):
                raise AuthError('identity_mismatch')
        if not isinstance(new_access, str) or not new_access or new_access == access or number(new_expiry, 0, 1e12) <= time.time() + 120:
            raise AuthError('refresh_incomplete')
        # Preserve unrelated/MCP fields. Lock excludes writers for the entire refresh.
        data[key] = new
        ccx.write_json(path, data)
    return credentials(worker['provider'], home)


def prepare_idle(store, worker_id):
    try:
        return _prepare_idle(store,worker_id)
    except AuthError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise AuthError('refresh_incomplete') from exc


def auth_failure(db, worker_id, reason, now=None):
    """Apply to the failing worker, inside the caller's finish/poll transaction."""
    now=time.time() if now is None else now
    permanent=reason in ('http_401','http_403','waiting_auth','identity_mismatch','expiry_unknown','invalid_credentials')
    db.execute('UPDATE workers SET auth=?,retry_after=? WHERE id=?',
               ('waiting_auth' if permanent else 'ready',0 if permanent else now+240,worker_id))
    cell_id=db.execute('SELECT cell_id FROM workers WHERE id=?',(worker_id,)).fetchone()[0]
    ready=db.execute("SELECT COUNT(*) FROM workers WHERE cell_id=? AND auth='ready' AND retry_after<=?",(cell_id,now)).fetchone()[0]
    db.execute('UPDATE cells SET auth=?,error=?,next_poll=? WHERE id=?',
               ('ready' if ready else ('waiting_auth' if permanent else 'expired_refreshable'),reason,now if ready else now+240,cell_id))


def reset_epoch(value, now):
    if value is None:
        return None  # Known usage, window not started/reset not announced. TTL still applies.
    if isinstance(value, str):
        return number(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp(), 1, 1e12)
    return number(value, 1, 1e12)


def parse_usage(provider, raw, now=None):
    now = time.time() if now is None else now
    windows = []
    def add(key, used, reset, seconds, model=None):
        if used is None:
            return  # The provider reports unused/disabled model windows this way.
        window={'key': key, 'used': number(used), 'reset': reset_epoch(reset, now),
                'seconds': number(seconds, 1, 366*86400), 'model': model}
        previous=next((win for win in windows if win['key']==key),None)
        if previous is not None:
            previous['used']=max(previous['used'],window['used'])
            previous['reset']=max(previous['reset'],window['reset']) if previous['reset'] is not None and window['reset'] is not None else None
        else:
            windows.append(window)
    if provider == 'claude':
        for key, seconds in (('five_hour', 18000), ('seven_day', 604800)):
            win = raw.get(key)
            if isinstance(win, dict):
                add(key, win.get('utilization'), win.get('resets_at'), seconds)
        for index, limit in enumerate(raw.get('limits') or []):
            if not isinstance(limit, dict) or limit.get('kind') != 'weekly_scoped':
                continue
            scope = limit.get('scope') or {}
            model = (scope.get('model') or {}).get('display_name')
            # Unknown scoped restriction is conservative: apply to all models.
            model = next((family for family in ('opus','sonnet','haiku') if isinstance(model,str) and family in model.lower()),None)
            add('scoped:' + (model or '*'), limit.get('percent', limit.get('utilization')),
                limit.get('resets_at'), 604800, model)
        for key, win in raw.items():
            if key.startswith('seven_day_') and isinstance(win, dict) and win.get('utilization') is not None:
                model = key.removeprefix('seven_day_')
                add(key, win['utilization'], win.get('resets_at'), 604800, model if model in ('opus', 'sonnet') else None)
    else:
        groups = [('general', raw.get('rate_limit'), None)]
        for index, extra in enumerate(raw.get('additional_rate_limits') or []):
            groups.append((f'additional:{index}', extra.get('rate_limit'), extra.get('limit_name')))
        for label, rate, model in groups:
            if not isinstance(rate, dict):
                if label == 'general':
                    raise ValueError('quota_shape')
                continue
            for key in ('primary_window', 'secondary_window'):
                win = rate.get(key)
                if not isinstance(win, dict):
                    continue
                reset = win.get('reset_at')
                if reset is None:
                    reset = now + number(win.get('reset_after_seconds'), 0, 366*86400)
                # Extra limits with unknown model labels constrain every job.
                add(label+':'+key, win.get('used_percent'), reset, win.get('limit_window_seconds'))
    if not windows or len({w['key'] for w in windows}) != len(windows):
        raise ValueError('quota_shape')
    return windows


def fetch(provider, auth):
    _, _, _, access, expiry, _, account = auth
    if expiry <= time.time():
        raise AuthError('expired_refreshable')
    headers = {'Authorization': f'Bearer {access}', 'User-Agent': 'ccx-fleet/1.0', 'Accept': 'application/json'}
    if provider == 'claude':
        headers['anthropic-beta'] = ccx.BETA_HEADER
        url = ccx.USAGE_URL
    else:
        headers['chatgpt-account-id'] = account['account_id']
        url = ccx_codex.USAGE_URL
    return parse_usage(provider, ccx.http_json(urllib.request.Request(url, headers=headers), timeout=8))


def refresh_idle(store):
    """Manual dashboard/CLI refresh with the same idle-profile and retry guards."""
    updated=0
    for cell in store.rows('SELECT id,observed FROM cells WHERE paused=0 AND next_poll<=?',(time.time(),)):
        poll_cell(store,cell['id'],idle_only=True)
        current=store.one('SELECT observed FROM cells WHERE id=?',(cell['id'],))
        if current and current['observed']>cell['observed']:
            updated+=1
    return updated


def poll_cell(store, cell_id, now=None, *, idle_only=False):
    now = time.time() if now is None else now
    cell = store.one('SELECT * FROM cells WHERE id=?', (cell_id,))
    if idle_only and (cell['paused'] or cell['next_poll']>now or store.rows(
            'SELECT id FROM workers WHERE cell_id=? AND (job_id IS NOT NULL OR login=1)',(cell_id,))):
        return
    workers = store.rows("SELECT * FROM workers WHERE cell_id=? AND auth='ready' AND retry_after<=? ORDER BY id", (cell_id,now))
    if not workers:
        with store.transaction() as db:
            db.execute('UPDATE cells SET next_poll=? WHERE id=?',(now+240,cell_id))
        return
    worker = workers[0]
    try:
        try:
            with store.worker_lock(worker['id']):
                current = store.worker(worker['id'])
                if current['job_id'] or current['login']:
                    if idle_only:return
                    auth = credentials(cell['provider'], store.home(worker['id']))
                else:
                    auth = prepare_idle(store, worker['id'])
        except BlockingIOError:
            if idle_only:return
            auth = credentials(cell['provider'], store.home(worker['id']))
        if cell['identity'] != auth[5]:
            raise AuthError('identity_mismatch')
        windows = fetch(cell['provider'], auth)
        observed = time.time()
        previous = {w['key']: w for w in json.loads(cell['windows'])}
        for win in windows:
            before = previous.get(win['key'])
            delta = observed - cell['observed']
            win['burn'] = max(0, (win['used'] - before['used']) / delta) if before and before['reset'] == win['reset'] and delta >= 30 else 0
        with store.transaction() as db:
            db.execute("UPDATE workers SET auth='ready',retry_after=0 WHERE id=?", (worker['id'],))
            db.execute("UPDATE cells SET windows=?,observed=?,next_poll=?,auth='ready',error='' WHERE id=?",
                       (json.dumps(windows), observed, observed + (180 if any(w['job_id'] for w in workers) else 240),cell_id))
            # A GET initiated after settlement has elapsed absorbs completed reservations.
            db.execute('DELETE FROM reservations WHERE cell_id=? AND release_after<=?', (cell_id, now))
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            reason = f'http_{exc.code}' if exc.code in (401,403,429) else 'http_error'
        elif isinstance(exc, AuthError):
            reason = str(exc)
        elif isinstance(exc,(ValueError,TypeError,KeyError)):
            reason = 'quota_invalid'
        else:
            reason = 'quota_unavailable'
        auth_error = isinstance(exc,AuthError) or reason in ('http_401','http_403')
        with store.transaction() as db:
            if auth_error:
                auth_failure(db,worker['id'],reason,now)
            else:
                db.execute('UPDATE cells SET next_poll=?,error=? WHERE id=?',(now+240,reason,cell_id))
            if reason=='quota_invalid':
                db.execute('UPDATE cells SET observed=0 WHERE id=?',(cell_id,))


def command(provider, options, cwd, output, login=False):
    binary = executable(provider)
    if provider == 'claude':
        if login:
            return [binary, 'auth', 'login']
        argv = [binary, '-p', '--output-format', 'stream-json', '--verbose', '--model', options['model'],
                '--effort', options['effort'], '--permission-mode', options.get('cli_mode') or ('plan' if options['permission']=='read-only' else 'acceptEdits'),
                '--permission-prompts', 'none']
        if options['permission'] == 'read-only':
            tools=options.get('allowed_tools')
            argv += ['--tools', ','.join(['Read','Glob','Grep'] if tools is None else tools)]
        elif options.get('allowed_tools') is not None:
            tools=options['allowed_tools']
            argv += ['--tools', ','.join(dict.fromkeys(tool.split('(')[0] for tool in tools))]
            if tools:argv += ['--allowedTools', ','.join(tools)]
        if options.get('output_schema') is not None:
            argv += ['--json-schema', json.dumps(options['output_schema'])]
        if options.get('disable_slash_commands'):
            argv += ['--disable-slash-commands']
        if options.get('strict_mcp'):argv += ['--strict-mcp-config']
        if options.get('restricted'):argv += ['--restricted']
        if not options.get('persist', True):
            argv += ['--no-session-persistence']
        return argv
    prefix = [binary, '-c', 'cli_auth_credentials_store="file"']
    if login:
        return prefix + ['login']
    extra=[]
    if options.get('ignore_user_config'):extra += ['--ignore-user-config']
    if options.get('output_schema') is not None:
        schema_path=Path(output).with_name('output-schema.json')
        ccx.write_json(schema_path,options['output_schema'])
        extra += ['--output-schema',str(schema_path)]
    if options.get('skip_git_check',True): extra += ['--skip-git-repo-check']
    return prefix + ['-c', 'model_reasoning_effort='+json.dumps(options['effort']), 'exec', '--json',
                     '--color', 'never', '-C', cwd, '-s',
                     options.get('cli_mode') or ('read-only' if options['permission']=='read-only' else 'workspace-write'),
                     '-m', options['model'], '-o', str(output), *extra, *([] if options.get('persist',True) else ['--ephemeral']), '-']


def login(store, worker_id):
    with store.worker_lock(worker_id):
        tree = processes.ProcessTree()  # Login descendants cannot outlive the lock owner.
        worker = store.worker(worker_id)
        with store.transaction() as db:
            busy = db.execute('SELECT job_id,login FROM workers WHERE id=?', (worker_id,)).fetchone()
            if busy['job_id'] or busy['login']:
                raise ValueError('worker_busy')
            own = processes.identity()
            db.execute('UPDATE workers SET login=1,pid=?,marker=? WHERE id=?', (own['pid'],own['process_start'],worker_id))
        try:
            home = store.home(worker_id)
            code = subprocess.call(command(worker['provider'], {}, '', '', login=True), env=environment(worker['provider'],home))
            if code:
                raise AuthError('login_failed')
            auth = credentials(worker['provider'],home)
            fetch(worker['provider'],auth)  # Successful usage GET before binding local claims.
            store.bind(worker_id,auth[5])
        finally:
            with store.transaction() as db:
                db.execute('UPDATE workers SET login=0,pid=NULL,marker=NULL WHERE id=?', (worker_id,))
    poll_cell(store,worker['cell_id'])
