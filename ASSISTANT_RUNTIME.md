# Assistant runtime (Stage C, native Codex)

`bin/ddistro_agent` runs the pinned native Codex App Server for one prepared task inside
a dedicated, attested sandbox. The model can change only the task's scratch copy.
Applying scratch changes to a live install is a separate, trusted launcher/manual
operation and is never done by this tool. A task ID or `--yes` grants nothing.
The one exception is `ddistro_run` (`live_run`, below): a client that opts in lets the model
propose commands on the LIVE distro, and each runs only after the user approves it (or a
session grant they approved covers it). Approved, it is not a sandbox: as root it is full
trust over the distro.

```bash
install_assistant_runtime [--archive PATH] [--host-archive PATH] [--install-prerequisites]   # root, explicit
ddistro_agent status            # any user; one JSON line
ddistro_agent probe             # root; real sandbox probe; writes the attestation
ddistro_agent serve --task ID   # root; raw Codex App Server JSONL on stdin/stdout
ddistro_agent home-config [--repair --sha256 HASH]   # root; account-home config status or quarantine
```

## Pinned runtime

| Item | Value |
|---|---|
| Release | `rust-v0.163.0` (`codex-cli 0.163.0`) |
| Archive | `https://github.com/openai/codex/releases/download/rust-v0.163.0/codex-x86_64-unknown-linux-musl.tar.gz` |
| Archive SHA256 | `beb339e8ecd9ba057a6bb2dc81ad8ca474812edfbf0fc5056ca818797648dde4` |
| Binary SHA256 | `399ff69c431c066e40de318fba45c2b07f45a76b19939d247ee4828fc70c4215` |
| Installed binary | `/opt/dwemerdistro-agent/runtime/codex-x86_64-unknown-linux-musl` (root, `0755`) |
| Helper links | `/opt/dwemerdistro-agent/runtime/bin/{codex,codex-linux-sandbox}` -> `../codex-x86_64-unknown-linux-musl` |
| Code-mode host archive | `https://github.com/openai/codex/releases/download/rust-v0.163.0/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz` |
| Host archive SHA256 | `314263128ba6592e7e807c265deeaf928a692b72dcbfeb5e0b20c9423b703529` |
| Host binary SHA256 | `8724d73ec190536233ab4628d24a071bfbcf27e5124f08fe35129f26fe48b916` |
| Installed host | `/opt/dwemerdistro-agent/runtime/codex-code-mode-host` (root, `0755`) |

The installer requires root and x86_64. For each archive it checks the archive hash before
listing or extracting it, accepts only the single expected regular file, checks the extracted
binary hash, and installs atomically. Rerunning it changes nothing when both binaries
already match; installing either one removes the attestation.

The code-mode host is the official companion from the same release. Models whose metadata
selects `code_mode_only` run every tool through it, and Codex 0.163.0 has no in-process
code-mode runner: `features.code_mode_host = false` selects a disabled provider, and only
`code_mode` (not `code_mode_only`) falls back to direct tools. Without the host such a model
cannot run any tool (`failed to spawn code-mode host .../codex-code-mode-host`). The App
Server starts it from the runtime binary's directory with piped stdio and no arguments, so
it runs as `ddagent` in the same outer sandbox and opens no listener. Its JavaScript has no
network, file, process, or import access; it can only call the session's tools, which still
go through the `dwemer-scratch` tool sandbox. It never installs packages unless `--install-prerequisites` is given.
Bubblewrap must support `--bind-fd`, `--ro-bind-data`, `--clearenv`, and
`--die-with-parent`. The installer and `status` check the installed `bwrap --help` for these
features, not its version: distro builds backport `--bind-fd` (Ubuntu 24.04's 0.9.0 and
Debian 12's `0.8.0-2+deb12u1` both list it). Without them the installer stops and `status`
reports `bwrap_unsupported`. Only a passing probe on the target distro shows the sandbox
works there. `/usr/bin/python3` (root-owned, under `/usr`) runs the in-sandbox guard. `update.sh` installs
`ddistro_agent` and `install_assistant_runtime` as root-owned `0755` scripts but never
downloads the runtime or installs packages.

Replacing the runtime or any helper the attestation is bound to (the `ddistro` CLI included)
makes the probe `probe_stale`. So a regular installer run (last, after the database reader and
every runtime file are final) and `update.sh` (after every Core file is copied) each run
`install_assistant_runtime --reprobe` once. It does nothing without the installed runtime and
`ddagent`, and runs only the fixed root-owned `/usr/local/bin/ddistro agent probe`, under that
command's existing 600 s bound and process-group handling. It never downloads, signs in, or
touches provider credentials. The probe takes `serve.lock` without waiting, so a running session
is never stopped or replaced: the probe reports `busy` (exit `75`) and is not retried. A busy,
failed, or skipped probe only prints a warning and never fails the install or update. The
Assistant and its new tools stay off until the user ends the session and presses **Run
isolation check** (or runs `ddistro agent probe` as root). A passing probe re-enables the
legacy session, but `managed_exec_isolation` is optional, so `ddistro capabilities` is still what
shows whether the Assistant foundations tools are on. `--db-reader` exits before this step and
never probes.

## Identity and state

| Path | Owner and mode | Purpose |
|---|---|---|
| user `ddagent` | system user, `/usr/sbin/nologin`, home `/nonexistent`; its group `ddagent` has no members and is no other account's primary group | runs Codex |
| `/var/lib/dwemerdistro/assistant` | root `0755` | state root |
| `.../tasks` | `root:ddagent 0710` (installer) | Stage B tasks |
| `.../tasks/<UUID>` | `root:ddagent 0710` | one task |
| `.../tasks/<UUID>/task.json` | root `0600` | task metadata |
| `.../tasks/<UUID>/work` | `ddagent`, not world writable | scratch copy, `/work` in the sandbox |
| `.../codex-home` | `ddagent:ddagent 0700` | `CODEX_HOME`: sign-in and history; preserved by the installer |
| `.../agent/attestation.json` | root `0644` | probe attestation |
| `.../agent/threads/<UUID>.json` | root `0600` (refused if looser) | task -> thread mapping |
| `.../agent/serve.lock` | root `0600` | singleton lock for serve, probe, and home-config |
| `.../agent/quarantine/<id>/` | root `0700` | a quarantined account-home `config.toml` (unchanged) and its root `0600` `receipt.json` |

`task.json` is `{"schema_version": 1, "task_id": "<UUID>", "product": "<id>",
"workspace": "<label>"}`. The task ID must be a lowercase UUID. Each path component is
opened without following symlinks and must be root-owned and not group/other writable.
The metadata must be a regular root file with mode `0600` or stricter, and at most 64 KiB.

Bubblewrap resolves a `--bind-fd` source by path as `ddagent`, so `ddagent` must be able to
search every directory above `work`. `tasks` and each task folder are therefore exactly
`root:ddagent 0710`: the dedicated group can pass through to `work` but cannot list, read, or
change anything, and other users get nothing. Everything else in a task (`task.json`,
`base/`, `stages/`, `apply/` with its backups, `lock`) stays root `0600`/`0700`, so the agent
cannot read it. Any other mode, another group, a group-writable folder, or a symlink is
refused (`tasks_root_untrusted` or `task_untrusted`). `ddistro changes prepare` creates
`0710` task folders when its scratch account is `ddagent` with a dedicated group; the
`dwemer` fallback stays root `0700`. A task folder that is root `0700` (prepared before the
runtime was installed) is changed to `root:ddagent 0710` by serve. This happens only for
that one folder, and only after its metadata and its `ddagent`-owned `work` pass every
check; nothing is changed recursively.
If `work` is not owned by `ddagent` (for example, when it was prepared as `dwemer`
before this runtime was installed), serve refuses with `work_owner_mismatch`. Only
`task_id`, `product`, `workspace` (as a label), and `scratch: "/work"` reach the model,
as `/task/task.json`. Nothing in `task.json` is used as a host path.

The account home must not contain `config.toml`, `managed_config.toml`,
`requirements.toml`, `hooks.json`, `AGENTS.md`, `AGENTS.override.md`, or `.env` (Codex
loads `$CODEX_HOME/.env` into its environment before reading config). Serve refuses with
`account_home_unmanaged_config` instead of loading them.

### Account-home config repair

`home-config` (root) reports, and `home-config --repair --sha256 HASH` repairs, only one case of
that refusal: a `config.toml` whose whole content is a project-trust entry in Codex's format for the sandbox
workspace (who wrote it is not verified; only its exact content is), `[projects."/work"]` with `trust_level = "trusted"` (spaces and tabs around `=` and
blank lines before or after are allowed; nothing else, no comment, no CR). It must be `ddagent`'s
own regular file with mode `0600` or stricter, one link, and at most 4 KiB, and no other unmanaged
file may be present. Anything else is `refused` with a reason (`config_unknown_content`,
`config_symlink`, `config_not_regular`, `config_oversize`, `config_wrong_owner`,
`config_unsafe_mode`, `config_hard_linked`, `other_unmanaged_file`, ...) and left in place; the
content of a refused file is never returned or loaded. Status prints `{state, reason, file,
sha256, size, receipt}` and changes nothing; `sha256` is the proposal.

The repair takes the same `serve.lock` without waiting (`busy`, exit `75`, while a session or probe
holds it; the lock file is never replaced), refuses while any `ddagent` process exists
(`agent_running`), and needs the current file to still hash to the proposal (`proposal_changed`).
It then renames the file, unchanged (same inode, owner, and mode), into a new root `0700` folder
under `agent/quarantine` (root `0700`; at most 64 entries, `quarantine_full`) and writes a root
`0600` `receipt.json` with its sha256, size, owner, mode, and times. A root `0600`
`agent/home-config.pending` guard is written before the move and removed only after the moved file
is verified (same inode, one link, same sha256) and its receipt is written; while it exists serve,
probe, and home-config refuse with `home_config_repair_pending`. A file that changed is linked back
without overwriting anything (`config_changed`, guard removed); if that is impossible, or anything
fails after the move, the guard stays. Clearing it is an administrator step: inspect the receipt
folder, then remove the guard. Nothing is deleted, and sign-in
and history in the account home are not touched. The file is moved, not edited, so the workspace is
simply no longer marked trusted for Codex. The model has no path to this command; the launcher runs
it only after the user confirms its inline proposal.

The App Server writes that same trust entry during normal use, so serve also repairs it on its own:
once per start, while holding `serve.lock` and before the strict managed open, it inspects the home
exactly as `home-config` status does and, only if the state is `repairable` and no `ddagent`
process remains (waiting at most 5 seconds), runs the same quarantine (pending guard, byte-identical
move, receipt). It does not retry. Any other state, including an unknown, symlinked, or wrongly owned config,
other unmanaged files, a pending guard, or a failed or partial repair, changes nothing more, and
the strict open refuses as before (`account_home_unmanaged_config` or
`home_config_repair_pending`), so the launcher's manual path still applies. No setting is ever
allowed through, and sign-in and thread state are not touched.

## Sandbox

Serve and probe run as root only to validate state, open `work` and the account home as
`O_PATH` descriptors, and hold the lock. Codex is started as `ddagent` with no
supplementary groups, `no_new_privs`, no capabilities, no core dumps, umask `077`, and
its own process group. It runs in this unprivileged bubblewrap sandbox:

- `--unshare-all --share-net --die-with-parent --new-session --clearenv`
- read-only `/usr` (with `/usr/local` hidden), the root `/bin`/`/lib*` links, and the
  runtime directory
- a sanitized `/etc`: selected read-only files (`ssl`, `alternatives`, loader, `nsswitch`,
  `php`, and similar), plus generated `passwd`, `group`, `hosts`, and `resolv.conf`.
  There is no `shadow` and no WSL `/mnt` link.
- `/etc/codex/requirements.toml` and `/etc/codex/config.toml`, generated by
  `ddistro_agent`
- `--bind-fd` of the account home at `/agent/home` (`CODEX_HOME`) and the scratch copy
  at `/work`. bwrap mounts the resolved path and checks that the mount's device and inode
  match the descriptor.
- an empty read-only tmpfs at `/agent/home/tmp/arg0-linux`, so Codex uses the pinned
  runtime binary as its sandbox helper rather than an alias under `/agent` (see below)
- a guard runs first, before Codex or any probe command. It is root-supplied Python code,
  run by `/usr/bin/python3 -I -S` from the read-only `/usr`, as `ddagent`. It requires that
  the real, effective, and saved UID and GID are `ddagent`'s, that there are no supplementary
  groups, that `NoNewPrivs` is 1 and `CapEff` is 0, and that `/agent/home` and `/work` have the
  device and inode of the descriptors root opened. Then it execs the command; otherwise it
  exits `97` and nothing else runs.
- private `/proc`, `/dev`, `/tmp`, and `/home`. There is no `/mnt`, `/run`, `/var`,
  `/root`, Windows interop, PostgreSQL socket, or live product root.

The App Server keeps provider and sign-in network access. Native tools run inside Codex's
own Linux sandbox under the root-managed `dwemer-scratch` permission profile. That
profile, defined only in `requirements.toml` so the account-home user layer cannot
redefine it, sets:

- read: `:minimal`, `/usr`, `/etc`, the runtime, and `/task`
- write: the workspace root (`/work`) and `/tmp`
- deny: `/agent`
- tool network: disabled

Codex 0.163.0 tries to create a per-session helper alias directory,
`/agent/home/tmp/arg0-linux/codex-arg0*`, holding `apply_patch`, `applypatch`, and
`codex-linux-sandbox` symlinks to the runtime binary, and would use that alias as its
sandbox helper. When the system bwrap has no `--argv0` (Debian 12's `0.8.0`), the tool
sandbox execs the helper by that alias path, which is under the masked `/agent`, so every
sandboxed command and the `AGENTS.md` load at `thread/start` failed with `bwrap: execvp
/agent/home/tmp/arg0-linux/.../codex-linux-sandbox: Permission denied`. (Ubuntu 24.04's
bwrap `0.9.0` has `--argv0`, so the alias was never exec'd there.) The outer sandbox
therefore mounts an empty read-only tmpfs at `/agent/home/tmp/arg0-linux`. Codex cannot
create the alias, prints `WARNING: proceeding, even though we could not create PATH
aliases`, and uses the pinned runtime binary under the readable runtime directory as its
helper on every distro. No helper directory is added to the profile. Nothing under `/agent`
is readable in the Linux tool sandbox: not `auth.json`, sessions, logs, state databases,
`tmp`, or a listing of the home. There the `/agent` deny is a `000` read-only tmpfs.

The requirements have no managed `[permissions.filesystem] deny_read`. Codex rejects
startup if any managed `deny_read` root contains the helper directory, and it has no
setting to move that directory out of `CODEX_HOME`. So `/agent`, `/agent/home`, and
`/agent/home/tmp` cannot be managed denies. Exact managed denies such as
`/agent/home/auth.json` would be nested masks under the profile's read-only `/agent`
tmpfs, which Codex's Linux sandbox does not prune or support. The `/agent` deny instead
rests on the profile:

- it is defined only in `requirements.toml`;
- it is the only allowed and default profile;
- the account home cannot carry config;
- the broker refuses client permission and sandbox overrides.

The probe checks the deny at run time.

The requirements also set: approval policy `never`; `default_permissions =
"dwemer-scratch"` as the only allowed profile; no login shell; managed hooks only; and
plugins, apps, hooks, memories, multi-agent, browser/computer use, image generation, and
remote plugins disabled. App Server runs as `codex app-server --strict-config --listen
stdio://`.

## status and probe output

`status` prints `{"installed", "probe_passed", "provider": "codex", "version", "reason"}`
and exits `0`. `reason` is one of:

- `ready`, `probe_required`, `probe_stale`
- `not_installed`, `unsupported_platform`, `helper_untrusted`, `runtime_untrusted`,
  `runtime_hash_mismatch`
- `agent_identity_missing`, `agent_identity_unsafe`
- `bwrap_missing`, `bwrap_unsupported`, `python_untrusted`
- `state_untrusted`, `tasks_root_untrusted` (rerun the installer), `account_home_missing`,
  `account_home_untrusted`

`probe` prints `{"passed", "checks": {name: bool}, "diagnostics"}`. It exits `0` only when
every check passes, and only then writes the attestation. Any failure removes an existing
attestation. If a session holds the lock, it exits `75` with only `lock_available: false`
and leaves the attestation unchanged. `diagnostics` has, per step (`outer`, `guard`, `code_mode_host`, `tool`,
`app_server`, `cancellation`, or `setup`), the exit status (negative for a signal) and up to
20 redacted stderr lines from the last 4 KiB, plus the broker's failure reason. Once the App
Server exits or a reply times out, the remaining App Server checks fail immediately instead of
waiting for each timeout. Checks run real commands in the same sandbox serve uses, against a
throwaway task (a `probe-*` folder under the real `tasks`, `root:ddagent 0710`), account home,
world-readable host secret, world-writable fake live directory, and loopback listener:

| Check | Evidence |
|---|---|
| `outer_sandbox_isolated` | App Server context is `ddagent`, `NoNewPrivs`, `CapEff` 0, and sees no `/mnt`, `/run`, `/var`, or host secrets |
| `provider_network` | App Server context reaches `chatgpt.com:443` |
| `code_mode_host_executes` | the pinned code-mode host starts in the App Server context |
| `scratch_identity_enforced` | told to expect a different directory at `/work`, the guard exits `97` and the command never runs (no marker file) |
| `tool_sandbox_executes` | `codex sandbox -P dwemer-scratch --include-managed-config` really ran the probe script |
| `scratch_write` | tool wrote `/work`; root sees the file owned by `ddagent` |
| `live_write_denied` | writes to the fake live dir, `/task`, `/usr`, `/etc`, runtime, and account home failed; host side unchanged |
| `auth_read_denied` | auth canary, account home listing, and `/proc/*/root` paths unreadable; no secret-like variables in the tool environment |
| `host_secret_denied` | host secret, `/etc/shadow`, `/root`, `/var`, and product roots are absent |
| `windows_interop_denied` | no `/mnt`, `/run/WSL`, `/init`, `WSL_INTEROP`, or `cmd.exe` |
| `privilege_escalation_denied` | tool UID is `ddagent`, `NoNewPrivs`, no capabilities, and `sudo`/`su` fail |
| `database_access_denied` | no PostgreSQL socket; tool cannot reach the probe's loopback listener (reachable from the App Server context) or port 5432 |
| `tool_network_denied` | tool cannot reach `1.1.1.1:443` or `chatgpt.com:443` |
| `app_server_initialize`, `signed_out_state` | stdio `initialize`; `account/read` with no sign-in reports `account: null` |
| `managed_requirements_active` | `configRequirements/read` shows only `never` approvals and only `dwemer-scratch` |
| `client_bypass_rejected`, `thread_policy_enforced` | override attempts, and history reads of a thread that is not this task's, are refused; `thread/start` reports `dwemer-scratch`, `never`, `/work` |
| `orderly_shutdown`, `cancellation_cleanup` | EOF and SIGTERM both leave no `ddagent` process |
| `lock_singleton`, `account_home_stays_managed` | second lock fails; Codex did not create a user config |

The attestation is bound to the runtime and code-mode host hashes and file identities, the policy hash, the
`ddistro_agent` hash, the bubblewrap hash, the kernel release and version, and the
`ddagent` UID and GID. A change to any of them makes `status` report `probe_stale` and
makes serve refuse. When they are installed, it also binds the sha256 of `ddistro_run`, `ddistro_access`,
`ddistro_changes`, `ddistro_exec` and `ddistro` files, and it records, when its optional canary passed,
`managed_exec_isolation: true` and, when the `ddistro_run` canary passed, `live_run_isolation: true`;
`ddistro capabilities` uses those (see ASSISTANT.md). A root-owned attestation that is group- or other-writable is ignored.

## serve

`serve --task <UUID>` writes nothing to stdout except App Server JSONL. If it must refuse
(task, identity, runtime, attestation, lock, or account-home checks), it starts no model.
It writes `{"error": "assistant_unavailable", "reason": ...}` to stderr, answers up to 16
JSON-RPC requests received within 15 seconds with error `-32001` and `data.reason`, then
exits with status `3`. A singleton lock means one session runs at a time (`busy`).
Closing stdin, SIGTERM, SIGINT, or SIGHUP stops only the session's own process group and
the session's own `ddistro_read` runs (below); the broker returns, and the lease is released,
only after those runs have been stopped and reaped. Bubblewrap `--die-with-parent` tears down the sandbox. Exit status: `0` after an orderly
stop, `4` if the broker closed the session (`session_closed` reason on stderr).

The broker relays JSONL both ways and forwards only these client methods:

| Method | Rule |
|---|---|
| `initialize`, `initialized` (notification), `model/list`, `configRequirements/read`, `permissionProfile/list`, `account/rateLimits/read` | unchanged |
| `account/read` | `refreshToken` only |
| `account/login/start` | `type: "chatgpt"` (browser; `appBrand`, `codexStreamlinedLogin`, `useHostedLoginSuccessPage`) or `type: "chatgptDeviceCode"`; API-key and external-token sign-in are refused |
| `account/login/cancel`, `account/logout` | unchanged |
| `thread/start` | `model`, `developerInstructions`, `ephemeral`, `serviceName`, `personality`, `serviceTier` |
| `thread/resume` | the above plus `threadId` (must be one of this task's threads) and `excludeTurns` (absent or `true`; the broker sends `true`, `false` is refused) |
| `thread/read` | `threadId` (this task), `includeTurns` (absent or `false`; the broker sends `false`) |
| `thread/turns/list` | `threadId` (this task), `cursor`, `limit`, `sortDirection` (`asc`/`desc`), `itemsView` (`notLoaded`, `summary`, `full`) |
| `thread/items/list` | `threadId` (this task), `turnId`, `cursor` (a string, or `{"type": "item", "itemId"}` with a `turnId`), `limit`, `sortDirection` |
| `thread/list` | `cursor`, `limit`, `sortDirection`; answered by the broker from this task's thread mapping (below) |
| `turn/start` | `threadId` (this task), `input` (only `text` items and `image` items with a `data:image/` or `https://` URL), `model`, `effort`, `summary`, `outputSchema`, `personality`, `serviceTier`, `clientUserMessageId` |
| `turn/interrupt` | `threadId` (this task), `turnId` |
| `thread/archive`, `thread/unarchive`, `thread/delete` | `threadId` (this task) only; see "Thread archive and delete" |
| `ddistro/action/decide` | `proposalId`, `threadId`, `decision` (`approve`/`cancel`, and for a `live_run` proposal `approve_session`), `digest` (required to approve a `live_run` proposal, refused on any other); answered by the broker, never forwarded; see "ddistro_action" and "ddistro_run" |
| `ddistro/run/revoke`, `ddistro/run/cancel` | only after `live_run` was negotiated; see "ddistro_run" |

For thread and turn requests, `approvalPolicy` may only be `never`, and `cwd` only
`/work`; the broker sets both. `permissions` may only be `dwemer-scratch`.
`sandbox: "workspace-write"` and `sandboxPolicy: {"type": "workspaceWrite"}` (with no
writable roots, network, or read-only access) are accepted and removed, so the stronger
managed profile applies; any other sandbox value is refused. Every other method (`fs/*`,
`command/exec`, `config/*`, plugins, MCP, and the rest) and every other parameter is
refused with JSON-RPC `-32601`/`-32602`, without reaching Codex.

History is bounded and read only through this task's threads. A resume never hydrates turns;
clients page them with `thread/turns/list` and `thread/items/list`, which are the Codex
0.163.0 stable App Server methods with the parameters above. The broker always sends a
`limit`: turn pages default to 20 and allow up to 50 (5 and 10 with `itemsView: "full"`),
item pages default to 100 and allow up to 200. A limit outside that range, a cursor over 4096
characters, or an id over 128 characters is refused. Cursors are passed to Codex unchanged.

`thread/list` never reaches Codex, whose list covers every thread of the shared account
home. The broker pages the task's mapping, newest first by default (default 25, up to 50),
reads each listed thread with an internal metadata-only `thread/read`, and answers
`{"data": [Thread], "nextCursor", "backwardsCursor": null}` in the Codex `ThreadListResponse`
shape. `nextCursor` is the last listed thread id; a cursor that is not one of this task's
threads is refused. Threads Codex can no longer read (for example ephemeral ones) are left out,
so a page can be shorter than `limit`. Client request ids starting with `ddistro-broker:` are
reserved for these internal reads (and the broker's own `turn/interrupt`) and refused.

Client answers to approval, permission, elicitation, and token-refresh server requests are
replaced with an error. Dynamic-tool calls (`item/tool/call`) never reach the client (below). When `thread/start` or `thread/resume` succeeds, the
broker checks the reported `activePermissionProfile.id`, `approvalPolicy`, and `cwd`. A
mismatch closes the session (`policy_mismatch`). Started threads are recorded in the
task's root-owned mapping.

The broker does not log request or response bodies. App Server stderr is forwarded up to
64 KiB. Lines are limited to 8 MiB from the client and 32 MiB from the server.

### Response byte budget

Page sizes do not bound bytes: one tool item or full turn can exceed the launcher's 4 MiB line
limit. Any server line over 3 MiB is re-encoded with long strings clipped (65536, then 16384,
4096, 1024, 256 characters) until it fits. Each clipped string ends with `...[clipped by
DwemerDistro: N of M characters omitted]`, and the `result` (or notification `params`) gets
`ddistroTruncation: {clippedStrings, maxStringChars, budgetBytes}`. Ids, `type`, `status`,
`role`, `method`, cursors, and keys ending in `Id` are never clipped, and no item or page is
dropped, so pagination is unchanged. A response that still does not fit, or a server line over
32 MiB that starts with its `id` and `result`/`error` (discarded unparsed, the session stays
up), is answered with JSON-RPC `-32003` and
`data: {"reason": "response_too_large", "budgetBytes"}`; ask for a smaller `limit` or
`itemsView: "summary"`. Any other server line over 32 MiB (a notification, a request, or a
response whose `id` is not first) cannot be dropped safely, and an oversized response is
discarded for at most 256 MiB in total. In either case every request the broker tracks
(thread start/resume, history reads, `thread/list`) is answered with `-32003` and
`data.reason: "server_message_too_large"`, and the session closes with that reason (exit 4).
An unfittable notification under 32 MiB is dropped; an unfittable server request is
answered with an error to Codex. `thread/read` replies and `thread/list` entries always carry
`turns: []`, and listed thread metadata strings are clipped to 4096 characters.

### ddistro_read dynamic tool

When the client's `initialize` has `capabilities.experimentalApi: true` (a boolean; anything
else is refused), every `thread/start` gets the broker's own declaration
`dynamicTools: [ddistro_read]`. Clients cannot send `dynamicTools`; older clients that do not
opt in get no tool and are otherwise unchanged. Codex 0.163.0 refuses `dynamicTools` without
that capability. The declaration (`TOOL_SPEC` in `bin/ddistro_agent`) is one function with
strict input `{"operation": "inventory"|"status"|"models"|"logs"|"health"|"performance"|"model_options"|"update_check", "product"?,
"log_source"?: "apache"|"postgres", "log"?, "lines"?: 1-400, "query"?: <= 200 chars,
"since"?: "YYYY-MM-DDTHH:MM:SSZ"}` and `additionalProperties: false`. `product` lists servers only
(`herika`, `lorkhan`, `stobe`, `dialectic`, `reign`); the shared Apache and PostgreSQL logs are
`log_source`. The schema is flat (no `oneOf` or conditionals, which the native tool API does not
accept), so pairing rules are checked at run time. Threads started before `log_source` were declared
`product` `apache`/`postgres`; the broker still accepts that for `logs` only.

The broker answers `item/tool/call` itself and never forwards it, so a client cannot see or
answer the request. It checks that the call is for `ddistro_read` with no namespace, from one
of this task's threads, and that the arguments match exactly: `inventory` takes nothing;
`status`/`health`/`performance`/`update_check` take an optional server product (`apache` or
`postgres` there is refused with a message pointing to `logs` with `log_source`); `models` and
`model_options` (`ddistro models options herika`) take only `herika`; `logs`
needs a server product or a `log_source` (not both; the older `product` `apache`/`postgres` form
is still accepted), an optional log name from that target's
`ddistro logs` allowlist, `lines` (default 50), and the optional `query` (one line of plain text,
not `--json` or `--request-id`) and `since` (a valid UTC time in exactly that form), passed as
`--query` and `--since`. Then it runs, as root and outside the
model sandbox, exactly `/usr/local/bin/ddistro <operation> [args] --json`. The helper must
be root-owned along its whole path. It runs with no stdin, a fixed `PATH`, and no other
environment except a numeric `DDISTRO_POSTGRES_PORT`. It may write at most 1 MiB of stdout
and run for 90 seconds, with at most two calls at a time. Its envelope must have
`schema_version` 1, the expected `command`, and `ok`, with `data` as an object on success.
Each run is its own process group, owned by the session: when the session ends (stdin
closed, a signal, App Server exit, or a broker failure) no new run starts, running groups get
SIGTERM and after 2 seconds SIGKILL, and the broker waits for them to be reaped and their
threads to finish before it returns. A group is signalled only while its leader is unreaped,
so no other process is ever signalled. A cancelled run is not answered.

Tool use is bounded per turn and per session so a model stuck in a read loop stops: at most 16
calls and 512 KiB of returned text per turn (a whole-distro check is about 4-10 calls), and 256
calls per session. Calls past a limit run nothing and return `success: false` with error code
`turn_tool_limit` or `session_tool_limit` and a message telling the model to answer from what
it has. If a turn reaches 32 calls anyway, the broker sends `turn/interrupt` for it once. The
client can interrupt a turn at any time with `turn/interrupt`. Budgets are kept for the 64 most
recent turns of this task's threads.

The reply is the native `{"success", "contentItems": [{"type": "inputText", "text"}]}`. `text`
is JSON: `{"tool", "operation", "captured_at", "ok", "data"|"error"}`. Control characters, URL
queries, and secret-shaped values are redacted in every string, and the text is clipped to
64 KiB the same way as above. A `logs` reply that would not fit unclipped drops its oldest
lines first and says so in `data.budget_trimmed_lines` and `truncated: true`.

Every reply also has `coverage`, computed by the broker from its own counters, never by the model:
`{result, turn_calls, turn_calls_left, turn_output_kib_left, session_calls_left, health?, performance?}`.
`result` is `checked`, `truncated` (clipped, budget-trimmed, a log filter whose `complete` is false, `models`
with `truncated` not false, `inventory` with `complete` not true or a truncated section, or
`performance` with `complete` not true),
`failed` also for `models` with `available` not true, `skipped` also for a log not written yet,
`refused` (`invalid_arguments`), `budget_hit` (`turn_tool_limit`, `session_tool_limit`), `skipped`
(`busy`, `cancelled`), or `failed`; a health reply adds its `passed`/`failed`/`skipped` counts and
`checks` (at most 64 `{id, result}` pairs in ddistro's id shape, results only `passed`, `failed`, or
`skipped`) with `checks_complete` (false when an entry was left out), so overlapping reads such
as `health herika` then `health` can be counted as unique checks rather than added summaries; a
performance reply adds its `measured`/`failed`/`skipped` counts. The client sees the same text in
the `dynamicToolCall` item, so the launcher can show a partial-check notice from it. A refused call returns `success: false` with error code
`invalid_arguments` and runs nothing. No write, repair, update, approval, or escalation is
reachable through the tool. Codex reports the call to the client as its usual
`dynamicToolCall` item notifications.

### ddistro_memory dynamic tool

When the session starts, `serve` runs `ddistro memory status --json` (the same trusted-helper
rules as below; it never creates the database). Only if that succeeds with `settings.retrieval`
true does every `thread/start` of an opted-in client get `dynamicTools: [ddistro_read,
ddistro_memory]`; otherwise only `ddistro_read` is declared and `ddistro_memory` calls are
refused as an unknown tool. A setting changed during a session takes effect for the
declaration in the next session, but the CLI checks the current settings on every call.

The declaration (`MEMORY_SPEC`) is strict: `{"operation": "search"|"remember", "query"?:
<= 200 chars, "text"?: <= 2000 chars, "kind"?: "preference"|"decision"}` with
`additionalProperties: false`. The broker accepts exactly `search` with `query`, or `remember`
with `text` and `kind`; any other key or operation (forget, reset, configure, task, thread),
empty or over-long values, control characters, a value of `--json` or `--request-id`, a
foreign thread, or a namespace runs nothing and returns `invalid_arguments`. It then runs:

- `ddistro memory search --assistant --query Q --limit 5 --json`, refused by the CLI with
  `disabled` when retrieval is off;
- `ddistro memory add --source assistant --kind K --text T --task TASK --thread THREAD --json`,
  where `TASK` is this session's task and `THREAD` the calling thread, both from the broker,
  never from the model. The CLI refuses it with `disabled` unless both retrieval and automatic
  learning (both on by default) are on, refuses secret-shaped text, and refuses a note that
  claims a verified fix.

Memory is automatic: the model searches and remembers on its own under the launcher's
instructions, and the user has no note editor. Configuration, forget, update, reset, and export
are never reachable from the model. The launcher runs `memory configure` (both on) as it
connects, before this broker starts, and offers the user only Clear memory (`memory reset
--yes`, after a confirmation); the other actions remain CLI controls for debugging. Memory calls share the `ddistro_read` children, process
group supervision, concurrency, 90 s / 1 MiB bounds, reply envelope and redaction, and the same
per-turn and per-session budgets (the two tools count together). Recalled notes are returned
as tool data marked `non_authoritative`; a note cannot change the policy, the sandbox, the
declarations, or what a tool may do, and it never replaces fresh `ddistro_read` evidence.

### ddistro_action live action

The live actions. A client opts in with `initialize` `capabilities.ddistroActions`, a list of
distinct names from `"restart_service"` and `"configure_model"` (with `experimentalApi: true`);
the broker removes the field before Codex sees it, and any other value is refused. Only then
does `thread/start` also declare `ddistro_action`. A client that sends only
`["restart_service"]` gets exactly `ACTION_SPEC`: strict `{"operation": "propose_restart",
"service": "apache", "reason": <= 300 chars}`, and can never be sent a model proposal.
`configure_model` adds `propose_model_change` (`profile_id`, `role`, `connector_id`, `reason`) and
`propose_model_rollback` (`reason` only) to a flat schema whose per-operation fields are checked
at run time. Threads started without the opt-in never get the tool, and older clients are
unchanged.

A model proposal assigns one fixed CHIM profile role to an existing connector and nothing else.
The broker, not the model, runs the root `ddistro models preview herika ... --task T --thread TH`
(or `rollback-preview --task T --thread TH`, the undo of this thread's own latest change; the
model never supplies or sees a receipt id or `plan_sha256`) and sends the client the validated
diff in `ddistro/action/proposed` `change`. After `approve`, it runs exactly `ddistro models
apply|rollback herika --receipt ID --plan-sha256 HASH` once; the preview's 120 s expiry and its
compare-and-swap still apply, so an approval after a concurrent edit is a `conflict` and writes
nothing. Approval, expiry, cancellation, and one-per-turn rules are as below. The model gets
`verified`, `unverified`, `conflict`, `not_executed`, `unknown`, or a refusal; a reply whose
receipt id does not match the proposal is `unknown`. `verified` means a fresh database read
shows the assignment: the provider, the model, and in-game replies are not tested.

The model can only propose. On a valid call from one of this task's threads, with a `turnId`,
at most one `propose_restart` per turn, one of each permitted model operation per turn, and
one pending per session, the broker records an in-memory proposal
with a random id and a 60 second expiry, and sends the client `ddistro/action/proposed`
`{proposalId, threadId, turnId, action: "restart", service, label, impact, reason, expiresAt}`.
The tool call waits; the model never sees the id. Only the client's `ddistro/action/decide`
for that id and thread, while it is still pending and unexpired, changes it, once, under the
broker lock: a replay, a late or second decision, or an unknown id is refused with `-32001`
and runs nothing. `turn/start` or `turn/interrupt` of that thread, any `thread/start` or
`thread/resume`, client EOF, a signal, or the expiry ends a pending proposal with nothing run.

After `approve`, if the session is not closing, the broker runs exactly `/usr/local/bin/ddistro
service restart <service> --json` as root, under the same trusted-helper rules as
`ddistro_read` (90 s, 1 MiB). It is not adopted into the session's stoppable runs: once
started, a restart is allowed to finish, because stopping it midway could leave the service
down. Session teardown waits for it (bounded by the 90 s run limit) so its result is still sent;
the root CLI runs in its own session and holds the maintenance lock until it finishes either way.
The client then gets `ddistro/action/updated` `{proposalId, threadId, state, executed,
verified, checks}`, and the model gets `{tool, operation, service, captured_at, state,
proposed, user_approved, executed, verified, data|error}`. `state` is `refused` (invalid or a
second proposal this turn), `busy`, `cancelled`, `expired`, `not_executed` (approved but
refused before running), `failed` (ran, or may have run when stopped at the 90 s limit or its
result was unreadable; the fresh check did not pass), or `verified` (ran; a
new process and every configured port answered). `success` is true only for `verified`. The
model has no path to approve, choose a command, a unit, or a path, or reach any other service.

### Assistant foundations (tool revisions 11/12)

The Launcher uses tool revision 11 (12 with memory) only when `ddistro capabilities` reports
`features.assistant_foundations: true` and lists `ddistro_explore` and `ddistro_exec`. The flag
needs every helper trusted and a passing probe (see ASSISTANT.md). Otherwise the Launcher stays on
revision 9/10 or lower, and those revisions behave exactly as described above.

**Negotiation.** With `experimentalApi: true`, a revision 11/12 client sends
`capabilities.ddistroActions: ["restart_service", "configure_model", "change_files",
"service_control"]` and `capabilities.ddistroTools: ["explore", "exec"]`. The broker removes both
fields before Codex sees them and refuses any value it does not know. Tools are declared on
`thread/start` only; `thread/resume` never adds one. A saved thread from a lower revision is
upgraded once, to a new thread.

**Tools.**

- `ddistro_explore` is read-only. It covers roots, file list, search and read, data-only config,
  database schema, and structured database queries with fixed operators. It never runs raw SQL.
  With `change_files`, it also has `checkout`, which copies files into the task's scratch folder
  and never writes to a live root.
- `ddistro_exec` runs `ddistro exec` in the no-network sandbox on redacted, read-only snapshots.
  The output describes those captured copies, not the live WSL host. The model checks live state
  with `ddistro_read` and `ddistro_explore`.

**Proposals.** The model can only propose. The broker builds every proposal from a root
`ddistro` preview, and the model never supplies a hash, a receipt id or a command.

- `propose_file_change` stages the task's scratch edits with `ddistro changes stage`. It can cover
  several roots, but only roots that `access roots` lists with `change: true`. The card shows the
  manifest hash and the complete diff (at most 4000 lines and 256 KiB). A larger or truncated diff
  is refused, never shortened.
- `propose_file_undo` uses `changes rollback-preview`, and approval runs `changes undo` with its
  preview hash.
- `propose_service` uses `ddistro operations preview` for one target and verb from the typed matrix
  (Apache restart, or a server install or update on an allowed branch). The card shows the target,
  verb, branch and the 8 summary lines. Approval runs `operations apply` once with that receipt and
  `plan_sha256`. Start, stop, PostgreSQL and the components are unsupported.

**Approval and status.** Approval and cancellation work as for restarts. `ddistro/action/decide`
`approve` or `cancel` is accepted once per proposal. A cancel, an expiry, a new turn, or the end of
the session runs nothing. The client gets `ddistro/action/updated` with `state` and `verified`:

| State | Meaning |
|---|---|
| `verified` | The change ran and was checked. |
| `failed` | The change ran or may have run, and the check did not pass. |
| `unverified` | The change ran, but the check could not confirm it. |
| `conflict`, `stale` | Something changed since the preview. Nothing was overwritten. |
| `cancelled`, `expired` | Nothing ran. |
| `busy` | Another operation held the lock. |
| `not_executed`, `refused` | Nothing ran. |
| anything else | The outcome is unknown. |

A receipt left `executing` is reported as unknown and is never run again. Running a used receipt
again only returns its recorded result.

### ddistro_run live commands (live_run)

`ddistro_run` runs code the model wrote on the LIVE distro: the real WSL filesystem, services and databases. It is
not a sandbox and not a snapshot. It exists so the Assistant can do general work (edit a setting in a product
database, install a package, inspect any path) that the typed tools do not cover. Its safety rests on the user's
approval of the exact request, not on an allowlist: there is no program, path, SQL or per-mod allowlist, no
read-only tier and no SQL preview. A command meant only to read can still change things, so a run is never let
through because the model says it only reads; the typed read tools (`ddistro_read`, `ddistro_explore`) stay the
automatic way to look.

**Availability.** The client adds `"live_run"` to `initialize` `capabilities.ddistroActions` (with
`experimentalApi: true`). It is honored only when, as the session starts, the attestation is current, binds the
sha256 of `/usr/local/bin/ddistro_run`, and records its canary as `optional_checks.live_run_isolation: true`
(see the probe below), and the file still has that digest; it is checked again before every run. Otherwise
`live_run` is dropped silently, like `change_files`. Only then does `thread/start` declare the `ddistro_run`
tool (`RUN_SPEC`). It is its own tool, not a `ddistro_action` operation. `ddistro capabilities` reports
`features.live_run` and `live_run.reason`.

**Tool.** `operation` `run`, `status`, `output`, `cancel` or `list`.

- `run`: `interpreter` (`bash`, `python3`, `php`, `psql`), `code` (at most 128 KiB), `reason` (shown to the user),
  and optionally `account` (default `dwemer`; any local account but `ddagent`), `cwd` (absolute; default the
  account's home), `network` (default `false`), `timeout_seconds` (1-3600, default 300), `database` (psql only),
  `wait_seconds` (0-60, default 20). Code with control characters other than tab, newline and CR, or with
  bidirectional or zero-width characters, is refused, so the reviewed text is what runs.
- `status` (`job_id`, `wait_seconds`), `output` (`job_id`, `offset`, `max_chars` up to 48 KiB), `cancel`
  (`job_id`), `list`. A job is visible only to the thread that started it.

**Approval.** With no covering grant, the broker sends `ddistro/action/proposed` with `action: "live_run"`, an
impact text (root: full trust), `decisions: ["approve", "approve_session", "cancel"]`, and `change`: the exact
`code`, `interpreter`, `account`, `cwd`, `network`, `timeout_seconds`, `database`, `code_sha256`, `digest` (the
sha256 of the exact request bytes the supervisor will read), `scope`, the current `grant` and `widens_grant`. It
expires after 600 s. The client answers `ddistro/action/decide` with that `digest` (required to approve; a
missing or different digest is `-32602` and changes nothing). `approve` runs it once. `approve_session` (Allow for
this task) runs it and adds its exact `(account, network)` scope to the session grant, installed under the same
lock as the decision so a later revoke always wins: later runs that one scope covers (same account, or root, at
the same or a wider network tier) run without a card. Scopes are never combined. A request no scope covers shows
a new card with `widens_grant: true`; approving it with `approve_session` adds one more scope. The grant lives only in the broker's memory for
this session and its task's threads; it is never stored and ends when the session ends or the client sends
`ddistro/run/revoke` (`{}` -> `{revoked}`; running jobs continue). `ddistro/run/grant` `{active, scope, reason}`
reports every change. The model has no path to approve, grant or revoke. Replay, late, unknown and foreign-thread
decisions are refused as for other proposals, and `turn/start`, `turn/interrupt`, `thread/start`,
`thread/resume`, expiry or the end of the session end a pending card with nothing run. One proposal of any kind is
pending at a time.

**Execution.** For an approved or covered request the broker checks the common maintenance lock
(`/run/lock/dwemerdistro-maintenance.lock`, used as it is, never replaced) by taking it shared without waiting and
releasing it at once: a run does not start while an exclusive change holds it (`busy`). The lock is not held for
the job, so the command's own `ddistro` changes take it as usual. A Stop, archive or delete of the thread after
the approval but before the job is registered means the run never starts. At most 4 jobs run at once per
session. The broker then sends `ddistro/run/started` `{threadId, turnId, jobId,
approval: user_once|user_task_grant|task_grant, digest, state: "starting", spec}` with the exact code, and starts
`/usr/local/bin/ddistro_run supervise` in its own session and process group, writing the request bytes to its
stdin. The root supervisor:

- checks the request again, sets its parent-death signal (the broker's death, even SIGKILL, stops the job) and
  puts the code in a sealed memfd, so it reaches the interpreter as `/dev/fd/9`, never on a command line;
- forks the command, which gets a private mount namespace in which `/mnt` (Windows drives, WSLg), `/run/WSL`
  (interop), `/init` and `/var/lib/dwemerdistro/assistant` (the Codex sign-in, memory, receipts, task and thread
  state) are hidden, and, with `network: false`, an empty network namespace with only its own loopback (no
  internet, no TCP to local services; Unix sockets such as PostgreSQL's work). These masks prevent accidents only:
  a root command can undo them. Product configuration is not hidden, so it can be edited;
- drops to the account (its groups, uid and gid) with no_new_privs unless it is root, so sudo, su and other setuid
  programs cannot raise a non-root run; root keeps every capability;
- runs it with stdin `/dev/null` and a fixed environment (`DEBIAN_FRONTEND=noninteractive`, `PAGER=cat`,
  `GIT_TERMINAL_PROMPT=0`, no WSL or Windows variables), and reports `started` with the digest of what it read;
  the broker stops the job unless that digest is the approved one;
- on timeout or cancel sends SIGTERM to the command's process group and SIGKILL after 5 s; when the command
  ends, whatever is left in its group is stopped too. Processes that left the group (a service started through
  its init script, setsid or start-stop-daemon) keep running by design and should redirect their output.

**Output and lifecycle.** stdout and stderr are read by the broker, redacted (ddistro_access's redaction, applied
to whole blocks so multi-line keys are caught), control characters replaced, lines clipped at 4096 characters and
stderr lines prefixed `[stderr] `. A job keeps the newest 512 KiB of characters; offsets are absolute. The client
gets `ddistro/run/output` `{threadId, jobId, offset, text, notStreamed}` at most 10 times a second, 16 K
characters each and 64 K per second (the rest stays in the ring), and `ddistro/run/updated` `{threadId, jobId,
state, started, exitCode, signal, succeeded, verified: false, reason, durationMs, outputChars}`. States:
`running`, `exited`, `timed_out`, `cancelled`, `killed`, `unknown` (no report from the supervisor; never treated
as success), and `refused` / `not_started` / `busy` (nothing ran). `turn/interrupt` (Stop), `thread/archive`,
`thread/unarchive` and `thread/delete` stop that thread's jobs, the end of the session stops all of them,
`ddistro/run/cancel` `{threadId, jobId}` stops one; a stop is SIGTERM to the group, then SIGKILL after 10 s. Jobs
otherwise outlive the turn, so a later turn of the same thread can ask for their status.

**Model replies** say `state`, `started`, `approval` and `user_approved`, and for a job `exit_code`, `signal`,
`succeeded` (exited 0) and `verified: false` with the note that only the exit status is known, the redacted
output tail (24 K characters) and a `hint`. The broker never retries; a cancelled, timed-out, killed or unknown
run may have changed things partway and is not rerun. There is no undo: the tool tells the model to back up a file
before editing it and to read the result back. ddistro_run calls have their own budget of 32 per turn (polling a
long job is expected), count toward the 256 per session, and run outside the diagnostic slots (at most 4 calls at
once).

**Probe canary** (`live_run_isolation`, optional like `managed_exec_isolation`, so a failure only turns
`live_run` off): it runs the installed, bound `ddistro_run` twice as `nobody` without network, changing nothing.
The first run must start with the expected digest, read its code from `/dev/fd/`, keep the code out of its
command line, run as `nobody` with no_new_privs, see `/mnt`, `/run/WSL` and the assistant state empty and `/init`
masked, have no WSL variables and an empty stdin, and reach neither the probe's host loopback listener (which the
outer check reached) nor `1.1.1.1:443`. The second must time out after 2 s with its background child gone and
its output closed without help from the probe.

### Thread archive and delete

`thread/archive`, `thread/unarchive`, and `thread/delete` (Codex 0.163.0 stable,
`{"threadId"}` params, `{}` result) are forwarded only for one of this task's threads, with no
other parameter. The launcher sends them only from an explicit user click (delete after a
confirmation); the model has no path to them. Archive and unarchive keep the thread in the
task mapping. When Codex answers a delete with a result, the broker removes exactly that id
from the task's thread mapping, so it is no longer readable, resumable, or listed; a failed
delete changes nothing. Neither touches other threads, the account home or sign-in, the task's
`task.json`, scratch, change receipts, or backups. Deleting a thread does not forget memory
notes captured in it: that is a separate, optional user action, `ddistro memory forget
--thread ID` (or `--task TASK`), which the launcher may offer next to the delete confirmation,
unchecked by default. `ddistro capabilities` reports this as `features.thread_lifecycle: true`,
a static fact that adds no command and opens no database.

## Known limitations

- `ddistro_run` is not isolation. An approved root run can read or change anything in the distro, including
  undoing its own masks (for example through `/proc/1/root` or by unmounting), and a non-root run has everything
  its account has. The PID namespace is shared on purpose (services, `ps`, init scripts), so a command can see and
  signal other processes its account may signal. Network off is all or nothing: there is no per-host rule.
- A `ddistro_run` job's output is redacted line by line with patterns; a secret in an unusual format can pass.
  Grant-covered runs show no card; the launcher shows them from `ddistro/run/started`.
- A background process a job leaves in its own session keeps running after the job, and if it kept the job's
  stdout or stderr, it gets an error writing to them once the broker stops reading.

- Codex 0.163 permission profiles are beta. The probe, not the configuration, is the
  evidence. Until a passing probe exists on the target distro, serve refuses.
- The App Server process itself shares the WSL network namespace, so it can reach
  loopback services. Only its sign-in and provider traffic use that network. Tools cannot.
- `thread/resume` of a persisted thread and real turns need a signed-in account and are
  not covered by the no-inference probe. The probe also does not read history pages; it only
  checks that reads of another thread are refused.
- Memory note text travels as argv to the root `ddistro` child, so for that child's
  lifetime it is visible in `/proc/<pid>/cmdline` to processes outside the sandbox that can
  read it; nothing logs it. Secret-shaped text is refused before it is stored.
- The account-home repair removes the trust entry but does not stop it from being written again (the source of the original file is unverified).
  An account-free `thread/start` with the pinned runtime did not write one (fixture), so what
  wrote it is not known; prevention through the managed config was not tested and is not done.
- `ddistro_action` holds Codex's dynamic tool call open while the user decides (at most 60 s).
  An approved legacy restart then takes up to about 75 s with its check; an approved
  `propose_service` waits at most 600 s, the CLI's existing bound for `changes` and the probe,
  and past it reports an unknown outcome without stopping `ddistro_server` or re-running the
  receipt. Whether Codex 0.163.0 bounds a dynamic tool call more tightly was not measured; if
  it does, the turn ends first and a pending proposal expires with nothing run.
- The task mapping keeps the 256 most recently started threads in start order, so `thread/list`
  orders by start time and cannot list older threads. A thread started between two pages
  shifts nothing, because the cursor is a thread id, not an offset.
