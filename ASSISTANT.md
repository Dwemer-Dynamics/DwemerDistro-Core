# ddistro command and JSON contract (schema version 1)

`bin/ddistro` is one stable, stdlib-only Python command for people, the launcher, and a
future assistant to read DwemerDistro state and run the existing server update. It adds
no daemon, service, or dependency. `update.sh` installs it as root-owned
`/usr/local/bin/ddistro` mode `0755`, so every user can read and run it.

```bash
ddistro version | capabilities | products
ddistro status [product]                        # summary, any user
ddistro logs <product|apache|postgres> [--log NAME] [--lines N] [--query TEXT] [--since UTC]   # any user who can read the file
ddistro server status [product]                 # full validated ddistro_server records
ddistro server update <product> --branch <branch> [--force]       # root only
ddistro changes prepare --product herika [--path RELATIVE ...]    # root only, see File changes
ddistro changes diff|stage|status|rollback TASK | changes list    # root only
ddistro changes apply TASK --manifest-sha256 HASH                 # root only
ddistro agent status | agent probe                                # probe root only, see Assistant agent
ddistro agent home-config | agent home-config-repair --sha256 HASH   # root only, see Assistant agent
ddistro agent serve --task TASK                                   # root only, raw JSONL
ddistro models herika                                             # root only, see Model assignments
ddistro inventory                                                 # any user, see Inventory
ddistro service restart <service>                                 # root only, see Service restart
ddistro service status <service> | service list                   # read-only, see Service restart
```

Products are the five `ddistro_server` products: `herika` (aliases `chim`, `skyrim`),
`lorkhan`, `stobe` (`kenshi`), `dialectic` (`fnv`, `ttw`), and `reign`. Aliases are exact,
lowercase matches. `--json` and `--request-id ID` may appear anywhere.

## JSON output

With `--json`, stdout holds exactly one JSON document and nothing else; helper output is
captured and never copied to stdout. Without it, results are human-readable text on
stdout and failures are `[FAIL] ...` on stderr.

```json
{"schema_version": 1, "ok": true, "command": "status", "request_id": "...",
 "tool": {"name": "ddistro", "version": "1.0.0", "source": "DwemerDistro-Core"},
 "data": {...}, "error": null, "warnings": []}
```

- `request_id` echoes `--request-id` (1-64 of `A-Za-z0-9._:-`) or is a fresh UUID.
- Mutations (`server update`) add `operation_id`, a fresh UUID, once arguments and root
  are accepted.
- On failure `ok` is false, `data` is null, and `error` is
  `{"code", "exit_code", "message", "details"}`. The process exit code equals `exit_code`.
- `tool.version` is the CLI contract version, not a product release version.

| Code | Exit | Meaning |
| --- | --- | --- |
| `failed`, `internal_error`, `helper_failed`, `helper_protocol` | 1 | General, unexpected, helper failure, or invalid helper status |
| `partial`, `conflict`, `rollback` | 10, 11, 12 | Some files written; live files changed (nothing written); rollback refused |
| `invalid_argument` | 64 | Unknown command, product, option, branch, or value |
| `refused`, `limit_reached` | 65 | Memory text refused (secret shape, unverified assistant claim); memory full |
| `not_found` | 66 | Update target is not installed; no memory note with that id |
| `unsupported`, `helper_unavailable`, `disabled` | 69 | `ddistro_server` missing or not a trusted root-owned file; memory setting off |
| `busy` | 75 | Another server operation, maintenance, or change holds a lock |
| `permission_denied`, `unsafe_path` | 77 | Not root for an update, unreadable log, or redirected/non-regular log |
| `update_blocked_tracked` | 79 | Tracked edits block the update; `--force` would discard them |
| `update_blocked_untracked` | 80 | Untracked files are in the way and must be moved |
| `timeout`, `cancelled` | 124, 130 | Helper stopped after timeout, or the caller cancelled |

`capabilities` lists only implemented commands, aliases, log names, limits, and error
codes; `features.file_changes` and `locks` are `true` and `chat` is `false`. `features.agent`
is `true` only when the trusted `ddistro_agent` helper is installed; it never means the
assistant is ready. Readiness comes only from `agent status` (`installed` and `probe_passed`).
`features.models` and `capabilities.models` describe `models herika` (roles and limits).
`features.model_settings` and `capabilities.model_settings` describe the root model-assignment
change commands (role field, preview seconds, compare-and-swap, exact undo, receipt limits), and
`features.update_check` and `capabilities.update_check` the local, network-free `update-check`.
`features.inventory` and `capabilities.inventory` describe `inventory` (scope, the products
with an extension folder, and limits); `features.component_catalog` and
`capabilities.inventory.component_catalog` declare catalog support, file, schema, limits, and
states from code. `capabilities.inventory.components` still lists the legacy component ids
(startup entries, from the catalog file's metadata only; no component path is checked), with
`components_source.available`/`reason`; an unusable catalog gives `[]` and its reason. `features.memory` and
`capabilities.memory` describe `memory` (scope, kinds, defaults, limits) from constants only;
`capabilities` never opens or creates the memory database. `features.thread_lifecycle` is
`true`: `agent serve` forwards `thread/archive`, `thread/unarchive`, and `thread/delete` with
`threadId` only, and only for one of the serving task's own threads; delete never removes the
task's change receipts or backups (see "Thread archive and delete" in `ASSISTANT_RUNTIME.md`).
It is a static fact, like the others, and adds no command.

`features.explore` is `true` when the trusted `ddistro_access` library loads. `features.exec` is
`true` when the trusted `ddistro_exec` helper is installed as well. Each feature lists its
Assistant tool (`ddistro_explore`, `ddistro_exec`) under `assistant_tools.tools` only while it is
true. `features.service_operations` and `capabilities.service_operations` describe the typed
service operations (below) from code.

`features.assistant_foundations` is `true` only when `ddistro_agent`, `ddistro_access`,
`ddistro_exec` and `ddistro_changes` are all trusted and `agent status` reports the runtime
installed with a passing probe whose attestation records the optional managed exec canary
(`managed_exec_isolation: true`) and sha256 digests equal to the installed `ddistro_access`,
`ddistro_changes`, `ddistro_exec` and `ddistro` files. Reinstalling any of them makes it false
until the probe runs again. When it is false, `capabilities.assistant_foundations.reason`
says why (for example `exec_helper_untrusted`, `probe_required` or
`agent_status_helper_protocol`, `managed_exec_unverified` or `<helper>_digest_stale`). Only while it is true does
`ddistro_action.capability_operations` list `change_files` (`propose_file_change`,
`propose_file_undo`) and `service_control` (`propose_service`). `ddistro_action.operations` stays
exactly `["propose_restart"]`. A false flag leaves every other capability readable, and the
Launcher stays on its earlier tool revision. To set the flag, `capabilities` runs `agent status`
once. It never starts a probe, a session or a model.

`features.live_run` is `true` only while `assistant_foundations` is, `/usr/local/bin/ddistro_run` is a trusted
root-owned file, and the passing attestation records `live_run_isolation: true` with a `managed_helpers.run`
digest equal to that file now. `capabilities.live_run` gives `available`, `reason` (`foundations_unavailable`,
`run_helper_missing`, `run_helper_untrusted`, `attestation_invalid`, `live_run_unverified`, `run_digest_stale`)
and static facts: interpreters, default account, network modes, masked paths, approval decisions, grant scope
and limits. `live_run.limits` holds the same values as the broker's `run_limits()` (the ones it enforces, states in
the `ddistro_run` description and returns from `list`). Only while it is true does `assistant_tools.tools` list `ddistro_run`. It runs approved commands on
the LIVE distro, not in a sandbox; root is full trust. `live_run.automatic` describes the optional `live_run_auto`
client capability: runs the model declares routine start without a prompt, while destructive, undeclared and
pattern-flagged runs always need the user's confirmation of the exact request and are never covered by a grant. See
"ddistro_run live commands" and "Automatic routine runs" in ASSISTANT_RUNTIME.md.
There is no `ddistro` command for it: the broker starts the `ddistro_run` supervisor itself.

## Status and update

Status always comes from `/usr/local/bin/ddistro_server status <product|all> --json`.
The CLI checks that the helper and its folder are root-owned regular files only root can
change, runs it with an explicit argument list (no shell), a minimal environment, no
stdin, and bounded output, then validates the document strictly: exact keys, types, and
product order. `state`, `repository_state`, and `database_present` are passed through
exactly as the helper reports them; the CLI adds no fields the helper does not provide.
Any other output is `helper_protocol`; nothing is scraped from text.

`server update` requires `--branch` from the product's `branches` list and an effective
root user. The CLI never calls `sudo` and never prompts. It reads status first, refuses
`not-installed` with `not_found`, then runs `ddistro_server update`. It never adds
`--force` or retries. Exit 79/80 are preserved; the helper's `DDISTRO_UPDATE_BLOCKED`
line is validated and returned as `error.details.blocker` (or `null` if invalid), with
sanitized stdout/stderr tails. Progress text is only shown (on stderr) in human mode.
Timeout or cancellation stops the helper's whole process group (TERM, then KILL).

`update-check [product]` is read-only and local only: it never fetches, pulls, updates,
forces, discards, or cleans anything, so it cannot know whether an update exists or would touch
local paths (`network_queried`, `remote_changes_checked` are `false`, `conflicts` is `unknown`).
A clean product is `no_local_blockers`, never ready or up to date; the others are `blocked`
(tracked changes or an unfinished merge/rebase), `unknown`, or `not_installed`. Only the known
product root is inspected, as a plain `.git` directory (a linked worktree's `commondir` is
`unexpected_repository_layout`); git gets that `.git` and root explicitly (`--git-dir`,
`--work-tree`), so a `core.worktree` redirect in the repository config is not followed. System
and global config, hooks, fsmonitor, pager, and attributes files are off. Before status, a
`git config --name-only --get-regexp ^filter\.` scan (the repository config with its includes
and conditional includes) refuses any filter driver as `git_filter_configured`; only key names
are printed, never the configured command. Any other exit or output is `git_config_failed`.
Status output past 4 MiB is cut, and then the result is `unknown` with `status_truncated` unless
a tracked change already makes it `blocked`; at most 20 paths of each kind are listed.

## Logs

Only allowlisted files under each target's declared folder are read (see `products` or
`capabilities.log_targets`); there is no path argument and no directory scan beyond the
PostgreSQL folder's `postgresql-<version>-main.log` name match. Symlinked or redirected
folders, symlinked files, and non-regular files are refused. The tail is bounded to 400
lines and 256 KiB, with each line clipped to 4096 characters. URL credentials and query
strings, authorization headers, keys, tokens, and passwords are redacted, and control
characters are replaced. A missing file is `ok` with `available: false` and
`reason: "missing"`; an empty file is `available: true` with no lines. Unreadable files
are `permission_denied`; `read_as` reports the user, because root access does not prove
that `dwemer` can read a file. Reads never write product data.

`--query TEXT` (1-200 characters, one line) and `--since YYYY-MM-DDTHH:MM:SSZ` (UTC, exactly that
form) filter the same bounded tail; nothing before it is read, so a filter is never a full
historical scan. The query is literal text, not a pattern, matched case-insensitively against
the redacted line, so redacted secrets cannot be matched. A line's time comes only from a fixed
prefix (ISO 8601 with an optional zone, also inside `[...]` or a leading JSON `time`/`timestamp`/`ts`
field; the Apache error-log form; the common-log `[dd/Mon/yyyy:hh:mm:ss +zone]` form); times
without a zone are read in the distro's local time (`naive_timestamps: "distro_local_time"` and
`local_utc_offset`). A line without a time belongs to the dated line above it; with `--since`,
undated lines before the first dated one are excluded and counted in `excluded_undated`. `--lines`
then keeps the newest matches. The result adds `filter: {query, since, scanned_lines, dated_lines,
undated_lines, excluded_undated, matched, returned, whole_file, since_covered, tail_bytes,
complete, ...}`. `complete` is true only when nothing relevant can be missing: the tail is the
whole file, or it starts at a dated line before `since`; no line was excluded for an unknown or
invalid time (bad date or zone offset beyond 14:59); times never go backwards (`out_of_order`); and no
match was left out by `--lines` or the byte bound. A plain tail (no filter) is unchanged.

## File changes

`changes` commands run the root-only `/usr/local/bin/ddistro_changes` (installed root-owned
`0755`, trust-checked like `ddistro_server`). Its stdout is one JSON document
`{"schema_version": 1, "ok", "data", "error"}` whose error code matches its exit code; the
CLI validates it and returns `data` or `error` unchanged. Only `herika` (`chim`, `skyrim`)
at `/var/www/html/HerikaServer` is editable; other products are `unsupported`.

- `prepare` returns `{task_id, workspace, product, files}`. Each `--path` is an existing
  text file or a new file in an existing folder. `.git`, logs, caches, uploads, voices,
  `conf/conf*.php`, and likely credential files are refused even when named. No paths gives a
  diagnostic-only workspace with sanitized status and recent log lines in `.ddistro-context/`.
- Task state is `/var/lib/dwemerdistro/assistant/tasks/<uuid>/` (root `0700`, or
  `root:ddagent 0710` when the workspace is `ddagent`'s so the assistant runtime can reach
  `work/` without listing or reading anything else; files `0600`)
  with `task.json` (`schema_version`, `task_id`, `product`, `workspace`, baseline hashes and
  metadata). The workspace is `<task>/work`, owned by `ddagent` if it exists, otherwise
  `dwemer`; a root broker can bind it without exposing the task metadata.
- `diff` previews and `stage` freezes the edited bytes into root-only storage; both return
  `{task_id, manifest_sha256, diff, files}`. Removing a selected file means delete; creating a
  selected missing file means create. Limits: 1 MiB per file, 200 files, 8 MiB per batch,
  UTF-8 without NUL, no links, no mode changes. Staging checks JSON and runs `php -n -l` on
  the frozen bytes as `nobody`; repository code is never run as root. A check that could not
  run (for example `php` missing) is a top-level `warnings` entry, never a pass; re-staging an
  already staged batch revalidates it and fails with reason `validation` if it no longer passes.
- `diff` is bounded to `limits.change_diff_bytes` of JSON-encoded text. `diff_truncated: true`
  marks an incomplete preview that must not be approved; `stage` refuses such a batch with
  reason `diff_too_large` (64), so a staged hash always comes with its complete diff and
  `files` lists every manifest entry that apply will write.
- `apply` needs the exact staged hash. Under the exclusive maintenance lock it rechecks every
  live file's hash, owner, group, and mode first; any change is `conflict` (11) with nothing
  written. Each file is journaled and replaced atomically in its folder, keeping its owner,
  group, and mode (new files take the folder's owner and group, `0644`/`0664`). Backups stay
  in the task. The batch is not atomic: a failure is `partial` (10) with per-file states.
  Re-running apply returns the stored receipt and writes nothing.
- `rollback` restores only files the receipt shows as written, after checking they still
  hold the applied bytes and metadata; otherwise `rollback` (12) and nothing is written.
- `status` and `list` report `prepared`, `staged`, `applied`, `partial`, `failed`,
  `no_changes`, `interrupted`, `rolled_back`, and similar states from immutable receipts and
  journals. An interrupted apply or rollback is never replayed or repaired automatically.

Schema 2 tasks can hold files from several roots and are named with `--task`:

- `changes checkout --task T --root R --path P [--path P2] [--root R2 --path P3]` copies the named
  files into the task's scratch folder (`work/<root>/<path>`). It never writes to a live root. A
  root is usable only when `access roots` lists it with `change: true`. Config files, secrets,
  files with inline credentials and non-text file types are refused.
- `diff`, `stage` and `apply` work as described above across all of the task's roots. Each file is
  written back to its own root.
- `changes rollback-preview --task T` returns the exact undo of the last applied round, with its
  `preview_sha256` (or `nothing_to_undo`). `changes undo --task T --preview-sha256 H` writes that
  undo under the same checks as `apply`. If a live file changed after the apply, the result is
  `conflict` and nothing is written.

`/run/lock/dwemerdistro-maintenance.lock` (root, `0644`) is one common lock: `update.sh`,
`update_gws`, `ddistro_server` mutations, and `ddistro_doctor --repair` hold it shared (so
nested updates work), while `changes apply` and `rollback` need it exclusively and fail with
`busy` otherwise. Root creates it if missing and never replaces it; unprivileged updaters
only open it read-only and stop with a message if it is missing.

## Assistant agent

`agent` commands run the root-owned `/usr/local/bin/ddistro_agent` (installed `0755` by
`update.sh` and trust-checked like the other helpers). See ASSISTANT_RUNTIME.md for the
runtime, sandbox, and probe. `update.sh` never downloads the runtime or installs packages;
that is an explicit `install_assistant_runtime` run as root.

- `agent status` (any user) returns `{installed, probe_passed, provider: "codex", version,
  reason}` exactly as the helper reports it. `reason` is a token such as `ready`,
  `probe_required`, `probe_stale`, `not_installed`, or `bwrap_unsupported`. A runtime that
  is not ready is still `ok`; only an unusable helper is an error.
- `agent probe` (root, adds `operation_id`) runs the real isolation probe and returns
  `{passed, checks: [{name, passed}], diagnostics, ...status fields}`, failed checks first.
  A failed probe is `ok` with `passed: false`, and the helper has removed the attestation.
  A running session gives `busy` (75) and leaves the attestation unchanged. `diagnostics`
  holds per-step exit status and bounded, redacted stderr lines.
- `agent serve --task TASK` (root) accepts exactly those arguments; `--json` and
  `--request-id` are refused. After checking the helper, `ddistro` replaces itself with
  `ddistro_agent serve --task TASK` (same process, a minimal environment, working directory
  `/`), so stdin and stdout are the helper's raw Codex App Server JSONL with no envelope or
  progress text. Refusals by `ddistro` itself are one `{"error", "reason"}` line on stderr
  with exit `64` (bad arguments) or `3` (`requires_root`, `helper_unavailable`); the
  helper's own refusals and exit codes are described in ASSISTANT_RUNTIME.md.
- `agent home-config` (root) is the dry run for the one repair of serve's
  `account_home_unmanaged_config` refusal: `{state, reason, file: "config.toml", sha256, size,
  receipt: null}` with `state` `clean`, `repairable` (with the proposal's `sha256`), or `refused`
  with a reason token (for example `config_unknown_content`, `config_symlink`,
  `config_wrong_owner`, `other_unmanaged_file`). It changes nothing.
- `agent home-config-repair --sha256 HASH` (root, adds `operation_id`) quarantines that file only
  when it is still exactly the proposal; it returns `state: "repaired"` and `receipt: {id, path}`.
  A held serve lock is `busy` (75); anything else not repaired is `refused` (65) with
  `details.reason` (`proposal_changed`, `agent_running`, `quarantine_full`, ...). See
  "Account-home config repair" in ASSISTANT_RUNTIME.md.

## Model assignments

`models herika` (aliases `chim`, `skyrim`; other products are `unsupported`, 69) reports the
CHIM connector assigned to each role of each configured profile. These are configured profile
assignments, not necessarily the profile an NPC is using.

```json
{"product": "herika", "available": true, "scope": "configured_profiles",
 "captured_at": "2026-10-19T12:00:00Z", "truncated": false, "reason": "",
 "assignments": [{"profile_id": 1, "profile": "Default", "role": "standard", "connector_id": 3,
                  "connector": "OpenRouter", "model": "...", "provider": "...", "driver": "..."}]}
```

- Roles are `standard`, `fast`, `powerful`, `experimental`, `formatter`, `fallback`, and
  `diary` (`core_profiles.llm_primary_id` ... `llm_quaternary_id`, `llm_formatter_id`,
  `llm_fallback_id`, `diary_connector_id`), ordered by profile id then role. A role whose
  field is absent from the schema is skipped; an unset or dangling connector id gives no row.
- The read runs as `/usr/sbin/runuser -u postgres -- /usr/bin/psql -X -A -t -q --no-password`
  over the local socket `/var/run/postgresql`, port `DDISTRO_POSTGRES_PORT` (digits) or 5432,
  database `dwemer`, in one read-only transaction with connect, statement, and lock timeouts
  and a minimal environment. Both binaries are trust-checked (`psql` may be one root-owned
  link hop to `pg_wrapper`). There are no SQL, schema, or database arguments and no PHP.
- Only fixed SQL runs: a names-only check of `information_schema.columns` for the known
  `public` columns, then `public.core_profiles` `id`, `label`, and the role fields joined to
  `public.core_llm_connector` `id`, `label`, `model`, `provider`, `driver`. URLs, metadata, API
  keys and badges, prompts, and NPC data are never selected.
- At most 200 assignments (`truncated: true` when there are more); each string is clipped,
  redacted like log lines (URL credentials and queries, keys, tokens), made single-line, and
  bounded to 256 characters. A missing label or model is `""`.
- When the read cannot be made the result is still `ok` with `available: false`, no
  assignments, and a `reason` token: `requires_root`, `helper_unavailable`,
  `database_unavailable`, `schema_missing`, `query_failed`, `timeout`, or
  `unexpected_output`. `psql` stderr and SQL are never returned, and no model is guessed.

`models options|preview|apply|rollback-preview|rollback herika` (root only; `ddistro` never
elevates) change one thing: which existing `core_llm_connector` one role of one
`core_profiles` row uses. Connectors, keys, URLs, other fields, and other profiles are never
written, and no connector is created.

- `options` lists profiles with their role ids and connectors (at most 200 each, flagged in
  `truncated`). `preview --profile ID --role ROLE --connector ID [--task T --thread TH]` reads the
  row and connector, refuses `no_change`, and saves a receipt bound to that task and thread with
  both row versions and a `plan_sha256`; it expires after 120 seconds.
- `apply --receipt ID --plan-sha256 HASH` runs only that unexpired, unused preview, under the
  maintenance lock, as one compare-and-swap on the profile row's version and value and the
  connector's version (`conflict` when either changed). The prestate is saved before the write;
  a fresh read then gives `verified`, `unverified`, `conflict`, `not_executed`, or `unknown`.
  `verified` means only that the database holds the new assignment: no provider, model, or game
  reply is tested.
- `rollback-preview` prepares the exact undo of an `applied` receipt (120 s, its own
  `plan_sha256`): `--receipt ID`, or `--task T --thread TH` for that thread's latest change. The
  latest change is the receipt with the highest write sequence, a number the store assigns to
  each apply or undo under its lock just before the write; preview time and receipt ids are never
  used as order. If that latest write's outcome is unknown, or the thread's receipts include one
  saved before sequences existed, or two share a sequence, nothing is prepared (`outcome_unknown`,
  `history_ambiguous`); an older change is never picked instead. The undo is refused as
  `changed_since` when the profile changed after the apply, and `original_missing` when the
  previous connector is gone. `rollback --receipt ID --plan-sha256 HASH` runs it like `apply`.
- Receipts are root `0600` single-link files in a root `0700` directory, read without following
  links and replaced atomically under a lock file whose identity is checked after locking. Any
  receipt that fails validation makes the store `receipts_untrusted`. Unused previews are dropped
  after expiry, receipts whose write is not in progress after 30 days, and the oldest past 200; a
  receipt whose write may be in progress or whose outcome is unknown is never dropped for room
  (`receipts_full`).

## Inventory

`inventory` is one fresh, read-only, presence-only snapshot of the whole distro for the
launcher's assistant. It runs no repository, plugin, or component code, reads no extension
or component code, configuration, or credential contents itself, and follows no link. The
server and custom mod rows come from the trusted `ddistro_server` and `ddistro_custom_mod`
helpers, which consult their own records (version, catalog presence) internally; only an
allowlisted status and presence projection of their output is kept. Presence is all it
reports, never health: an extension that is present may be disabled or broken, and a
component whose startup marker is present may not be running.

```json
{"captured_at": "2026-10-19T12:00:00Z", "scope": "presence_only", "read_as": "root", "complete": true,
 "servers": {"available": true, "reason": "", "items": [{"product": "herika", "state": "installed",
             "repository_state": "managed", "database_present": true, "branch": "aiagent", "version": "..."}]},
 "extensions": {"items": [{"product": "herika", "root": "ext", "state": "listed", "total": 2, "truncated": false,
                           "skipped": 0, "entries": [{"name": "example_mod", "kind": "dir"}]}],
                "no_known_root": ["lorkhan", "stobe", "reign"]},
 "components": {"basis": "startup_markers", "items": [{"id": "xtts", "name": "CHIM XTTS", "role": "tts",
                                                        "state": "present"}]},
 "catalog": {"available": true, "reason": "", "schema_version": 1, "catalog_version": "2026.10.20-1",
             "sha256": "...", "basis": "installed_markers",
             "items": [{"id": "pockettts", "aliases": ["pocket_tts"], "name": "Pocket-TTS (CPU / Python)",
                        "role": "tts", "listed": true, "installed": "present", "installed_reason": "",
                        "startup": "present", "startup_reason": "", "checks": ["presence", "port", "http"]}]},
 "custom_mods": {"available": true, "reason": "", "truncated": false,
                 "items": [{"id": "example-server", "name": "...", "state": "ready", "branch": "main", "commit": "..."}]}}
```

- `servers` comes from the same validated `ddistro_server status all --json` as `status`
  (bounded to 20 seconds here), reduced to product, state, repository state, database
  presence (`null` when unknown), branch, and version; branch and version are redacted and
  clipped to 128 characters. Database presence is the helper's own catalog check; no
  product table is read.
- `extensions` lists the names directly inside `ext/` of HerikaServer and DialecticServer,
  the products whose repositories load add-ons from that folder; `no_known_root` names the
  others. Each folder is opened from `/` one component at a time with `O_NOFOLLOW`, each
  step anchored to the folder already opened; when a step fails, a no-follow stat of that
  name under the same folder decides the state, so any link on the way (the product root,
  `ext/`, or a parent) gives `redirected` and nothing is listed. `state` is `listed`,
  `absent` (no `ext/`), `product_absent`, `redirected`, `not_directory` (a non-folder where a
  folder should be), or `unreadable` (including a name that changed while being checked).
  `kind` is `dir`, `file`, `link` (never followed), or `other`. Hidden names, names outside
  `[A-Za-z0-9][A-Za-z0-9 ._+()@,=~-]{0,127}`, and names the log redaction would change are
  counted in `skipped`, not listed; any skipped name makes `complete` false, because those
  names are omitted. At most 200 names are returned, sorted; at most 4096
  entries are examined, and `truncated` marks either limit (`total` is then a lower bound).
- `components` checks the startup markers `start_env` and `update_gws` use (for example
  `/home/dwemer/xtts-api-server/start.sh`, the Dwemer Dashboard's `.git`), without
  following links: `present`, `absent`, `redirected` (the marker or any folder above it is a
  link), or `unreadable`. Present means startup
  would try the component, not that it runs or is healthy. Paths are never returned. Its rows
  are the catalog entries that declare startup markers, under their older ids (the first
  alias, such as `pocket_tts` or `chim_mcp`), so older launchers read it unchanged.
- `catalog` is every component in the component catalog (below), under the launcher's ids:
  `{"available", "reason", "schema_version", "catalog_version", "sha256", "basis":
  "installed_markers", "items": [{"id", "aliases", "name", "role", "listed", "installed",
  "installed_reason", "startup", "startup_reason", "checks"}]}`. `installed` uses the same
  rules as the launcher's Install Components window and is `present`, `absent`, or `unknown`
  with a reason (`unreadable`, `unsafe`, `unsafe_redirect`, `redirect_limit`, `scan_limit`).
  `startup` is the marker state above (`present`, `absent`, `unknown`, or `n/a` when the entry
  declares no markers). Installed is not running: neither says the component runs or works.
  `checks` comes from code only: always `presence`, plus `port` and `http` when `health`
  has a fixed local check for it. Components are not tool targets: there are no component
  logs, restarts, configuration, or edits through the assistant. Paths and the rules
  themselves are never returned. When the catalog is missing or invalid, `available` is false
  with `reason` `catalog_missing`, `catalog_unreadable`, `catalog_unsafe`, or
  `catalog_invalid`, both `catalog.items` and `components.items` are empty, and `complete` is
  false; there is no built-in fallback list.
- `custom_mods` runs the trusted `/usr/local/bin/ddistro_custom_mod status --json` (no
  `--check-health`, no network, 10 seconds) and keeps only `id`, `name`, `state`, `branch`,
  and a 40-hex `commit` for at most 100 mods; repository, URLs, folders, databases, and
  messages are dropped.
- A section that cannot be read is still `ok`: `available: false` with a `reason` token
  (`helper_unavailable`, `helper_failed`, `helper_protocol`, `timeout`, or `failed`). `complete`
  is true only when every section was read in full: nothing unavailable, truncated, or
  skipped, and no folder or marker `redirected`, `not_directory`, or `unreadable`, the catalog
  available, and no catalog component `unknown`; cancellation stops the helper and the
  whole command is `cancelled`. Run as a user other than root, folders that user cannot read
  are `unreadable` and database presence may be `null`; the launcher runs it as root.

### Component catalog

`/etc/dwemerdistro-components.json` (from `etc/` in this repository, installed by `update.sh`
as a root-owned 0644 file) lists each component's identity and how to tell it is installed.
It is data, not code: it cannot name a command, expression, environment value, port, log, or
action, and nothing in it is run. Install commands, ports, and health checks stay in code. A
new entry appears in `inventory` and `health` (all) without any code or prompt change, but
gets presence only until code adds a check for it.

- The file must be a regular file of at most 64 KiB, opened from `/` without following a
  link; it, `/`, and `/etc` must be root-owned and not group- or other-writable. There is no
  option or environment variable to read another file.
- `{"schema_version": 1, "catalog_version", "components": [...]}`, 1 to 48 entries. Each entry
  has exactly `id`, `aliases` (0 to 4), `name`, `role`, `listed`, `installed`, and optionally
  `startup`; unknown keys, duplicate keys, and an id or alias used twice make the whole file
  invalid. Ids and aliases match `^[a-z][a-z0-9_]{1,31}$`. `role` is one of `llm`,
  `embeddings`, `tts`, `stt`, `mcp`, `dashboard`, `gpu_runtime`. `name` is a display label
  only (1 to 64 printable characters, no control characters, unchanged by secret
  redaction) and is never used as a path. At most 32 entries may declare `startup`.
- A rule is one test, or `{"all": [...]}` / `{"any": [...]}` over 1 to 4 tests (no nesting).
  Tests: `exists`, `file`, `dir` on a path; `glob` on a path whose one `*` is inside one
  folder name after a literal prefix (`.../lib/python*/site-packages/...`); `which` with
  `nvcc`, the only allowed name, looked up as an executable regular file in the fixed folders
  `/usr/local/sbin`, `/usr/local/bin`, `/usr/sbin`, `/usr/bin`, `/sbin`, `/bin`, and
  `/usr/local/cuda/bin` (never the caller's PATH, never run). `startup` uses `exists` only.
- Paths are absolute, below `/home/dwemer/`, `/var/www/html/`, or `/usr/`, at most 10 names,
  with no `.`, `..`, empty name, or backslash.
- Installed checks read metadata only (no file contents). A link is followed only within its
  zone: a system link (under `/usr`, `/etc/alternatives`, or a top-level `/bin`, `/sbin`,
  `/lib*`) may point into the system zone, and a link inside `/home/dwemer/<name>` or
  `/var/www/html/<name>` may point into that same folder or the system zone (so CUDA's
  `/usr/local/cuda` and a venv's `bin/python` count). Every entry passed must not be
  other-writable and must be owned by root (system zone), root or dwemer (`/home/dwemer`),
  or root, dwemer or www-data (`/var/www/html`). Anything else, or more than 8 links, is
  `unknown`, never present. `all` is absent when any test is absent, `any` is present when
  any test is present, otherwise an unknown test makes the result unknown. A glob examines
  at most 256 names.

## Service restart

`service restart <service>` (root) restarts one service and then verifies it fresh. The
service is either the code-owned `apache` (`/etc/init.d/apache2 restart`, the same root-owned
init script the distro start uses) or a root-registered service (below). Apache is shared, so
CHIM (HerikaServer), STOBE and Dialectic web servers and custom PHP mods stop answering for a
few seconds. TTS, speech and MCP services start from `dwemer`-owned `start.sh` scripts and a
PostgreSQL restart drops every database session, so `postgresql`, `components` and any name
that is not registered are `unsupported`; the message says root must register a trusted init
wrapper. Nothing in the request becomes a path or an argument: only an id that resolves to
`apache` or a valid registry entry.

**Registered services.** Root may register a service it controls with one
`/etc/dwemerdistro/services.d/<id>.json` (at most 16 are read; there is no installer or
migration that writes them):
`{"schema": 1, "id", "label", "impact", "init", "pidfile", "comm", "ports": [..], "http_path"?}`.
The file is read as data through the same trusted read as health (every folder from `/` and the
file root-owned, not group- or world-writable, no link followed, at most 4 KB). An entry is
refused as a whole, and listed in `service list` `refused` with a reason, unless: `id` equals the
file name and matches `^[a-z][a-z0-9-]{1,31}$` and is not reserved (`apache`, `apache2`,
`postgresql`, `postgres`, `components`, nor any id containing `apache` or `postgres`); `init` is
a file directly in `/etc/init.d`, `/usr/local/sbin` or `/usr/local/lib/dwemerdistro` that is not
an Apache or PostgreSQL script (it must also pass the trusted-helper check when used, and must
accept `restart`); `pidfile` is under `/run/` (not Apache's); `comm` is the 1-15 character
process name (not `apache2`, `postgres`, `postmaster`, `init` or `systemd`); `ports` lists 1-8
distinct ports from 1024-65535, none of them Apache's `Listen` ports or PostgreSQL's; and
`http_path`, when given, is a plain absolute path. Code-owned `apache` cannot be overridden.
Registering is root trust: the init script runs as root.

The controller refuses a restart before running an init script unless its own mount and
network namespaces match PID 1's. Apache listener verification resolves `IfModule` blocks
against `apache2ctl -M`, including disabled conditional HTTPS listeners. Unsupported
listener directives or unreadable module information require manual verification and
refuse a restart; the command does not guess or change Apache configuration.

The command takes the maintenance lock exclusively without waiting (`busy` while an update,
repair, or `changes apply` runs). It then reads the pid in `/run/apache2/apache2.pid` (the
process must be named `apache2`), runs the init script for at most 60 seconds, and polls for up
to 15 seconds until a new process (different pid or start time) is behind the pid file, the
old process is gone, and
every active `Listen` port in `/etc/apache2/ports.conf` (a registered service: its `ports`) accepts a
loopback connection. The new process must also be in the distro's own view: `namespace.mnt` and
`namespace.net` pass only when `/proc/<new pid>/ns/{mnt,net}` equal PID 1's, so a process left in
a `ddistro_run` private view (see ASSISTANT_RUNTIME.md) fails them; unreadable is `unknown`,
never passed. With `http_path`, `http` passes on any HTTP status line from the first port, 503
included: it proves transport, not application health (`health: "not_checked"`). `data` is
`{service, label, action, executed: true, state: "verified"|"failed", verified, checks:
[{id: "command"|"new_process"|"namespace.mnt"|"namespace.net"|"port.N"|"ports"|"http", result,
detail?}], before: {pid}, after: {pid}, namespace: {mnt, net}, impact, health, verified_at,
output_tail}`. An exit code of 0 alone is never `verified`. A refusal
before the restart (`permission_denied`, `helper_unavailable`, `unsafe_path`, `busy`,
`unsupported`) is an error envelope and runs nothing. `capabilities` lists the code-owned
services with label and impact under `service_restart` (with the registry rules), and
`features.assistant_actions` says the broker offers it to the Assistant (see
ASSISTANT_RUNTIME.md, "ddistro_action" and "Service operation").

`service status <service>` is read-only and needs no root where `/proc` is readable: `{service,
label, action: "status", executed: false, state: "running"|"degraded"|"unchecked"|"not_running",
pid, started, namespace, checks, health: "not_checked", captured_at}` with the same namespace,
port and `http` checks (`degraded`: a check failed, for example a private view; `unchecked`: a
namespace could not be read). `service list` returns `{schema: 1, services: [{id, label, impact,
verbs, source: "code_owned"|"registry", ports?}], refused: [{id, reason}], registry,
unsupported, captured_at}`. The typed `operations` matrix and the legacy `propose_restart` and
`propose_service` cards stay `apache`-only.

## Typed service operations

`operations` is a fixed matrix of code-owned handlers. It never takes a command, script, argument
or environment from a request.

```
operations targets
operations preview --target T --verb V [--branch B] [--task UUID --thread ID]   (root)
operations apply --receipt ID --plan-sha256 HASH [--task UUID --thread ID]      (root)
```

- `apache`: `restart` only, through `service restart apache` with its lock and checks.
- `herika`, `lorkhan`, `stobe`, `dialectic`, `reign`:
  - `install` needs the server to be `not-installed`.
  - `update` needs it to be `installed` and `managed`.
  - Both run the trusted `ddistro_server` with exactly `<verb> <product> --branch <choice>`. The
    branch must be one the product lists (default `main`).
  - `--force`, `repair` and `uninstall` are never used.
- `start` and `stop` are not operations. PostgreSQL and every component (TTS, STT, LLM, MCP and
  others) are `unsupported`. `operations targets` and `capabilities.service_operations` list each
  unsupported verb and target with its reason.

`preview` changes nothing. It saves a root-private receipt that expires after 120 seconds and
holds:

- at most 8 plain summary lines
- the captured baseline: Apache's process, or the server's state, branch and commit
- the helper's sha256
- `plan_sha256`

`apply` runs that plan at most once:

1. It checks the hash, the task and thread binding, and the expiry.
2. It captures the baseline and the helper hash again. If either changed, the result is `stale`
   and nothing runs.
3. It runs the plan. An install or update is `verified` only when, afterwards, the server is
   installed and managed on the resolved branch and its `HEAD` equals its local origin ref. Exit
   0 alone is `unverified`. No health check is claimed (`result.health: "not_checked"`).

States: `verified`, `unverified`, `failed`, `busy`, `not_executed`, `stale`, `expired`, and
`executing` (outcome unknown). Applying an already used receipt again returns its recorded outcome
with `replayed: true` and runs nothing, even after a failure. Receipts are kept in
`/var/lib/dwemerdistro/assistant/operation-receipts/` (at most 200, for 30 days).

## Read-only access: files, config, database

These root-only, read-only commands come from `ddistro_access`, a library installed root-owned
next to `ddistro`. ddistro loads it only when the file and its folder are root-owned and only root
can change them. There is no path or environment override. When the library is missing or
untrusted, these commands fail with `helper_unavailable`, `features.explore` is false, and
`capabilities.access` is `{available: false, reason}`. Every other command works as before.

```
access roots
files list ROOT [--path P] [--depth 1-3] [--limit <=500]
files search ROOT [--query TEXT] [--name-glob GLOB] [--path P] [--limit <=100]
files read ROOT --path P [--start N] [--lines <=400]
config get ROOT [--key KEY]
db schema PRODUCT [--schema S] [--table T]
db query PRODUCT --query-json OBJECT
```

**Roots** are ids from a table in code: the five servers and the 18 catalog component ids. The
catalog supplies no paths and grants nothing. Python components point at their fixed service source
folder (for example minime `/home/dwemer/minime-t5`, xtts `/home/dwemer/xtts-api-server`, pipertts
`/home/dwemer/piper`, localwhisper `/home/dwemer/remote-faster-whisper`, melotts
`/home/dwemer/MeloTTS`, mimic3 `/home/dwemer/mimic3`), not their virtualenv. `present` only means that
folder exists as a real directory; it says nothing about install health. `change` is true for the
roots in the code-owned `CHANGE_ROOTS` set (the five servers and every component with a fixed
folder); ddistro_changes reads that flag and still applies these exclusions and its own file-type
rules. `cuda` (`gpu_global_mount`) and `lmstudio`
(`account_state_folder`) have no readable root and are `unsupported`. Paths are relative to a root.
`..`, absolute paths, backslashes and control characters are `invalid_argument`. Every folder is
opened from `/` without following links. Symbolic links are listed but never followed or read.
Hard-linked files are refused. These are never listed into, searched, or read:

- Git metadata
- dependencies (`venv`, `.venv`, `node_modules`, `site-packages`, `.tox`, `.eggs`)
- nested worktrees (`.worktrees`, `.codex-worktrees`) and tool caches (`.mypy_cache`, `.pytest_cache`,
  `.ruff_cache`)
- runtime data (`log(s)`, caches, `uploads`, `voices`, `models`, backups)
- media, model weights and archives
- likely secret files (`.env`, keys, certificates, anything named like a token, password or
  credential)
- the PHP servers' `conf/conf.php`, per-NPC `conf/conf_*` files and `data/pictures`

Reading one of these is `refused` with `details.reason` `secret_path`, `excluded_vcs`,
`excluded_dependency`, `excluded_runtime` or `excluded_media`.

Text is UTF-8 only; binary files are `refused` (`binary`). Every returned line is redacted
(key/value secrets, PHP `$GLOBALS[...]` keys, URL user info and queries, bearer tokens, token
shapes, JWTs, private keys, DSN passwords) and clipped to 4096 characters. `files search` is
literal and case-insensitive. It matches the redacted line, so it cannot be used to probe secret
values. It reads files up to 1 MiB and stops at 32 MiB, 5000 files, 20,000 entries or 20 seconds.
With only `--name-glob` it searches names. Links and hard-linked files are never read; they are
counted in `coverage.skipped` (`link`, `hard_link`), as are unreadable files.

**Config** is parsed as data and never executed. It reads the PHP servers' `conf/conf.php` as plain
`$NAME[...] = literal;` assignments, plus JSON, INI/CFG/CONF and TOML files in the root, `conf/`
and `config/`. YAML is listed as `unsupported_format`. Concatenation, function calls,
interpolation and array literals are listed under `unparsed` by key and line only. A key that names
a secret shows `<set>` or `<unset>`, and opaque token-like values become `[redacted]`.
`effective_value` is always `uncertain`, because loaders, profiles and database settings can
override file values.

**Database** reads run as the dedicated PostgreSQL role and OS account `ddreader` over local
peer authentication (`runuser -u ddreader -- psql -X --no-password`), never as `postgres`. Each
read is one `BEGIN TRANSACTION READ ONLY` with a 5-second statement timeout. Root provisions the
reader with `install_assistant_runtime --db-reader`. A regular `install_assistant_runtime` run does
the same step itself when PostgreSQL is running and `ddistro_access` is installed. It downloads
nothing for this step. If the step cannot run, the installer prints a warning and the runtime
install still stands. The step:

- creates or validates the account and role (no superuser, createdb, createrole, replication,
  bypassrls, memberships or owned objects)
- sets read-only defaults, timeouts and `CONNECTION LIMIT 2`
- drops every table, column and schema privilege ddreader itself holds in each reviewed database
  (column grants are revoked by name, because `REVOKE ALL ON TABLE` leaves them), then grants
  `CONNECT`, `USAGE` and `SELECT` on reviewed base-table columns only, in one transaction per database
- creates or refreshes one managed view per granted herika schema whose `core_npc_master` has
  `id`, `npc_name` and a json/jsonb `extended_data`: `ddreader_core_npc_master` (`security_barrier`,
  owned by `postgres`, labelled with a fixed comment, `PUBLIC` revoked on that view only). It exposes
  `id`, `npc_name`, `middle_term_memory_count`, `middle_term_memory_type` and
  `middle_term_memory_present`, never raw JSON. ddreader gets `SELECT` on the view, not on
  `extended_data`. A same-named object that is not this labelled view stops the run for that database
  with nothing changed. This is the only metadata the installer writes; no row, table, other role,
  owner or `PUBLIC` default changes.

Privileges granted to ddreader by a role other than the object owner, and views left in snapshot
schemas that are no longer listed (their grants are removed), are not cleaned up. The view depends on
`core_npc_master.extended_data`: dropping that table or altering that column without `CASCADE` fails
while it exists. `DROP VIEW <schema>.ddreader_core_npc_master` removes it; rerunning the installer (or the automatic
refresh described below, for `public`) recreates it.

The reviewed columns are:

- herika: `memory`, `memory_summary`, `eventlog`, `quests`, `questlog`, `core_npc_master` (id,
  npc_name), in `public` and in the `chim_profile_*` schemas listed in
  `chim_meta.playthrough_profiles`. Only that table's `id`, `schema_name` and `is_active` are
  readable. `public` is the current live game and the default. Each `chim_profile_*` schema is a
  stored snapshot that CHIM clones back into `public` when a playthrough loads, so it can lag the live
  game, including the one `is_active` names. Snapshot answers are labelled as snapshots; an
  `is_active` snapshot is never picked as live. Zero rows in `public` is a valid live answer.
- dialectic: the same tables in `public`, except `questlog` and `core_npc_master`.
- stobe: `memory`, `memory_summary`, `eventlog`, `diarylog` and `core_npc` descriptors.
- lorkhan and reign: `unsupported` (`no_reviewed_tables`).

No connector, configuration, auth, settings or blob table, and no vector column, is granted.
CHIM's playthrough clone copies tables and data only, never views or grants. Loading a playthrough
drops `public` with `CASCADE` and clones the snapshot back, so it removes the view and every ddreader
grant in `public`. Saving creates a snapshot schema without grants or the view.

`ddistro db` repairs this itself. When a read hits exactly one of these gaps (`not_granted`,
`projection_missing`, or `db schema` finding `public` ungranted or its view missing), it runs the
installer's refresh script once, as `postgres`, for the one reviewed database being read. Then it
retries the read once. The script is `ddistro_access --emit-refresh-sql DATABASE`, which the
installer also runs:

- The script is fixed in code. No model text, SQL, argv or environment reaches it.
- It runs in one transaction with a 10-second statement timeout and a 2-second lock timeout, inside a
  20-second process-group timeout. psql and runuser must be trusted root-owned programs.
- It first checks that `ddreader` is already the dedicated role the installer made: its comment,
  no privileged attributes, `NOINHERIT`, read-only default, no memberships or owned objects, and
  `CONNECT`. Otherwise it stops with nothing changed (`role_not_dedicated` or `db_reader_missing`).
  It never creates an account or role, and never touches `pg_hba`, services, rows, other roles or
  `PUBLIC` defaults.

Every `db query` and `db schema` result has `coverage.refresh`:
`{count: 0|1, trigger, ok, metadata_changed, schemas, reason, database}`.
`metadata_changed` compares fingerprints of ddreader's privileges and the managed views taken
before and after the refresh.

If the refresh fails, the read keeps its original error, with `details.refresh` saying why. The
reasons are `view_collision`, `lock_timeout` (for example, CHIM is in the middle of a load),
`statement_timeout`, `role_not_dedicated`, `not_root`, `helper_unavailable` and `failed`. On a
failed refresh, `db schema` stays incomplete (`public_not_granted`, with `coverage.recovery`). A
`core_npc_master` entry without the view keeps `derived: "projection_missing"`. Nothing is reported
as empty or zero. Root then reruns `install_assistant_runtime --db-reader`.

A request refreshes at most once. Asking about a snapshot schema never triggers a refresh, because
only gaps in `public` do. A query whose grant stays missing after a successful refresh is still
`permission_denied` (`not_granted`). A reader that was never provisioned is `unsupported`
(`db_reader_missing` or `unsupported_auth`).

Future CHIM migrations are not verified. A migration that drops `core_npc_master` without
`CASCADE`, or changes the type of `extended_data`, `id` or `npc_name`, would fail while the view
exists. The current load path (`DROP SCHEMA public CASCADE`) and the known `db_updates.php`
migrations are safe.

`db query` takes a JSON object and never SQL. Raw SQL and `--sql` are `refused` (`raw_sql_refused`).

```
{"schema": "public" | "chim_profile_*", "table": T, "columns": [C...],
 "aggregates": [{"fn": "count"|"count_distinct"|"sum"|"min"|"max"|"avg", "column": C?, "as": ALIAS?}],
 "filters": [{"column": C, "op": "eq"|"ne"|"lt"|"le"|"gt"|"ge"|"contains"|"starts_with", "value": V}
             | {"column": C, "op": "in"|"not_in", "values": [V...]} | {"column": C, "op": "is_null"|"not_null"}],
 "group_by": [C...], "order_by": [{"key": C|ALIAS, "dir": "asc"|"desc"}], "limit": 1-500, "offset": 0-100000}
```

Rules for the query object:

- Filters are joined with AND.
- Identifiers come only from the reviewed policy and are quoted.
- Text values are passed as psql variables. Numbers and booleans are validated literals.
- The builder generates no functions besides the aggregates and its own text clipping and
  matching.
- herika `core_npc_master` adds three derived columns from `extended_data.middle_term_memory`:
  `middle_term_memory_count` (array length or object key count, 0 when absent, NULL for text),
  `middle_term_memory_type` and `middle_term_memory_present` (1 for a non-empty value). A query that
  uses any of them reads the managed `ddreader_core_npc_master` view in the same schema instead of
  the table; other `core_npc_master` queries read the table. The view name is fixed in code.
  `extended_data` itself is never selectable or granted. A schema without the view is `unsupported`
  (`projection_missing`).

The result is `{columns, rows (arrays), row_count, redactions, ms, transaction: "read_only"}`.
Text cells are clipped to 512 characters and redacted, and the output is capped at 1 MiB.

`db schema` lists the granted columns, planner row estimates and the playthroughs. Its `notes`
explain the memory categories. Dialogue memory rows, summaries, events and NPC middle-term memory
are different things and are never one total.

Every result carries `captured_at`, `complete`, `truncated`, `reason` (for example `entry_limit`,
`time_limit`, `byte_limit`, `row_limit`, `line_window`, `playthroughs_unavailable`) and `coverage`.
A cut is never reported as absence.

## Sandboxed exec on snapshots

`exec --task UUID --request-json JSON` (root) runs `/usr/local/bin/ddistro_exec` with exactly
those arguments. The request is `{argv, files, timeout}`, at most 64 KiB:

- `argv[0]` is one of a fixed list of tools (`grep`, `sed`, `awk`, `diff`, `python3`, `sh` and
  others).
- `files` lists up to 20 `{root, path}` files, read through the `ddistro_access` policy.
- `timeout` is 1 to 30 seconds.

Each file becomes a redacted, read-only snapshot at `/inspect/<root>/<path>`, captured when the
command starts. The sandbox has no network, no live root, no home folder and no database socket.
Only the task's scratch folder (`/work`) and a private `/tmp` are writable.

Snapshots are sanitized copies, not the live system, and a redacted snapshot can differ from the
file on disk. Whether a service is running, and what the WSL host actually holds, come from
`status`, `logs`, `health`, `performance` and `access` instead.

`data` reports the exit status, stdout and stderr (redacted, capped at 64 KiB and 16 KiB), and each
snapshot with `live: false`. A timeout or cancel stops the whole process group.

## Trust boundaries and future scope

The CLI is a convenience and contract layer, not a sandbox or authorization boundary:
anything it can do, the same user can do directly. Root-only work stays in
`ddistro_server` and `ddistro_changes`, which own their locking. Without `live_run` the Assistant gets
no root shell, no raw SQL and no general service control: it can use only the typed, allowlisted operations
described here, and every change needs approval in the Launcher. A Launcher that negotiates `live_run` adds
`ddistro_run`: arbitrary commands, SQL through `psql`, and package or service work on the live distro, as
`dwemer`, `root`, `postgres` or another account, each run approved by the user in the Launcher (or covered by a
session grant the user approved). A Launcher that also negotiates `live_run_auto` lets runs the model declares
routine start without a prompt; destructive ones still need the user's confirmation of the exact request, and the
broker's destructive-pattern check is a backstop, not a guarantee. That is a general, full-trust path, not a sandbox
and not an allowlist; its masks only prevent accidental access to Windows and the assistant's own state. Launcher chat may come
later and will appear in `capabilities` only when implemented. The existing CHIM-MCP `/chat` is unchanged.

## Health

`health [product]` runs bounded local checks now and reports each one; it repairs nothing (it
never calls `ddistro_doctor --repair`) and tests no external provider, model, or in-game path.
There is no overall verdict: each check is `passed`, `failed`, or `skipped` with a `reason`.

```json
{"captured_at": "...", "scope": "local_bounded", "product": null, "read_as": "root",
 "summary": {"passed": 9, "failed": 1, "skipped": 16},
 "checks": [{"id": "server.herika.port", "component": "herika", "result": "passed",
             "evidence": "tcp 127.0.0.1:8081 accepted in 1 ms (port from ddistro_server status)",
             "reason": null, "captured_at": "..."}],
 "scope_limits": ["..."]}
```

- `server.<product>.installed|port|database`: from the validated `ddistro_server status`
  (30 seconds). `installed` passes only for state `installed`; a `not-installed` product is
  one skipped check. `port` connects to the product's reported port on `127.0.0.1` (1.5 s).
  `database` is the helper's presence check (`skipped` when PostgreSQL was not ready). A
  helper failure is one failed `servers.status` check.
- `postgres.port`: TCP to `DDISTRO_POSTGRES_PORT` (the existing override) or 5432.
- `component.<id>.port` (whole-distro only): an absent or unreadable startup marker is
  skipped. A present component is port-checked only when it has a known port: `xtts` 8020,
  `omnivoice` 8021, `parakeet` 8022, `pockettts_audiocpp` 8086 (overridable by plain
  `NAME=port` lines of a root-owned `/etc/dwemerdistro_services.conf`, which is parsed, never
  sourced), and `chatterbox`/`pocket_tts` from their `.dwemerdistro-port` file (legacy 8020).
  Every other component is skipped with `no known local port contract`. Both files are read
  only when they are regular files of at most 4 KiB, reached from `/` without following any
  link, with the file and every folder above it owned by root (or, for the port files, the
  `dwemer` account) and not group- or other-writable; they are opened non-blocking, so a FIFO
  or device never stalls the check. Anything else is ignored and the default port is used.
- `disk./`, `disk./var/lib/postgresql`, `disk./home/dwemer`: fails below 2 GiB or 5% free.

- `server.<product>.http` and `component.<id>.http` (after a passing port check): one fixed
  `GET` on `127.0.0.1` with a 2 second total limit and a 64 KiB answer limit, only where the
  service itself defines the endpoint: `dialectic` `/DialecticServer/health.php` and `stobe`
  `/StobeServer/health.php` (pass on HTTP 200 with the expected `service` and `status: "ok"`;
  `database`/`schema`/`background_processor` facts are shown), `omnivoice` `/health` (`status:
  "ok"`), and the TTS identity `/provider_info` for `xtts`, `chatterbox`, and `pocket_tts` (the
  same check `ddistro_doctor` uses: the expected provider passes, another provider fails as a
  port conflict, no `provider` is skipped because older releases predate it). Every other
  server and component gets a skipped `http` check with `no known local health contract`. No
  other URL, host, or port is ever requested.

A passing port check proves only that something accepts TCP there, not which service.

## Performance

`performance [product]` measures bounded local timings now; like `health` it changes nothing
and has no overall verdict. Each measurement is `measured`, `failed`, or `skipped` with a
`reason`; `duration_ms` values are whole wall-clock milliseconds (`time_unit: "ms"`) measured by
ddistro now, never provider, TTS, or in-game latency. Herika request timings are recorded by
HerikaServer itself and reported as `values`. Nothing is judged fast or slow.

```json
{"captured_at": "...", "scope": "local_bounded", "product": null, "read_as": "root", "time_unit": "ms",
 "deadline_seconds": 15.0, "complete": true, "summary": {"measured": 7, "failed": 0, "skipped": 6},
 "measurements": [{"id": "postgres.select1", "component": "postgres", "result": "measured",
                   "duration_ms": 48, "values": {"client_setup_included": true}, "reason": null}],
 "scope_limits": ["..."]}
```

- `server.<product>.http`: the same fixed `GET` health makes (2 s, 64 KiB), timed, for
  `dialectic` and `stobe` only; values are `http_status`, `service_matched`, `status_ok`. No body,
  path, or port is returned. Other servers are skipped (`no known local health contract`); a
  not-installed one is skipped too. Ports come from `ddistro_server status`.
- `postgres.select1` (root only, else skipped `requires_root`): one `SELECT 1` through the
  `models` path (`runuser -u postgres psql`, read-only transaction, statement timeout), at most
  10 seconds. It includes process start, connection setup, and up to 100 ms of polling
  (`client_setup_included: true`), so it is not query performance. Output other than `1` fails
  as `unexpected_output`.
- `host.load` (`/proc/loadavg`, plus `cpu_count`), `host.memory` (`/proc/meminfo` total,
  available, and swap, in MiB), `host.pressure.cpu|memory|io` (PSI `avg10` percent): fixed paths
  read with the health config-file reader (4 KiB, no links); missing or malformed counters are
  skipped, never estimated. These are the distro's Linux (WSL) view, not Windows, game, or GPU load.
- `herika.request_timing` (all products or `herika` only): recent HerikaServer request records,
  parsed from the same bounded `chim.log` tail `logs herika` reads (last 256 KiB, no links, regular
  file only, in an unredirected log folder). See below.
- `llm.provider_latency`, `tts.recent_timing`: always skipped. No maintained server records
  provider latency, time to first token, or TTS timing.

### HerikaServer `[PERF]` records

The supported schema is the one HerikaServer's `lib/request_performance.php` writes at the end of
a request through `Logger::trace`, one line in `log/chim.log`:

```
[2026-10-20T14:00:00+00:00] [trace] [PERF] {"run_id": "...", "request_type": "...", "status": "complete",
  "total_ms": 2500.5, "sql_ms": 40.25, "peak_memory_mb": 12, "phases": [{"name": "request_parsed", "delta_ms": 3.1, "total_ms": 3.1}, ...]}
```

Installed versions that predate it write no such line; the measurement is then skipped (`no [PERF]
records in the chim.log tail`). It is never turned on: ddistro does not enable trace logging, change
configuration, or ask for a generation. Legacy connector START/END lines and audit elapsed times are
not read, because their durations are not defined as request or provider timings.

Only lines matching `[<time>] [trace] [PERF] {...}` are considered, newest first. A record counts
when its time has a zone and falls in the last 15 minutes (up to 60 s ahead is tolerated), its
`status` is `complete`, the line is at most 16 KiB, and `total_ms`, `sql_ms` (both at most one day in
ms), and `peak_memory_mb` (at most 1 TiB) are finite, non-negative JSON numbers (a boolean or string
is not a number; `NaN`/`Infinity` are malformed). `phases` must be a list of at most 64 entries;
only these stage names are kept, each with its `delta_ms` (time since the previous mark), validated
the same way: `request_parsed`, `lock_ready`, `profile_ready`, `context_history_ready`,
`memory_ready`, `oghma_ready`, `prompt_ready`, `llm_complete`. Other phase names are ignored. The
newest 32 valid records are summarised:

```json
{"window_seconds": 900, "tail_bytes": 262144, "perf_records_in_tail": 4, "older_than_window": 1,
 "skipped_records": {"invalid_number": 1}, "samples": 2, "sample_limit": 32, "sample_limit_reached": false,
 "window_covered": true, "newest_age_seconds": 30, "oldest_age_seconds": 300,
 "total_request_ms": {"count": 2, "median": 2500.0, "max": 3000.0}, "sql_ms": {...}, "peak_memory_mb": {...},
 "stage_delta_ms": {"prompt_ready": {...}, "llm_complete": {"count": 2, "median": 1800.0, "max": 2100.0}},
 "llm_complete_scope": "connector_generation_output_tts_and_retry_not_provider_latency_or_ttft"}
```

- `skipped_records` counts rejected `[PERF]` lines by fixed reason only: `malformed`, `oversize`,
  `invalid_time`, `time_without_zone`, `future_time`, `not_complete`, `missing_field`,
  `invalid_number`.
- `llm_complete` is the time from `prompt_ready` until HerikaServer's `call_llm()` returns: local
  connector work, generation, per-sentence output (which may include TTS synthesis and player TTS),
  and any fallback retry. It is not provider latency, time to first token, or a TTS timing, and
  none of those can be separated from it.
- `window_covered` is true only when the tail reaches back past the 15 minute cutoff (it starts at
  the file's beginning or at a dated line older than the cutoff) and the sample limit was not hit.
  Otherwise the result carries `reason` `partial: ...` and makes no claim about the whole period.
- The raw line, the JSON, `run_id`, `request_type`, `status`, phase names outside the list, and
  any other key are never returned. No record means `skipped` with a specific reason: no `[PERF]`
  record in the tail, none in the last 15 minutes, none valid, or the log missing, unreadable, a
  link, not a regular file, or in a redirected folder.

The whole run has a 15 second deadline. A step that no longer fits is skipped with `deadline
reached` and the reply has `complete: false`; host counters are always read. At most 64
measurements are returned. No external request, provider call, or configuration change is made.
`herika.request_timing` with `window_covered: false` does not make the run incomplete; `complete`
is only about the deadline.

## Assistant memory

`memory <action> [flags] --json` (root) manages short, user-controlled notes that the assistant
may recall in any chat of this installation (scope `installation`: one store shared by every
task and thread; provenance is recorded, not used to filter). Notes are never authoritative:
anything that can change (servers, ports, models, logs, health) must come from fresh
diagnostics, and search results say so (`authority: "non_authoritative"`, `notice`). Nothing
imports old chats, nothing stores transcripts, and Codex's own `features.memories` stays off.

Storage is SQLite at `/var/lib/dwemerdistro/assistant/memory/memory.sqlite`, separate from
Codex's conversation state and the broker's lock and thread mapping. Every component is opened
without following links; the parents must be root-owned and not group/other-writable, the
`memory` directory root `0700`, the database a root `0600` regular file with one link. Anything
else (symlink, FIFO, hardlink, other owner, looser mode) fails with `unsafe_path`
(`details.reason: "memory_untrusted"`, `details.location`) and is neither read nor repaired.
Files are created under umask `0077`; `secure_delete` is on, so forgotten text is overwritten.
`status`, `list`, `search`, `export`, `forget`, and `reset` never create the directory or
database; only `add` and `configure` do. The schema (`PRAGMA user_version` 1) is upgraded in
place and idempotently: an FTS5 index (with triggers) is added whenever SQLite has FTS5, also to
a database made without it; a newer schema is refused (`unsupported`). Without FTS5, search is an
escaped `LIKE` scan. Every statement is parameterized; query words are quoted FTS terms, never
FTS or SQL syntax.

| Action | Flags | `data` |
|---|---|---|
| `status` | - | `present`, `location`, `schema_version`, `search_engine` (`fts5`/`lexical`/`null`), `settings`, `notes`, `expired`, `scope`, `authority`, `limits` |
| `list` | `--limit 1-200` (50), `--offset 0-1000` | `notes`, `total`, `limit`, `offset`, `next_offset` (newest update first; expired notes included with `expired: true`) |
| `search` | `--query TEXT` (required, <= 200 chars, first 8 words), `--limit 1-10` (5), `--assistant` | `terms`, `engine`, `notes`, `authority`, `notice` (expired notes never match) |
| `add` | `--text TEXT` (required), `--kind preference\|decision\|note` (`note`), `--expires-days 1-3650`, `--source user\|assistant` (`user`), `--task TASK`, `--thread ID` | `note`, `duplicate` (same kind and text returns the existing note) |
| `update` | `--id N` (required), and any of `--text`, `--kind`, `--expires-days`, `--no-expiry` | `note` (an edited note becomes `source: "user"`; task/thread provenance is kept) |
| `forget` | exactly one of `--id N`, `--task TASK`, `--thread ID`, `--expired` | `forgotten`, `by` |
| `reset` | `--yes` (required) | `forgotten`, `settings_kept: true` (deletes every note, keeps settings) |
| `export` | - | `exported_at`, `schema_version`, `scope`, `settings`, `notes` (all, including expired) |
| `configure` | `--retrieval on\|off`, `--auto-learning on\|off` (at least one) | `settings` |

A note is `{"id", "kind", "text", "source", "task_id", "thread_id", "captured_at", "updated_at",
"expires_at", "expired"}` with UTC `...Z` times; `settings` is `{"retrieval": bool,
"auto_learning": bool}`, defaulting to both on: memory is automatic. A setting stored by `configure`
(for example an earlier `off`) wins over the default; the launcher configures both on once as it
connects, before the broker starts, and `configure` otherwise stays a debugging control. Limits: 1000
notes (`limit_reached`, exit 65), 2000 characters per note, 10 search results, 200 query
characters, 8 query terms. `--task` is a ddistro task id; `--thread` is 1-128 letters, digits,
`-` or `_`.

Text is trimmed and refused (`invalid_argument`) when empty, too long, or holding control
characters other than newline and tab. Text that looks like a credential is refused, never
stored redacted: `refused` (exit 65, `details.reason: "secret_shape"`) for anything the log
redaction would change (key or token assignments, `Bearer`/`Basic`, `sk-`/`ghp_`/`hf_`-style
tokens, URL credentials or queries) and for private-key blocks, JWTs, cloud key ids, and long
mixed letter-digit tokens. Refused text is never echoed in the error. Arguments are passed as
plain argv; ddistro logs none of them, but `--json` and `--request-id` are global flags
anywhere on the line, so neither can be a value.

`--source assistant` is the broker's path only (see ASSISTANT_RUNTIME.md): it needs `--kind
preference|decision`, `--task`, and `--thread`, and is refused with `disabled`
(`details.reason: "auto_learning_off"`, exit 69) unless both retrieval and automatic learning
are on. Such a note may not claim a verified fix or resolution (`refused`, `details.reason:
"unverified_claim"`); only the user can record one. `search --assistant` is refused with
`disabled` (`retrieval_off`) when retrieval is off; a user search without it always works.
Errors from the database are `busy` (another writer for over 5 s) or `failed`
(`details.reason: "memory_database"`). Every action requires root (`permission_denied`).
