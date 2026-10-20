# Assistant runtime (Stage C, native Codex)

`bin/ddistro_agent` runs the pinned native Codex App Server for one prepared task inside
a dedicated, attested sandbox. The model can change only the task's scratch copy.
Applying scratch changes to a live install is a separate, trusted launcher/manual
operation and is never done by this tool. A task ID or `--yes` grants nothing.

```bash
install_assistant_runtime [--archive PATH] [--host-archive PATH] [--install-prerequisites]   # root, explicit
ddistro_agent status            # any user; one JSON line
ddistro_agent probe             # root; real sandbox probe; writes the attestation
ddistro_agent serve --task ID   # root; raw Codex App Server JSONL on stdin/stdout
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
`ddistro_agent` and `install_assistant_runtime` as root-owned `0755` scripts but never runs
the installer, downloads the runtime, or installs packages.

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
| `.../agent/serve.lock` | root `0600` | singleton lock for serve and probe |

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
makes serve refuse. A root-owned attestation that is group- or other-writable is ignored.

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
strict input `{"operation": "inventory"|"status"|"models"|"logs"|"health", "product"?, "log"?,
"lines"?: 1-100}` and `additionalProperties: false`.

The broker answers `item/tool/call` itself and never forwards it, so a client cannot see or
answer the request. It checks that the call is for `ddistro_read` with no namespace, from one
of this task's threads, and that the arguments match exactly: `inventory` takes nothing;
`status`/`health` take an optional server product; `models` takes only `herika`; `logs`
needs a server product, `apache`, or `postgres`, an optional log name from that target's
`ddistro logs` allowlist, and `lines` (default 50). Then it runs, as root and outside the
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
64 KiB the same way as above. A refused call returns `success: false` with error code
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
- The task mapping keeps the 256 most recently started threads in start order, so `thread/list`
  orders by start time and cannot list older threads. A thread started between two pages
  shifts nothing, because the cursor is a thread id, not an offset.
