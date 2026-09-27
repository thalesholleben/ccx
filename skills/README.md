# CCX agent skill

Teach Claude Code and Codex how to inspect fleet capacity, choose an account and
dispatch an authorized task into an isolated profile. The entry point explains
commands first, then internals, limits and scheduling strategies.

```sh
python scripts/install-skill.py both
# Or: claude-code / codex
```

This installs into `~/.claude/skills/ccx` and `~/.agents/skills/ccx`. Codex also
receives `agents/openai.yaml`. Restart agent sessions to refresh skill discovery.
Use `--force` to update the bundled files. An existing `references/local.md` is
preserved. Only previously recorded package files are pruned on updates;
unknown operator files remain untouched. The installer records the current clone path in `installation.json`;
rerun it if the clone moves. No credentials are copied and no service is enabled.

The skill has an automatic trigger **before invoking another agent**. For
environments with an explicit agent policy, add this short rule to the existing
instructions: "Before invoking an executor, subagent or reviewer, read the CCX
skill. Reading it does not authorize additional delegation."

Native IDE subagents are not automatically routed through CCX. Protocol-specific
runners retain their own permissions, recursion guards and review limits.

The public package contains generic examples only. Machine paths and operator
preferences belong in the installed `references/local.md`, outside this repo.

## Optional protocol transport

`skills/ccx/scripts/transport.py` exposes `enabled()` and `execute(...)` for an
existing runner that explicitly integrates with it. The runner keeps its prompt,
output validation, review rounds and recursion rules. Execution uses
`fleet.client.execute`, with the original model, effort, permissions, tools and
JSON schema. Failures do not fall back to a global account.

After integrating a runner, enable its installed skill transport by creating
`transport.json` beside the installed `SKILL.md`, containing `{"enabled": true}`.
The repository path comes from `installation.json`. These files are local to the
operator. Updating the skill preserves local transport preferences. Installing
the skill alone does not modify any runner or intercept native IDE subagents.
