# Shared Assistant API v1

Core owns the native Codex broker workers. The Launcher attaches through
`ddistro_api bridge --task UUID` over a root-only Unix HTTP socket. Mods use
HTTP at `http://127.0.0.1:18423/v1` with a separately provisioned bearer token.
The public listener never exposes the Launcher protocol relay.

## Lifecycle and installation

Install `bin/ddistro_api` as `/usr/local/bin/ddistro_api`, root:root 0755.
The updater installs it alongside the existing broker. `ddistro_api ensure`
starts the detached supervisor if needed. The Launcher relay calls ensure;
`start_env` also calls it when an Assistant attestation exists. No provider
sign-in or dependency installation happens automatically.

The supervisor takes the existing `serve.lock` before opening either endpoint.
Legacy direct sessions and probes report busy while it owns that lock. Session
workers inherit the owner's descriptor; they cannot create a competing owner.
At most four task-scoped workers coexist, with one active model turn across
all clients and a FIFO queue of eight reservations. Detached live runs keep the
reservation until their final broker event. Idle workers expire after 15 minutes;
an idle worker can also be evicted to admit a new conversation.

An external-engine turn (`ddistro/external/turn/start`, see ASSISTANT_RUNTIME.md
"External engine") takes the same reservation and queue as a Launcher
`turn/start`, and a queued one is cancelled by `turn/interrupt` the same way.
A Claude turn ends only when the Launcher ends it. A detach first interrupts it, and so does an
attached relay that stops polling events for 60 seconds. That releases the
reservation and stops its proposals and live jobs. No public endpoint changes.

Closing the Launcher detaches its relay, not the API. Active requests remain
supervised. Reattaching an active Launcher task refuses until it completes;
it never initializes the same running worker twice. There is no durable-job or
reboot-resume promise. Restarted API requests with unknown outcomes are marked
`outcome_unknown`, never replayed.

For an idle shutdown, root may POST `{}` to `/v1/admin/shutdown` over the control
socket. Active or queued work refuses shutdown. Do not kill or replace the lock.

## Pair a mod

Run as root on the distro:

```sh
ddistro_api register my-mod
# Returns the token once. Store it privately; do not put it in URLs, source or logs.
ddistro_api revoke my-mod
```

Registration is local administration, not an HTTP endpoint. Only SHA-256 token
digests are stored. Revocation denies subsequent authenticated requests; a stream
already authorized may finish its bounded 15-second connection. Public clients
have only `chat`: no host shell, SQL, service control, file changes, Launcher
memory, account methods, approvals or game actions. Their worker has no distro
dynamic tools; native execution, apps, browser and agent spawning are disabled
in its pinned runtime configuration. The existing outer sandbox remains enforced.
Game scratch starts empty, without the Launcher's diagnostic snapshot. Its tasks
live under a separate `game-tasks/` root and never appear in maintenance task lists.

The same provider account signs in once through the trusted Launcher. Provider
credentials are not sent to mods. Each client's conversation/task/thread mapping
is separate. V1 persists game conversation history but does not enable a learned
game memory tool; Launcher memories remain inaccessible. Adding game actions or
configuration permissions requires an explicit future typed adapter and policy.

## Public endpoints

All requests require `Authorization: Bearer TOKEN`. POST bodies are JSON.

| Method/path | Body/result |
|---|---|
| GET `/v1/capabilities` | Effective client identity and permissions |
| POST `/v1/conversations` | `{}` -> `{id}` |
| GET `/v1/conversations` | Own conversation identifiers, newest 100 |
| POST `/v1/conversations/{id}/requests` | `{message, context?, idempotency_key}` -> `{id,state}` |
| GET `/v1/requests/{id}` | Own request state and bounded answer |
| GET `/v1/requests/{id}/events` | SSE text deltas, progress, final state |
| POST `/v1/requests/{id}/cancel` | `{}` -> cancellation requested |

The idempotency key is 1–48 lowercase letters/digits/underscore/hyphen. Exact
retries return the original request; changing its content returns 409. It is
scoped to a client, including the conversation in the content digest. Only one
pending request per conversation is admitted. Polling status is appropriate after
a disconnected stream; never blindly resubmit under a new key.

SSE uses monotonically increasing `id` values and accepts `Last-Event-ID`. The
in-memory ring retains at most 512 events / 8 MiB per worker. An expired cursor
returns 409 `event_gap`; use request status instead. A finished/evicted request
may return JSON with `events_available:false` and its saved answer instead of SSE.

States: queued, running, completed, failed, cancelled, outcome_unknown. Cancel is
best effort until the broker confirms interruption. No automatic mutation retry.
Messages allow 16 Ki characters, context 32 Ki, public bodies 64 KiB. Answers are
capped at 64 Ki characters. Limits: 32 registered clients, 100 conversations per
client, 200 retained requests per client / 1,000 globally; reaching a retention
limit refuses new requests rather than forgetting idempotency records. No silent
deletion or unbounded transcript growth. Public HTTP has 32 handler slots.

## Trust boundary

Bind is IPv4 loopback only. No LAN listener, port forwarding, CORS or browser
origin access is enabled. Windows-host loopback forwarding must be checked on the
actual WSL setup; otherwise call from the mod's WSL server. Do not expose this
unencrypted bearer-token endpoint over a network. Same Windows-user or WSL-root
compromise is outside client-token isolation.

`api/` is root 0700, its database and control socket root 0600. `game-tasks/` is
root:ddagent 0710, with private per-conversation scratch. Root-local relay
access also checks Unix peer credentials. HTTP never accepts a client-supplied
role, owner, permission list, task path or raw Codex method. It returns generic
errors and does not log tokens, prompts, replies or request bodies.

The trusted Launcher relay alone accepts its existing bounded protocol messages;
the broker still enforces method policy and exact destructive-action approvals.
No public approval endpoint or approval-token transfer exists. An in-game request
cannot promote itself to maintenance; the user starts that work in the Launcher.

No game adapter is included. Game-session expiry, action validation and execution
receipts belong to that later integration; a chat response cannot prove game action.
