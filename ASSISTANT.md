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
| `partial`, `conflict`, `rollback` | 10, 11, 12 | Reserved for later stages |
| `invalid_argument` | 64 | Unknown command, product, option, branch, or value |
| `not_found` | 66 | Update target is not installed |
| `unsupported`, `helper_unavailable` | 69 | `ddistro_server` missing or not a trusted root-owned file |
| `busy` | 75 | Another server operation holds the shared lock |
| `permission_denied`, `unsafe_path` | 77 | Not root for an update, unreadable log, or redirected/non-regular log |
| `update_blocked_tracked` | 79 | Tracked edits block the update; `--force` would discard them |
| `update_blocked_untracked` | 80 | Untracked files are in the way and must be moved |
| `timeout`, `cancelled` | 124, 130 | Helper stopped after timeout, or the caller cancelled |

`capabilities` lists only implemented commands, aliases, log names, limits, and error
codes; `features.file_changes`, `locks`, `agent`, and `chat` are `false`.

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

## Trust boundaries and future scope

The CLI is a convenience and contract layer, not a sandbox or authorization boundary:
anything it can do, the same user can do directly. Root-only work stays in
`ddistro_server`, which owns locking and the update itself. Later stages may add file
changes with locks, an isolated assistant agent, and launcher chat; each will appear in
`capabilities` only when implemented. The existing CHIM-MCP `/chat` is unchanged.
