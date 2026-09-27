# CCX contributor instructions

CCX is a local account fleet for official Claude Code and Codex CLIs. Read
`README.md` and `docs/features/fleet.md` first. Before invoking another agent,
read `skills/ccx/SKILL.md`; this does not authorize extra delegation.

## Map

- `ccx-fleet.py`, `fleet/cli.py`: primary CLI. `ccx.py fleet` is an alias.
- `fleet/store.py`: SQLite state, validation, deduplication and schema migration.
- `fleet/scheduler.py`: quota, reservations, account selection, workspace exclusion.
- `fleet/providers.py`: official CLI adapters, isolated login, quota parsing/refresh.
- `fleet/service.py`, `runner.py`, `processes.py`: admission, reconciliation and process ownership.
- `fleet/panel.py`, `presentation.py`, `tray.py`: Tk dashboard and Windows tray.
- `skills/ccx`: generic distributable skill; `scripts/install-skill.py`: dual installation.
- `ccx.py`, `ccx_codex.py`, `ccx_runtime.py`: compatibility modules and reused primitives.
  Global monitor/bridge entry points are retired; installers accept uninstall only.
  ccx.py stats and ccx_codex.py status route to the fleet.

## Validate

`python scripts/check.py` runs offline regression suites and the installer test.
Desktop changes also require `python tests/panel-smoke.py` and
`python tests/tray-smoke.py` on interactive Windows. No real accounts in tests.
Pillow is optional for screenshots/icon exports. Runtime must stay stdlib-only.

## Invariants

- One account pool per identity; a separately authenticated OAuth home per worker.
  Do not clone grants, alter global client login, or open managed profiles externally.
- Login, refresh and execution hold the same OS worker lock. Credential writes
  preserve unrelated fields and replace atomically; corrupt JSON never means empty.
- Admission and reservation share one transaction. No numeric global/per-cell cap;
  schema 3 keeps retired max_active columns at zero for compatibility only.
- Unknown/stale quota or expired reset requires measurement, never projected zero.
  HTTP 429 is a measurement error; 401/403 block that profile for login.
- Session and weekly weights differ. Keep model-scoped windows and conservative
  reservations. Completed reservations need a delayed fresh measurement to release.
- One ticket claim, one runner. Ambiguous failures do not replay prompts. Stop the
  service without killing live runners; close the panel without stopping service.
- Overlapping paths exclude any job when one is a writer. Keep ownership until the
  process tree exits, even after a terminal event.
- Preserve permissions, model, effort, recursion guards and explicit account affinity.
- Errors/logs must not reveal credentials or private prompts. Never commit real state.
- Schema 4 adds display_name without changing IDs or credentials. UI/CLI registration
  generates an internal ID; label edits preserve jobs, reservations and profile paths.
- Schema 5 separates provider plan weights. Old Codex pro x1 becomes plus; calibrated
  old pro weights become custom unchanged. Never assume Claude Pro equals Codex Pro.
- Account removal requires idle unlocked profiles and no queued affinity. Preserve
  task history/results; delete only validated local profile paths. Report cleanup failures.
- Protocol transports preserve schema, permissions, tools and recursion guards.
  Cancellation on timeout must confirm runner exit before another dispatch.
- Registration UI has name/provider/plan only. The dashboard monitors accounts and
  jobs; it does not create tasks.

Public code, docs, skills and screenshots must be generic. Operator paths and
preferences belong in installed `references/local.md`, outside this repository.
Ignore `.git`, caches and local state, except when deliberately validating them.
