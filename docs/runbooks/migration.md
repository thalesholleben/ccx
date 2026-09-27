# Migrating from global rotation to the fleet

Fleet mode is the primary interface. Global rotation and the optional Codex
bridge do not distribute independent jobs across accounts.

## Updating an existing fleet installation

Before updating fleet code, stop admission with `python ccx-fleet.py service stop`
and wait for `service status` to report that the service is no longer alive.
Existing runners may finish; do not kill them. Update the code, open the panel
and start the service again. Schema 3 refuses migration while an older service
is alive, because that scheduler treats the retired zero cap as zero free slots.
The stop command remains available without migrating the old database.
Old custom plan names become `custom`, preserving their existing weights.

## Moving from global rotation

1. Register name, provider and plan in the panel. Every account starts with one
   worker. Complete its official login; do not copy existing tokens.
2. Run `python ccx-fleet.py status` and confirm identity, current quota and profile
   health. Submit one small authorized read-only canary per provider/account.
3. For parallelism within an account, add another worker and perform another
   independent login. Validate that the first worker and any existing interactive
   session remain authenticated. One authenticated profile runs one job at a time.
4. Point ordinary integrations at `ccx-fleet.py run` or `submit`. Account affinity
   is `--cell ID`. Preserve model, effort, permissions and recursion guards.
   Protocol runners must integrate explicitly; a skill is not a process wrapper.
5. Disable old automation. On Windows, `install-ccx-monitor.ps1 -Uninstall` removes
   the old scheduled Claude monitor and stops only its verified process. Remove
   only the old CCX rotation hook from Claude settings, preserving other hooks.
   Remove old `auto` tasks from workspace/editor configuration.
6. If the Codex bridge was installed, run `install-ccx-codex-bridge.ps1 -Uninstall`.
   It restores the relevant editor setting and preserves unrelated settings.
   A live launcher may remain loaded until the next editor restart. Finish live
   conversations first; never kill their app-server to complete migration.
7. Keep old account stores private until canaries and integrations are confirmed.
   They are not needed by fleet jobs, but deleting them is irreversible and does
   not revoke provider tokens. Do not delete shared Python modules: the fleet
   still imports compatibility helpers from `ccx.py` and `ccx_codex.py`.

No automatic token import, retry of ambiguous work, mid-job account handoff or
editor-session migration occurs. Existing tasks and accounts survive the schema
3 update; only obsolete numeric agent caps are retired. Closing the dashboard
preserves the service and jobs. Automatic logon startup is not installed.

Completion means real account logins, successful canaries and every intended
executor explicitly routed through the fleet. If any of those are pending,
report them instead of calling migration complete.

## Account labels (schema 4)

Schema 4 adds an editable `display_name`, initially copied from the existing ID.
It never renames cells, workers, jobs, reservations, profile paths or credentials.
New registrations in the panel and CLI generate an opaque unique ID. The user
chooses a label; `cell configure CELL_ID --name "New label"` edits it. The panel
exposes the same operation in **Editar conta**. Renaming alone keeps custom weights.
Integration commands continue using the immutable ID printed at registration or
shown in `status`. Names must be printable, nonempty and at most 48 characters;
duplicate names are refused. Existing account IDs remain valid after the update.
