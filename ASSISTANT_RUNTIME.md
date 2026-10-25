# Assistant runtime (Stage C, native Codex)

`bin/ddistro_agent` runs the pinned native Codex App Server for one prepared task inside
a dedicated, attested sandbox. The model can change only the task's scratch copy.
Applying scratch changes to a live install is a separate, trusted launcher/manual
operation and is never done by this tool. A task ID or `--yes` grants nothing.

```bash
install_assistant_runtime [--archive PATH] [--install-prerequisites]   # root, explicit
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

The installer requires root and x86_64. It checks the archive hash before listing or
extracting it, accepts only the single expected regular file, checks the extracted
binary hash, and installs atomically. Rerunning it changes nothing when the binary
already matches. It never installs packages unless `--install-prerequisites` is given.
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

Codex 0.163.0 always creates a per-session helper alias directory,
`/agent/home/tmp/arg0-linux/codex-arg0*`, holding `apply_patch`, `applypatch`, and
`codex-linux-sandbox` symlinks to the pinned runtime binary plus a lock file. Codex adds
that one directory as a read-only root of the active profile. It is the most specific
entry, so it overrides the `/agent` deny for Codex itself. Nothing else under
`/agent/home` is readable: not `auth.json`, sessions, logs, state databases, `tmp`, or a
listing of the home. In the Linux tool sandbox the `/agent` deny is a `000` read-only
tmpfs mounted after the read roots, so tools cannot see the helper either. Codex re-execs
its sandbox stage through the runtime binary, not the alias.

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
and leaves the attestation unchanged. `diagnostics` has, per step (`outer`, `guard`, `tool`,
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
| `client_bypass_rejected`, `thread_policy_enforced` | override attempts are refused; `thread/start` reports `dwemer-scratch`, `never`, `/work` |
| `orderly_shutdown`, `cancellation_cleanup` | EOF and SIGTERM both leave no `ddagent` process |
| `lock_singleton`, `account_home_stays_managed` | second lock fails; Codex did not create a user config |

The attestation is bound to the runtime hash and file identity, the policy hash, the
`ddistro_agent` hash, the bubblewrap hash, the kernel release and version, and the
`ddagent` UID and GID. A change to any of them makes `status` report `probe_stale` and
makes serve refuse. A root-owned attestation that is group- or other-writable is ignored.

## serve

`serve --task <UUID>` writes nothing to stdout except App Server JSONL. If it must refuse
(task, identity, runtime, attestation, lock, or account-home checks), it starts no model.
It writes `{"error": "assistant_unavailable", "reason": ...}` to stderr, answers up to 16
JSON-RPC requests received within 15 seconds with error `-32001` and `data.reason`, then
exits with status `3`. A singleton lock means one session runs at a time (`busy`).
Closing stdin, SIGTERM, SIGINT, or SIGHUP stops only the session's own process group.
Bubblewrap `--die-with-parent` tears down the sandbox. Exit status: `0` after an orderly
stop, `4` if the broker closed the session (`session_closed` reason on stderr).

The broker relays JSONL both ways and forwards only these client methods:

| Method | Rule |
|---|---|
| `initialize`, `initialized` (notification), `model/list`, `configRequirements/read`, `permissionProfile/list`, `account/rateLimits/read` | unchanged |
| `account/read` | `refreshToken` only |
| `account/login/start` | `type: "chatgpt"` (browser; `appBrand`, `codexStreamlinedLogin`, `useHostedLoginSuccessPage`) or `type: "chatgptDeviceCode"`; API-key and external-token sign-in are refused |
| `account/login/cancel`, `account/logout` | unchanged |
| `thread/start` | `model`, `developerInstructions`, `ephemeral`, `serviceName`, `personality`, `serviceTier` |
| `thread/resume` | the above plus `threadId` (must be one of this task's threads) and `excludeTurns` |
| `turn/start` | `threadId` (this task), `input` (only `text` items and `image` items with a `data:image/` or `https://` URL), `model`, `effort`, `summary`, `outputSchema`, `personality`, `serviceTier`, `clientUserMessageId` |
| `turn/interrupt` | `threadId` (this task), `turnId` |

For thread and turn requests, `approvalPolicy` may only be `never`, and `cwd` only
`/work`; the broker sets both. `permissions` may only be `dwemer-scratch`.
`sandbox: "workspace-write"` and `sandboxPolicy: {"type": "workspaceWrite"}` (with no
writable roots, network, or read-only access) are accepted and removed, so the stronger
managed profile applies; any other sandbox value is refused. Every other method (`fs/*`,
`command/exec`, `config/*`, plugins, MCP, and the rest) and every other parameter is
refused with JSON-RPC `-32601`/`-32602`, without reaching Codex.

Client answers to approval, permission, dynamic-tool, elicitation, and token-refresh server
requests are replaced with an error. When `thread/start` or `thread/resume` succeeds, the
broker checks the reported `activePermissionProfile.id`, `approvalPolicy`, and `cwd`. A
mismatch closes the session (`policy_mismatch`). Started threads are recorded in the
task's root-owned mapping.

The broker does not log request or response bodies. App Server stderr is forwarded up to
64 KiB. Lines are limited to 8 MiB from the client and 32 MiB from the server.

## Known limitations

- Codex 0.163 permission profiles are beta. The probe, not the configuration, is the
  evidence. Until a passing probe exists on the target distro, serve refuses.
- The App Server process itself shares the WSL network namespace, so it can reach
  loopback services. Only its sign-in and provider traffic use that network. Tools cannot.
- `thread/resume` of a persisted thread and real turns need a signed-in account and are
  not covered by the no-inference probe.
