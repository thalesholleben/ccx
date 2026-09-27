# CCX

![CCX capacity bars](assets/ccx-icon.png)

**A local fleet for Claude Code and Codex agents, with isolated accounts and weighted capacity.**

[![Tests](https://github.com/thalesholleben/ccx/actions/workflows/tests.yml/badge.svg)](https://github.com/thalesholleben/ccx/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/github/license/thalesholleben/ccx)](LICENSE)

[Quickstart](#quickstart) · [Fleet guide](docs/features/fleet.md) · [Agent skill](skills/README.md) · [Migration](docs/runbooks/migration.md) · [Português](README.pt-BR.md)

![Compact fleet dashboard showing synthetic Pro, Max 5 and Max 20 accounts](docs/assets/fleet-dashboard.png)

*Real application screenshot with synthetic accounts. The desktop interface is currently in Portuguese.*

## Why CCX

A Pro account and a Max 20 account at 50% usage do not represent the same remaining
capacity. CCX selects an account before starting each job, accounts for estimated
cost and reserves capacity across session, weekly and model-specific limits.

- **One account, multiple isolated workers.** Every worker has its own authenticated
  profile. Accounts sharing an identity cannot be registered as independent pools.
- **Compact capacity dashboard.** Register a name, provider and plan; inspect limits,
  account health and jobs. Tasks arrive through the CLI and integrations.
- **Independent execution.** Closing the dashboard keeps the service and agents
  running. The Windows tray icon opens or hides the panel.
- **No fixed agent cap.** Admission depends on quotas, free authenticated workers
  and workspace conflicts. One profile runs one agent at a time.
- **Persistent queue and reservations.** SQLite transactions coordinate dispatch.
  Unknown usage blocks admission; ambiguous failures never replay a prompt blindly.
- **An agent skill.** Commands, account selection, margins and operating strategies
  for Claude Code and Codex, with separate optional operator configuration.

## Quickstart

The validated desktop environment is **Windows with Python 3.12+ and Tkinter**.
Install the official Claude Code and/or Codex CLI and make the native executable
available on `PATH`. Runtime uses Python's standard library; no `pip install` is
required. POSIX process support exists, but is not part of the validated desktop
matrix. Provider subscriptions and logins are supplied by the operator.

```sh
git clone https://github.com/thalesholleben/ccx.git
cd ccx
python ccx-fleet.py panel
```

On Windows, double-click `ccx-panel.cmd`. Register an account, then select **Login**
and complete the official provider flow. Each account starts with one worker.
For a terminal-only setup:

```sh
python ccx-fleet.py cell add account-a claude --plan pro
python ccx-fleet.py cell login account-a
python ccx-fleet.py service start
python ccx-fleet.py status
```

Submit an authorized task from a UTF-8 file:

```sh
python ccx-fleet.py run claude --prompt-file task.md --cwd /path/to/project --request-id review-001 --timeout 900
```

Target a particular account with `--cell account-a`. Omit it for automatic
selection. To authorize file changes, add `--permission write`. The default is
read-only; Claude's read-only mode does not enable shell commands. Specify
`--model` and `--effort` when required. See the [command reference](skills/ccx/references/commands.md).

## Capacity and execution

| Profile | Session weight | Default weekly weight | Default margin |
| --- | ---: | ---: | ---: |
| Pro | 1 | 1 | 10% |
| Max 5 | 5 | 1 | 10% |
| Max 20 | 20 | 1 | 10% |

These are scheduling profiles, not guaranteed token allowances. Max profiles
apply to Claude; Codex supports Pro or custom capacity. Weekly weight remains
conservative until calibrated with account evidence. The default task estimate is
15 equivalent x1 points. Weekly/model limits can block an account with session
capacity remaining. Usage collected by CCX can age out when the fleet is idle.

A cell identifies an account/quota pool. Workers hold independent OAuth homes.
The scheduler reserves capacity and assigns a free worker; a separate runner
launches the official CLI. Two jobs cannot share the same profile. Concurrent
writes require separate worktrees or non-overlapping directories.

A waiting-client timeout does **not** cancel the job. Keep its ID and use `wait`
or `cancel`. Closing the panel does not stop execution. Stopping the service
stops new dispatches; existing runners finish. Restarting Windows is different:
there is no automatic logon installation or live-job migration between accounts.

## Agent installation

```sh
python scripts/install-skill.py both
```

Install into Claude Code and Codex's skill directories, then refresh your agent
session. The skill triggers before calling another agent and teaches explicit
account targeting without changing global credentials. Details: [skills](skills/README.md).
Native IDE subagents and custom review runners need an explicit integration;
installing a skill alone does not route their processes through CCX.

## Migration and compatibility

Fleet mode is the primary interface. Old global rotation commands, hooks and
Codex bridge are compatibility tools, not the fleet scheduler. Stop fleet admission before updating an existing installation. Follow the
[migration runbook](docs/runbooks/migration.md) to stop old automation without
interrupting live sessions. Legacy modules remain because the fleet reuses their
parsers and file/lock primitives; deleting them would break the new runtime.

Never copy global tokens into worker profiles. Complete a separate official login
for every worker and validate coexistence before increasing parallelism.

## Development

```sh
python scripts/check.py
```

This runs the offline regression suites and skill-installer test using synthetic
credentials and temporary state. On an interactive Windows desktop, also run
`python tests/tray-smoke.py` and `python tests/panel-smoke.py`. Pillow is optional
for screenshot capture and rebuilding icons, not for runtime.

Read [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). Source is licensed under [MIT](LICENSE).
