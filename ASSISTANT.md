# ddistro command and JSON contract (schema version 1)

`bin/ddistro` is one stable, stdlib-only Python command for people, the launcher, and a
future assistant to read DwemerDistro state and run the existing server update. It adds
no daemon, service, or dependency. `update.sh` installs it as root-owned
`/usr/local/bin/ddistro` mode `0755`, so every user can read and run it.

```bash
ddistro version | capabilities | products
ddistro status [product]                        # summary, any user
ddistro logs <product|apache|postgres> [--log NAME] [--lines N]   # any user who can read the file
ddistro server status [product]                 # full validated ddistro_server records
ddistro server update <product> --branch <branch> [--force]       # root only
ddistro changes prepare --product herika [--path RELATIVE ...]    # root only, see File changes
ddistro changes diff|stage|status|rollback TASK | changes list    # root only
ddistro changes apply TASK --manifest-sha256 HASH                 # root only
ddistro agent status | agent probe                                # probe root only, see Assistant agent
ddistro agent serve --task TASK                                   # root only, raw JSONL
ddistro models herika                                             # root only, see Model assignments
ddistro inventory                                                 # any user, see Inventory
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
| `not_found` | 66 | Update target is not installed |
| `unsupported`, `helper_unavailable` | 69 | `ddistro_server` missing or not a trusted root-owned file |
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
`features.inventory` and `capabilities.inventory` describe `inventory` (scope, the products
with an extension folder, component ids, and limits).

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
  would try the component, not that it runs or is healthy. Paths are never returned.
- `custom_mods` runs the trusted `/usr/local/bin/ddistro_custom_mod status --json` (no
  `--check-health`, no network, 10 seconds) and keeps only `id`, `name`, `state`, `branch`,
  and a 40-hex `commit` for at most 100 mods; repository, URLs, folders, databases, and
  messages are dropped.
- A section that cannot be read is still `ok`: `available: false` with a `reason` token
  (`helper_unavailable`, `helper_failed`, `helper_protocol`, `timeout`, or `failed`). `complete`
  is true only when every section was read in full: nothing unavailable, truncated, or
  skipped, and no folder or marker `redirected`, `not_directory`, or `unreadable`; cancellation stops the helper and the
  whole command is `cancelled`. Run as a user other than root, folders that user cannot read
  are `unreadable` and database presence may be `null`; the launcher runs it as root.

## Trust boundaries and future scope

The CLI is a convenience and contract layer, not a sandbox or authorization boundary:
anything it can do, the same user can do directly. Root-only work stays in
`ddistro_server` and `ddistro_changes`, which own their locking. Launcher chat may come
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

A passing port check proves only that something accepts TCP there, not which service.
