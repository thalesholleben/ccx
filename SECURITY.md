# Security

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/thalesholleben/ccx/security/advisories/new).
If private reporting is unavailable, open an issue requesting a private contact
without exploit details or account data. Do not post tokens, prompts, usage dumps
or authentication files. The current `master` branch receives fixes; no separate
support promise is made for older snapshots.

## Trust boundaries

CCX runs locally with the operator's OS permissions. It is not a multi-tenant
service or a filesystem sandbox. The official CLIs enforce their own permission
modes. Read-only Claude jobs restrict tools; write jobs are explicitly authorized.
A separate OAuth home isolates credentials, not arbitrary filesystem access.

Fleet state lives in `~/.ccx/fleet`: the SQLite database, profiles and job outputs
are private. The OS user can read them; there is no at-rest encryption layer.
Never commit, publish or share this directory. Legacy stores under `~/.ccx`
also contain real OAuth credentials and must remain private.

Every worker requires an official login. CCX does not import global grants.
Refresh, login and job execution use exclusive profile locks; writes preserve
unrelated credential fields and replace files atomically. Unknown/stale usage
blocks dispatch. HTTP 429 does not mean a free or exhausted account.

The panel uses local Tk widgets, not an HTTP listener. Fleet service/runners use
local files and SQLite. Provider adapters read limits and refresh tokens at the
provider endpoints; model work goes through the official CLIs. CCX adds no usage
telemetry or third-party model proxy. Provider CLI networking is controlled by
those tools and their configuration.

Prompt files and result files are private. Public status avoids prompts/tokens,
but account names and local paths may still be sensitive. Use synthetic accounts
for screenshots and reports. Errors and logs must be sanitized.

## Legacy compatibility

Old rotation tools can modify the client's global credentials. Their optional
Codex bridge listens on authenticated loopback and is not part of fleet mode.
See the migration runbook before uninstalling it. Never stop a live conversation
or remove a live bridge registration to force an account change.

The offline test suite validates synthetic state, parsing, locking and process
lifecycle. It does not establish provider terms, subscription entitlement or
coexistence of multiple real OAuth grants; validate those with your own accounts.
