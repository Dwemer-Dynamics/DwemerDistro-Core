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

## Trust boundaries and future scope

The CLI is a convenience and contract layer, not a sandbox or authorization boundary:
anything it can do, the same user can do directly. Root-only work stays in
`ddistro_server` and `ddistro_changes`, which own their locking. Launcher chat may come
later and will appear in `capabilities` only when implemented. The existing CHIM-MCP `/chat` is unchanged.
