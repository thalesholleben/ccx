# Contributing

Describe the observable problem, expected behavior and platform in an issue or PR.
For a fix, include the smallest reproducible example using synthetic accounts.
Do not include tokens, real account identifiers, prompts, OAuth logs or state dumps.

Use Python 3.12+; runtime uses the standard library. Read `AGENTS.md` for the map
and concurrency invariants. Run `python scripts/check.py` before submitting.
For desktop changes, run both smoke tests on an interactive Windows desktop.
No tests should access real provider credentials or send model prompts.

Keep changes scoped. Account identity, locking, process ownership, cancellation,
quota parsing and schema migrations need observable regression tests. Pure visual
changes need a screenshot with synthetic data. State which platforms were tested.

The default branch is `master`. Open a pull request with the problem, resulting
behavior, validation and any remaining limitation. Contributions use the MIT license.
