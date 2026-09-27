<p align="center">
  <img src="docs/assets/readme-banner.png" width="100%" alt="CCX: Claude Code + Codex, isolated profiles and weighted capacity." />
</p>

<h1 align="center">CCX</h1>

<p align="center"><strong>Your agents, across your accounts. Capacity that reflects each plan.</strong></p>

<p align="center">
  <a href="#quickstart">Install</a> ·
  <a href="#product-preview">Preview</a> ·
  <a href="#agent-skill">Agent skill</a> ·
  <a href="docs/features/fleet.md">Fleet guide</a> ·
  <a href="https://syntaxlab.com.br">SyntaxLab</a> ·
  <a href="README.pt-BR.md">Português do Brasil</a>
</p>

<p align="center">
  <a href="https://github.com/thalesholleben/ccx/actions/workflows/tests.yml"><img src="https://github.com/thalesholleben/ccx/actions/workflows/tests.yml/badge.svg" alt="Tests" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-7ae4c6?style=flat-square&amp;labelColor=151d22" alt="MIT license" /></a>
  <a href="#quickstart"><img src="https://img.shields.io/badge/desktop-Windows-a0b0b7?style=flat-square&amp;labelColor=151d22" alt="Windows desktop" /></a>
  <a href="#quickstart"><img src="https://img.shields.io/badge/Python-3.12%2B-a0b0b7?style=flat-square&amp;labelColor=151d22&amp;logo=python&amp;logoColor=white" alt="Python 3.12+" /></a>
  <a href="#agent-skill"><img src="https://img.shields.io/badge/skill-Claude%20Code%20%2B%20Codex-7ae4c6?style=flat-square&amp;labelColor=151d22" alt="Skill for Claude Code and Codex" /></a>
</p>

CCX manages a local fleet of Claude Code and Codex accounts. Register a label,
provider and plan, sign in through the official CLI, and let the scheduler choose
an account for each task based on measured limits, plan weights and reserved
capacity. The compact dashboard monitors the fleet; the execution service keeps
working when you close it.

**The agent skill is part of the product.** Bundled with CCX and installable in
Claude Code and Codex, it teaches agents how to inspect limits, respect margins,
choose an account and dispatch work through isolated profiles. It connects the
agent's decision to the CLI and the scheduler. [Install the skill](#agent-skill).

> The desktop interface is currently in Portuguese. This README is also available
> in [Português do Brasil](README.pt-BR.md).

## Product preview

![CCX dashboard with Claude and Codex icons beside aligned account names and plans](docs/assets/fleet-dashboard.png)

*Captured from the real Windows application using synthetic accounts. No real
account data or credentials appear here.*

## Why CCX

A Claude Pro account and a Max 20 account at 50% usage do not represent the same remaining
capacity. CCX selects an account before starting each job, accounts for estimated
cost and reserves capacity across session, weekly and model-specific limits.

- **One account, multiple isolated workers.** Every worker has its own authenticated
  profile. Accounts sharing an identity cannot be registered as independent pools.
- **Compact capacity dashboard.** Register a label, provider and plan; edit the label without changing the
  generated account ID or login. Inspect limits,
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
For a terminal-only setup (replace CELL_ID with the ID printed by registration):

```sh
python ccx-fleet.py cell add account-a claude --plan pro
python ccx-fleet.py cell login CELL_ID
python ccx-fleet.py service start
python ccx-fleet.py status
```

Submit an authorized task from a UTF-8 file:

```sh
python ccx-fleet.py run claude --prompt-file task.md --cwd /path/to/project --request-id review-001 --timeout 900
```

Account creation prints its generated ID. Use that ID for login and `--cell CELL_ID`. Omit it for automatic
selection. To authorize file changes, add `--permission write`. The default is
read-only; Claude's read-only mode does not enable shell commands. Specify
`--model` and `--effort` when required. See the [command reference](skills/ccx/references/commands.md).

## Terminal at a glance

`python ccx.py stats` and `python ccx-fleet.py status` show up to three accounts
per row, grouped by provider, with session and weekly usage only. Bars turn
amber at 70% and red at 90%. Narrow terminals adapt to one or two columns.
Each limit shows its usage and time until reset in hours, such as `50% reset 2.5h`.
`reset n/d` means no reset was reported; `reset pend.` means the reported time has
passed and a new reading is needed. Use `--refresh` to update eligible idle profiles,
`--details` for diagnostics and measurement age, or `--json` for integrations.

## Capacity and execution

| Profile | Session weight | Default weekly weight | Default margin |
| --- | ---: | ---: | ---: |
| Claude Pro | 1 | 1 | 10% |
| Claude Max 5 | 5 | 1 | 10% |
| Claude Max 20 | 20 | 1 | 10% |
| Codex Plus | 1 | 1 | 10% |
| Codex Pro | 5 | 1 | 10% |

These are scheduling profiles, not guaranteed token allowances. Codex Pro x20 is
not offered in this version. Custom weights remain available through the CLI. Weekly weight remains
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

## Agent skill

The bundled [CCX skill](skills/ccx/SKILL.md) is the operating guide for agents.
It instructs Claude Code and Codex to read it **before delegating work**, then
covers commands, internal execution, limits and strategies for choosing an
account. Automatic selection is the default; `--cell CELL_ID` targets a specific
account while preserving profile isolation and capacity checks.

The skill teaches agents to:

- Inspect account health and fresh quota measurements before dispatch.
- Compare weighted headroom across plans, including weekly and model limits.
- Respect margins, reserve estimated cost and use a free authenticated worker.
- Preserve permissions, model, effort and workspace isolation.
- Follow jobs through completion, timeout or cancellation without duplicating work.

Install it for both clients from the repository root:

```sh
python scripts/install-skill.py both
```

Install into Claude Code and Codex's skill directories, then refresh your agent
session. The skill triggers before calling another agent and teaches explicit
account targeting without changing global credentials. Details: [skills](skills/README.md).
Protocol runners can integrate through `fleet.client.execute`: it preserves
model, effort, permissions, tool restrictions, structured output and recursion
guards. A timeout requests cancellation and confirms runner exit before returning.
The optional skill transport uses local installation metadata; see the [integration
guide](skills/README.md). Installing the skill does not intercept native IDE
subagents or change the IDE account.

## Migration and compatibility

Fleet mode is the primary interface. Global rotation commands, hooks and the
Codex bridge are retired. The old installers only accept `-Uninstall`. Stop fleet admission before updating an existing installation. Follow the
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
